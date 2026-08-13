"""Acceso de solo lectura a la tabla `suscripciones` (pagos/db.py es la dueña del esquema y del
resto de las operaciones) — usado únicamente para aprobar o rechazar solicitudes de unión al canal
VIP en bot.py::cb_solicitud_union, comparando la identidad de quien pide entrar contra el estado
real de su suscripción."""
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
