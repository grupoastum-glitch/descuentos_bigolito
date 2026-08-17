"""Acceso de solo lectura a la tabla `suscripciones` (pagos/db.py es la dueña del esquema y del
resto de las operaciones) — usado únicamente para aprobar o rechazar solicitudes de unión al canal
VIP en bot.py::cb_solicitud_union, comparando la identidad de quien pide entrar contra el estado
real de su suscripción.

Excepciones a "solo lectura": actualizar_email() y actualizar_username() — ver sus docstrings."""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger("bot.db")

_pool: asyncpg.Pool | None = None


async def conectar(database_url: str) -> asyncpg.Pool:
    """Idempotente: si ya hay un pool abierto en este proceso, lo reusa. No bootstrapea el
    esquema — si conectara antes que pagos/db.py alguna vez, esta_activo() simplemente no
    encontraría filas, que es el comportamiento correcto (rechazar)."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
        log.info("Pool de PostgreSQL conectado.")
    return _pool


async def esta_activo(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> bool:
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT estado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila is not None and fila["estado"] == "activa"


async def obtener_acceso_hasta(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str):
    """Fecha (UTC, tz-aware) hasta la que el usuario tiene acceso pagado, o None si no hay fila.
    Para mostrarla a un humano, convertir a hora de Chile antes de formatear."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT acceso_hasta FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila["acceso_hasta"] if fila else None


async def canales_activos(pool: asyncpg.Pool, telegram_user_id: int) -> list[str]:
    """canal_id de cada suscripción activa de este usuario, o [] si no tiene ninguna."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            "SELECT canal_id FROM suscripciones WHERE telegram_user_id = $1 AND estado = 'activa'",
            telegram_user_id,
        )
    return [f["canal_id"] for f in filas]


async def obtener_email(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> str | None:
    """Último email de MercadoPago que funcionó para esta persona en este canal, o None si nunca
    se suscribió antes — evita volver a pedirlo en una renovación."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT payer_email FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila["payer_email"] if fila and fila["payer_email"] else None


async def actualizar_email(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str, email: str) -> None:
    """Guarda el email que el usuario acaba de escribir y confirmar, para no volver a pedirlo en
    la próxima renovación (ver obtener_email). Única excepción a "solo lectura" de este módulo:
    solo actualiza una fila que ya existe, nunca inserta — crear filas sigue siendo
    responsabilidad exclusiva de pagos/db.py vía el webhook. Necesario porque
    preapproval.get("payer_email") (pagos/logica.py) nunca viene poblado en la respuesta real de
    la API de MercadoPago (confirmado contra la documentación oficial: el recurso de preapproval
    solo trae payer_id, no payer_email) — este es el único lugar que persiste ese dato."""
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE suscripciones SET payer_email = $3 WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id, email,
        )


async def actualizar_username(pool: asyncpg.Pool, telegram_user_id: int, username: str | None) -> None:
    """Guarda/actualiza el username de Telegram de quien acaba de interactuar con el bot, en la
    tabla telegram_usuarios (esquema bootstrapeado por pagos/db.py). Se llama en cada update
    recibido (ver bot.py::capturar_usuario) para que pagos/logica.py pueda mostrarlo en el aviso
    de venta al canal admin sin tener que pedirlo en vivo a la API de Telegram al momento del
    pago (que podría fallar si el usuario nunca inició conversación con el bot)."""
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO telegram_usuarios (telegram_user_id, username, actualizado_en)
               VALUES ($1, $2, now())
               ON CONFLICT (telegram_user_id) DO UPDATE SET
                   username = EXCLUDED.username, actualizado_en = EXCLUDED.actualizado_en""",
            telegram_user_id, username,
        )
