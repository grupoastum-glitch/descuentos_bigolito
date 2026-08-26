"""Acceso de solo lectura a la tabla `suscripciones` (pagos/db.py es la dueña del esquema y del
resto de las operaciones) — usado únicamente para aprobar o rechazar solicitudes de unión al canal
VIP en bot.py::cb_solicitud_union, comparando la identidad de quien pide entrar contra el estado
real de su suscripción.

Excepciones a "solo lectura": actualizar_email() y actualizar_username() — ver sus docstrings.
estadisticas() también es de solo lectura, pero contra pagos_historial (tabla nueva, ver
pagos/db.py), no contra suscripciones.

set_pausa_manual()/leer_pausa_manual() son otra excepción: la tabla `pausa_manual` (comandos
/pausar, /reanudar, /estado en bot.py) no tiene un dueño claro como suscripciones — el bot la
escribe, scraper/main.py y scraper/publicar.py la leen, sin orden de arranque garantizado entre
servicios — por eso, a diferencia del resto de este módulo, acá sí se bootstrapea su esquema
(ver conectar()), duplicado a propósito con el mismo DDL en scraper/db.py."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

log = logging.getLogger("bot.db")

_pool: asyncpg.Pool | None = None

_DDL_PAUSA_MANUAL = """
CREATE TABLE IF NOT EXISTS pausa_manual (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    activa BOOLEAN NOT NULL DEFAULT false,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def conectar(database_url: str) -> asyncpg.Pool:
    """Idempotente: si ya hay un pool abierto en este proceso, lo reusa. No bootstrapea el
    esquema de suscripciones/pagos_historial (esos son de pagos/db.py) — si conectara antes que
    pagos/db.py alguna vez, esta_activo() simplemente no encontraría filas, que es el
    comportamiento correcto (rechazar). `pausa_manual` es la excepción: no tiene un dueño único,
    ver docstring del módulo."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
        async with _pool.acquire() as con:
            await con.execute(_DDL_PAUSA_MANUAL)
        log.info("Pool de PostgreSQL conectado.")
    return _pool


async def esta_activo(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> bool:
    """Vigente = mismo criterio que pagos/db.py::listar_vencidas: 'activa' cubre el caso normal,
    'cancelada'/'pausada' con acceso_hasta todavía en el futuro también cuentan como vigentes —
    es el período de gracia de cancelación (ver COMPLETADO_periodo_gracia_cancelacion.md), que
    deja al usuario con acceso hasta que vence lo ya pagado en vez de cortarlo al cancelar. 'prueba'
    (prueba gratis, ver iniciar_prueba_gratis) se trata igual: vigente hasta que vence
    acceso_hasta, mismo mecanismo de expulsión que una suscripción paga vencida."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            """SELECT 1 FROM suscripciones
               WHERE telegram_user_id = $1 AND canal_id = $2
                 AND estado IN ('activa', 'cancelada', 'pausada', 'prueba') AND acceso_hasta > now()""",
            telegram_user_id, canal_id,
        )
    return fila is not None


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
    """canal_id de cada suscripción vigente de este usuario (mismo criterio que esta_activo: incluye
    el período de gracia de cancelación y la prueba gratis), o [] si no tiene ninguna."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT canal_id FROM suscripciones
               WHERE telegram_user_id = $1
                 AND estado IN ('activa', 'cancelada', 'pausada', 'prueba') AND acceso_hasta > now()""",
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


async def estadisticas(pool: asyncpg.Pool) -> dict:
    """Resumen histórico de ventas para el comando /stats admin-only (ver bot.py::cmd_stats).
    Dos queries porque activos_ahora sale de suscripciones (estado actual) y el resto de
    pagos_historial (una fila por venta confirmada, ver pagos/db.py). COUNT(*) siempre da 0 sobre
    una tabla vacía, pero SUM da NULL — de ahí el COALESCE en los ingresos."""
    async with pool.acquire() as con:
        activos_ahora = await con.fetchval(
            "SELECT COUNT(*) FROM suscripciones WHERE estado = 'activa'"
        )
        fila = await con.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE tipo = 'alta') AS altas,
                   COUNT(*) FILTER (WHERE tipo = 'renovacion') AS renovaciones,
                   COUNT(DISTINCT telegram_user_id) AS personas_unicas,
                   COALESCE(SUM(monto), 0) AS ingresos_totales,
                   COALESCE(SUM(monto) FILTER (WHERE creado_en >= date_trunc('month', now())), 0) AS ingresos_mes
               FROM pagos_historial"""
        )
    return {"activos_ahora": activos_ahora, **dict(fila)}


