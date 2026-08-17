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

-- último email de MercadoPago que funcionó para esta persona en este canal — permite no volver a
-- pedirlo en una renovación futura (ver bot/db.py::obtener_email).
ALTER TABLE suscripciones ADD COLUMN IF NOT EXISTS payer_email TEXT;

-- username de Telegram de cada usuario que interactuó con el bot alguna vez (ver
-- bot/db.py::actualizar_username, que la actualiza en cada update). Separada de suscripciones
-- porque no todo el mundo que escribe al bot llega a pagar, y el username puede cambiar con el
-- tiempo independientemente del estado de una suscripción puntual.
CREATE TABLE IF NOT EXISTS telegram_usuarios (
    telegram_user_id  BIGINT PRIMARY KEY,
    username          TEXT,
    actualizado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- una fila por cada pago confirmado (alta o renovación) — a diferencia de suscripciones (que se
-- pisa in-place), esto es el historial que va a alimentar el futuro comando de métricas de
-- ventas. Se llena desde pagos/logica.py junto con el aviso al canal admin (ver
-- pagos/telegram_client.py::avisar_pago).
CREATE TABLE IF NOT EXISTS pagos_historial (
    id                BIGSERIAL PRIMARY KEY,
    telegram_user_id  BIGINT NOT NULL,
    canal_id          TEXT NOT NULL,
    tipo              TEXT NOT NULL,
    monto             INTEGER,
    payer_email       TEXT,
    creado_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pagos_historial_creado_en ON pagos_historial (creado_en);
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
    payer_email: str | None = None,
) -> bool:
    """Crea o actualiza la suscripción de (telegram_user_id, canal_id). Devuelve True si esta
    llamada es la que activa el acceso por primera vez (fila nueva, o pasa de un estado que no
    era 'activa' a 'activa') — el caller usa esto para decidir si hay que mandar la invitación
    al canal (ver pagos/webhook.py). Una renovación que ya estaba activa devuelve False.

    payer_email es opcional: si el caller no lo tiene a mano (ej. algún camino que no venga de
    aplicar_estado_preapproval), COALESCE conserva el que ya hubiera guardado en vez de borrarlo."""
    ahora = datetime.now(timezone.utc)
    async with pool.acquire() as con:
        async with con.transaction():
            anterior = await con.fetchrow(
                "SELECT estado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
                telegram_user_id, canal_id,
            )
            await con.execute(
                """INSERT INTO suscripciones
                       (telegram_user_id, canal_id, mercadopago_preapproval_id, estado, fecha_inicio, ultima_actualizacion, payer_email)
                   VALUES ($1, $2, $3, $4, $5, $5, $6)
                   ON CONFLICT (telegram_user_id, canal_id) DO UPDATE SET
                       mercadopago_preapproval_id = EXCLUDED.mercadopago_preapproval_id,
                       estado = EXCLUDED.estado,
                       ultima_actualizacion = EXCLUDED.ultima_actualizacion,
                       payer_email = COALESCE(EXCLUDED.payer_email, suscripciones.payer_email)""",
                telegram_user_id, canal_id, mercadopago_preapproval_id, estado, ahora, payer_email,
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


async def esta_activo(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> bool:
    """Duplicado a propósito de bot/db.py::esta_activo (mismo criterio que otras constantes
    chicas entre servicios independientes, ej. CANAL_CHAT_ID). Usado por
    pagos/pagos_tarjeta.py para no cobrar una tarjeta nueva si el usuario ya tiene una
    suscripción activa vigente por cualquiera de los dos métodos de pago — sin este chequeo,
    alguien podría pagar dos veces si toca los dos botones de pago del mismo mensaje."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT estado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila is not None and fila["estado"] == "activa"


async def listar_activas(pool: asyncpg.Pool) -> list[dict]:
    """Usado por pagos/reconciliacion.py: todas las suscripciones que hoy están marcadas como
    activas localmente, para volver a confirmar contra la API de MercadoPago."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id, mercadopago_preapproval_id
               FROM suscripciones WHERE estado = 'activa'""",
        )
    return [dict(f) for f in filas]


async def obtener_username(pool: asyncpg.Pool, telegram_user_id: int) -> str | None:
    """Username capturado por bot/db.py::actualizar_username en la última interacción de esta
    persona con el bot, o None si nunca interactuó (o no tiene username configurado)."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT username FROM telegram_usuarios WHERE telegram_user_id = $1",
            telegram_user_id,
        )
    return fila["username"] if fila else None


async def registrar_pago_historial(
    pool: asyncpg.Pool,
    telegram_user_id: int,
    canal_id: str,
    tipo: str,
    monto: int | None,
    payer_email: str | None,
) -> None:
    """Inserta una fila nueva por cada pago confirmado (alta o renovación) — a diferencia de
    upsert_suscripcion, nunca pisa una fila existente: es el historial crudo para el futuro
    comando de métricas de ventas."""
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pagos_historial (telegram_user_id, canal_id, tipo, monto, payer_email)
               VALUES ($1, $2, $3, $4, $5)""",
            telegram_user_id, canal_id, tipo, monto, payer_email,
        )


async def buscar_por_preapproval_id(pool: asyncpg.Pool, mercadopago_preapproval_id: str) -> dict | None:
    """Usado al procesar un webhook de cobro recurrente (invoice): el invoice trae el
    preapproval_id, no el telegram_user_id/canal_id directamente. Incluye `estado` para que el
    caller detecte una recuperación (ej. de 'vencida' tras un reintento de cobro exitoso).
    Incluye `payer_email` para poder avisar/registrar el pago sin una consulta aparte."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            """SELECT telegram_user_id, canal_id, estado, acceso_hasta, ultimo_invoice_id, payer_email
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


# margen de gracia entre que acceso_hasta pasa y se lo considera "vencido" de verdad — el cobro
# (primero o recurrente) se confirma de forma asíncrona del lado de MercadoPago, documentado en
# ~1h; sin este margen, una suscripción recién activada (acceso_hasta arranca en "ahora" hasta
# que aplicar_pago_recurrente confirma el cobro real, ver pagos/logica.py) ya cuenta como vencida
# desde el instante en que se invita. No confundir con el período de gracia de cancelación
# (COMPLETADO_periodo_gracia_cancelacion.md) — ese deja al usuario con acceso hasta que vence lo
# ya pagado; este es un margen nuevo sobre cuándo se considera vencido ese vencimiento.
_MARGEN_GRACIA_VENCIMIENTO = timedelta(hours=6)


async def listar_vencidas(pool: asyncpg.Pool) -> list[dict]:
    """Usado por pagos/reconciliacion.py: suscripciones cuyo período pagado ya venció (con el
    margen de _MARGEN_GRACIA_VENCIMIENTO ya descontado) y todavía no fueron expulsadas. Incluye
    'activa' a propósito: una fila activa con acceso_hasta pasado (más el margen) significa que un
    cobro recurrente falló en silencio (MercadoPago sigue reintentando sin haber cambiado el
    status de la preapproval todavía) — no solo cancelaciones/pausas explícitas. Se incluye
    `estado` para que el caller distinga ambos casos al marcar el resultado."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id, estado FROM suscripciones
               WHERE estado IN ('activa', 'cancelada', 'pausada')
                 AND acceso_hasta <= now() - $1::interval""",
            _MARGEN_GRACIA_VENCIMIENTO,
        )
    return [dict(f) for f in filas]
