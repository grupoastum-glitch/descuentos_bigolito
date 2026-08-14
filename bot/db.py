"""Acceso de solo lectura a la tabla `suscripciones` (pagos/db.py es la dueña del esquema y del
resto de las operaciones) — usado únicamente para aprobar o rechazar solicitudes de unión al canal
VIP en bot.py::cb_solicitud_union, comparando la identidad de quien pide entrar contra el estado
real de su suscripción.

Única excepción a "solo lectura": actualizar_email() — ver su docstring."""
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