async def set_pausa_manual(pool: asyncpg.Pool, activa: bool) -> None:
    """Escrita por /pausar y /reanudar (ver bot.py). Fila única (id=1, ver _DDL_PAUSA_MANUAL)."""
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pausa_manual (id, activa, actualizado_en) VALUES (1, $1, now())
               ON CONFLICT (id) DO UPDATE SET activa = EXCLUDED.activa, actualizado_en = EXCLUDED.actualizado_en""",
            activa,
        )


async def leer_pausa_manual(pool: asyncpg.Pool) -> tuple[bool, datetime | None]:
    """(activa, desde_cuándo) — para el comando /estado. (False, None) si nunca se usó /pausar."""
    async with pool.acquire() as con:
        fila = await con.fetchrow("SELECT activa, actualizado_en FROM pausa_manual WHERE id = 1")
    if not fila:
        return False, None
    return bool(fila["activa"]), fila["actualizado_en"]


# duración de la prueba gratis y valor centinela de mercadopago_preapproval_id — duplicados a
# propósito de pagos/db.py (mismos valores, mismo criterio que CANAL_CHAT_ID en bot.py): bot/ es
# quien crea la fila de prueba (el usuario la activa charlando con el bot), pagos/ es quien la
# expulsa al vencer (pagos/reconciliacion.py) y le manda el aviso previo.
_DURACION_PRUEBA_GRATIS = timedelta(days=30)
PREAPPROVAL_ID_PRUEBA_GRATIS = "TRIAL"


async def existe_registro(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> bool:
    """True si ya existe cualquier fila (pagada, vencida, expirada, en prueba) para este
    (telegram_user_id, canal_id) — usado para decidir si ofrecer la prueba gratis: por el UNIQUE
    de la tabla, quien ya tuvo alguna vez una fila acá no vuelve a calificar."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT 1 FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila is not None


async def iniciar_prueba_gratis(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> None:
    """Crea la fila de prueba gratis: estado='prueba', sin preapproval real, acceso_hasta a 30 días
    desde ahora. El caller debe haber chequeado existe_registro() antes — ON CONFLICT DO NOTHING
    acá es solo por seguridad ante un doble clic, no reemplaza ese chequeo.

    acceso_hasta se calcula acá en Python (no `$4 + intervalo::interval` en el SQL) porque
    reusar el mismo parámetro $4 como timestamptz en unas columnas y dentro de una suma con
    interval en otra le genera a asyncpg un AmbiguousParameterError ("inconsistent types deduced
    for parameter $4: interval versus timestamp with time zone") — reproducido en vivo con una
    cuenta nueva de verdad, asyncpg no logra inferir un único tipo para el parámetro repetido en
    esos dos contextos."""
    ahora = datetime.now(timezone.utc)
    acceso_hasta = ahora + _DURACION_PRUEBA_GRATIS
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO suscripciones
                   (telegram_user_id, canal_id, mercadopago_preapproval_id, estado,
                    fecha_inicio, ultima_actualizacion, acceso_hasta)
               VALUES ($1, $2, $3, 'prueba', $4, $4, $5)
               ON CONFLICT (telegram_user_id, canal_id) DO NOTHING""",
            telegram_user_id, canal_id, PREAPPROVAL_ID_PRUEBA_GRATIS, ahora, acceso_hasta,
        )


async def registrar_prueba_historial(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> None:
    """Deja rastro de la activación de una prueba gratis en pagos_historial (tabla bootstrapeada
    por pagos/db.py, ver su _DDL) con tipo='prueba' — separado de 'alta'/'renovacion' a propósito,
    para que un futuro /stats no confunda activaciones gratis con ventas reales."""
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO pagos_historial (telegram_user_id, canal_id, tipo, monto, payer_email)
               VALUES ($1, $2, 'prueba', 0, NULL)""",
            telegram_user_id, canal_id,
        )
