"""Acceso a PostgreSQL (mismo addon que ya usa scraper/db.py) para el estado de suscripciones
pagas. Mismo patrón que scraper/db.py: pool creado una vez por proceso, esquema bootstrapeado con
CREATE TABLE IF NOT EXISTS en el primer connect — sin herramienta de migraciones.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

import config

log = logging.getLogger("pagos.db")

_DDL = """
CREATE TABLE IF NOT EXISTS suscripciones (
    id                          BIGSERIAL PRIMARY KEY,
    telegram_user_id            BIGINT NOT NULL,
    canal_id                    TEXT NOT NULL,
    mercadopago_preapproval_id  TEXT NOT NULL,
    estado                      TEXT NOT NULL,
    fecha_inicio                TIMESTAMPTZ NOT NULL,
    ultima_actualizacion        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (telegram_user_id, canal_id)
);

CREATE INDEX IF NOT EXISTS ix_suscripciones_estado ON suscripciones (estado);

-- acceso_hasta: hasta cuándo tiene acceso pagado, independiente del estado actual — permite
-- respetar el período ya pagado al cancelar en vez de expulsar al instante (ver
-- PLAN_periodo_gracia_cancelacion.md). ultimo_invoice_id evita extender el acceso dos veces si
-- MercadoPago reenvía el mismo webhook de cobro.
ALTER TABLE suscripciones ADD COLUMN IF NOT EXISTS acceso_hasta TIMESTAMPTZ;
ALTER TABLE suscripciones ADD COLUMN IF NOT EXISTS ultimo_invoice_id TEXT;
"""

_pool: asyncpg.Pool | None = None


async def conectar() -> asyncpg.Pool:
    """Idempotente: si ya hay un pool abierto en este proceso, lo reusa."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as con:
            await con.execute(_DDL)
        log.info("Pool de PostgreSQL conectado y esquema verificado.")
    return _pool


async def cerrar() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def upsert_suscripcion(
    pool: asyncpg.Pool,
    telegram_user_id: int,
    canal_id: str,
    mercadopago_preapproval_id: str,
    estado: str,
) -> bool:
    """Crea o actualiza la suscripción de (telegram_user_id, canal_id). Devuelve True si esta
    llamada es la que activa el acceso por primera vez (fila nueva, o pasa de un estado que no
    era 'activa' a 'activa') — el caller usa esto para decidir si hay que mandar la invitación
    al canal (ver pagos/webhook.py). Una renovación que ya estaba activa devuelve False."""
    ahora = datetime.now(timezone.utc)
    async with pool.acquire() as con:
        async with con.transaction():
            anterior = await con.fetchrow(
                "SELECT estado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
                telegram_user_id, canal_id,
            )
            await con.execute(
                """INSERT INTO suscripciones
                       (telegram_user_id, canal_id, mercadopago_preapproval_id, estado, fecha_inicio, ultima_actualizacion)
                   VALUES ($1, $2, $3, $4, $5, $5)
                   ON CONFLICT (telegram_user_id, canal_id) DO UPDATE SET
                       mercadopago_preapproval_id = EXCLUDED.mercadopago_preapproval_id,
                       estado = EXCLUDED.estado,
                       ultima_actualizacion = EXCLUDED.ultima_actualizacion""",
                telegram_user_id, canal_id, mercadopago_preapproval_id, estado, ahora,
            )
    era_activa = anterior is not None and anterior["estado"] == "activa"
    return estado == "activa" and not era_activa


async def marcar_estado(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str, estado: str) -> None:
    """Cambia el estado de una suscripción existente (pausada/cancelada) sin tocar
    mercadopago_preapproval_id ni fecha_inicio."""
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE suscripciones SET estado = $3, ultima_actualizacion = now()
               WHERE telegram_user_id = $1 AND canal_id = $2""",
            telegram_user_id, canal_id, estado,
        )


async def listar_activas(pool: asyncpg.Pool) -> list[dict]:
    """Usado por pagos/reconciliacion.py: todas las suscripciones que hoy están marcadas como
    activas localmente, para volver a confirmar contra la API de MercadoPago."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id, mercadopago_preapproval_id
               FROM suscripciones WHERE estado = 'activa'""",
        )
    return [dict(f) for f in filas]


async def buscar_por_preapproval_id(pool: asyncpg.Pool, mercadopago_preapproval_id: str) -> dict | None:
    """Usado al procesar un webhook de cobro recurrente (invoice): el invoice trae el
    preapproval_id, no el telegram_user_id/canal_id directamente."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            """SELECT telegram_user_id, canal_id, acceso_hasta, ultimo_invoice_id
               FROM suscripciones WHERE mercadopago_preapproval_id = $1""",
            mercadopago_preapproval_id,
        )
    return dict(fila) if fila else None


async def extender_acceso(
    pool: asyncpg.Pool,
    telegram_user_id: int,
    canal_id: str,
    periodo: timedelta,
    invoice_id: str | None = None,
) -> None:
    """Suma un período de acceso pagado. Parte del mayor entre `acceso_hasta` actual y ahora (no
    siempre `acceso_hasta` a secas) para cubrir tanto la primera activación (todavía sin valor)
    como un webhook que llega tarde. `invoice_id` se guarda para no procesar el mismo cobro dos
    veces si MercadoPago reenvía la notificación (la primera activación no tiene invoice propio,
    así que pasa None)."""
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE suscripciones
               SET acceso_hasta = GREATEST(COALESCE(acceso_hasta, now()), now()) + $3::interval,
                   ultimo_invoice_id = COALESCE($4, ultimo_invoice_id),
                   ultima_actualizacion = now()
               WHERE telegram_user_id = $1 AND canal_id = $2""",
            telegram_user_id, canal_id, periodo, invoice_id,
        )


async def listar_vencidas(pool: asyncpg.Pool) -> list[dict]:
    """Usado por pagos/reconciliacion.py: suscripciones canceladas/pausadas cuyo período pagado ya
    venció y todavía no fueron expulsadas."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id FROM suscripciones
               WHERE estado IN ('cancelada', 'pausada') AND acceso_hasta <= now()""",
        )
    return [dict(f) for f in filas]
