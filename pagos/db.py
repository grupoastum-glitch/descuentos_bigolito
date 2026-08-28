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

-- cuándo se mandó el DM de "tu prueba gratis está por vencer" (ver listar_pruebas_por_vencer) —
-- NULL hasta que se manda, evita reenviarlo cada día mientras la prueba sigue en la ventana de
-- aviso. Solo se usa para estado='prueba'; en filas pagas queda NULL siempre.
ALTER TABLE suscripciones ADD COLUMN IF NOT EXISTS recordatorio_prueba_enviado_en TIMESTAMPTZ;

-- precio (CLP) que quedó fijado para esta persona en este canal la primera vez que pagó — ver
-- escalamiento de precios por tramos (pagos/precios.py). NULL hasta el primer pago confirmado;
-- una vez fijado nunca se pisa (ver el COALESCE al revés en upsert_suscripcion, comparado con
-- payer_email), para que sobreviva a renovaciones y a cancelar/resuscribirse más adelante.
ALTER TABLE suscripciones ADD COLUMN IF NOT EXISTS precio_congelado INTEGER;

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
    precio_congelado: int | None = None,
) -> bool:
    """Crea o actualiza la suscripción de (telegram_user_id, canal_id). Devuelve True si esta
    llamada es la que activa el acceso por primera vez (fila nueva, o pasa de un estado que no
    era 'activa' a 'activa') — el caller usa esto para decidir si hay que mandar la invitación
    al canal (ver pagos/webhook.py). Una renovación que ya estaba activa devuelve False.

    payer_email es opcional: si el caller no lo tiene a mano (ej. algún camino que no venga de
    aplicar_estado_preapproval), COALESCE conserva el que ya hubiera guardado en vez de borrarlo.

    precio_congelado: el monto real (transaction_amount de MercadoPago) de esta preapproval — ver
    pagos/precios.py. A diferencia de payer_email, el COALESCE va al revés (el valor que ya
    hubiera en la fila gana sobre el nuevo): una vez que una persona fijó su precio, no debe
    cambiar nunca, ni siquiera si este upsert se llama de nuevo para la misma fila (renovación,
    o resuscripción tras cancelar)."""
    ahora = datetime.now(timezone.utc)
    async with pool.acquire() as con:
        async with con.transaction():
            anterior = await con.fetchrow(
                "SELECT estado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
                telegram_user_id, canal_id,
            )
            await con.execute(
                """INSERT INTO suscripciones
                       (telegram_user_id, canal_id, mercadopago_preapproval_id, estado, fecha_inicio, ultima_actualizacion, payer_email, precio_congelado)
                   VALUES ($1, $2, $3, $4, $5, $5, $6, $7)
                   ON CONFLICT (telegram_user_id, canal_id) DO UPDATE SET
                       mercadopago_preapproval_id = EXCLUDED.mercadopago_preapproval_id,
                       estado = EXCLUDED.estado,
                       ultima_actualizacion = EXCLUDED.ultima_actualizacion,
                       payer_email = COALESCE(EXCLUDED.payer_email, suscripciones.payer_email),
                       precio_congelado = COALESCE(suscripciones.precio_congelado, EXCLUDED.precio_congelado)""",
                telegram_user_id, canal_id, mercadopago_preapproval_id, estado, ahora, payer_email, precio_congelado,
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


async def listar_pares_registrados(pool: asyncpg.Pool) -> dict[tuple[int, str], dict]:
    """Todas las filas de suscripciones indexadas por (telegram_user_id, canal_id) — usado por
    pagos/reconciliacion.py para el descubrimiento de preapprovals perdidas (clientes nuevos y
    resuscripciones, ver _descubrir_preapprovals_perdidas). Se trae estado y
    mercadopago_preapproval_id de cada par para que el caller decida si una preapproval
    'authorized' de MercadoPago representa algo que ya sabíamos o algo que se perdió."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            "SELECT telegram_user_id, canal_id, estado, mercadopago_preapproval_id FROM suscripciones",
        )
    return {(f["telegram_user_id"], f["canal_id"]): dict(f) for f in filas}


async def obtener_username(pool: asyncpg.Pool, telegram_user_id: int) -> str | None:
    """Username capturado por bot/db.py::actualizar_username en la última interacción de esta
    persona con el bot, o None si nunca interactuó (o no tiene username configurado)."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT username FROM telegram_usuarios WHERE telegram_user_id = $1",
            telegram_user_id,
        )
    return fila["username"] if fila else None


async def obtener_precio_congelado(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> int | None:
    """El precio que ya quedó fijado para esta persona en este canal (ver precio_congelado en el
    DDL), o None si nunca pagó acá todavía — en ese caso pagos/precios.py::resolver_precio calcula
    el tramo que le toca según cuánta gente pagó antes."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT precio_congelado FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila["precio_congelado"] if fila else None


async def contar_precios_congelados(pool: asyncpg.Pool, canal_id: str) -> int:
    """Total histórico de personas que alguna vez fijaron un precio en este canal (primer pago
    confirmado) — la cantidad que pagos/precios.py usa para decidir en qué tramo cae la próxima
    persona nueva. Nunca baja aunque haya cancelaciones, a propósito (decisión del usuario): una
    fila cuenta para siempre una vez que precio_congelado quedó seteado, sin importar su estado
    actual."""
    async with pool.acquire() as con:
        return await con.fetchval(
            "SELECT COUNT(*) FROM suscripciones WHERE canal_id = $1 AND precio_congelado IS NOT NULL",
            canal_id,
        )


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
    status de la preapproval todavía) — no solo cancelaciones/pausas explícitas. También incluye
    'prueba' (prueba gratis sin convertir a pago) — cae naturalmente en la rama "expirada" del
    caller porque no es 'activa', mismo tratamiento final que una cancelación explícita. Se
    incluye `estado` para que el caller distinga los casos al marcar el resultado."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id, estado FROM suscripciones
               WHERE estado IN ('activa', 'cancelada', 'pausada', 'prueba')
                 AND acceso_hasta <= now() - $1::interval""",
            _MARGEN_GRACIA_VENCIMIENTO,
        )
    return [dict(f) for f in filas]


# duración de la prueba gratis (ver iniciar_prueba_gratis) — mismo criterio de "mes" que
# logica.py::_periodo_de usa para frequency_type="months" (30 días).
_DURACION_PRUEBA_GRATIS = timedelta(days=30)

# valor centinela de mercadopago_preapproval_id para filas de prueba gratis: la columna es
# NOT NULL pero una prueba no tiene preapproval real detrás. No colisiona con nada porque el
# UNIQUE de la tabla es sobre (telegram_user_id, canal_id), no sobre esta columna. listar_activas
# filtra por estado='activa' (nunca 'prueba'), así que este valor nunca se manda a la API de
# MercadoPago durante la reconfirmación diaria.
PREAPPROVAL_ID_PRUEBA_GRATIS = "TRIAL"


async def existe_registro(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> bool:
    """True si ya existe cualquier fila (activa, vencida, expirada, en prueba, lo que sea) para
    este (telegram_user_id, canal_id) — por el UNIQUE de la tabla, solo puede existir una. Se usa
    para decidir si ofrecer la prueba gratis: quien ya tuvo alguna vez acceso a este canal (pagado
    o de prueba) no vuelve a calificar."""
    async with pool.acquire() as con:
        fila = await con.fetchrow(
            "SELECT 1 FROM suscripciones WHERE telegram_user_id = $1 AND canal_id = $2",
            telegram_user_id, canal_id,
        )
    return fila is not None


async def iniciar_prueba_gratis(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> None:
    """Crea la fila de prueba gratis: estado='prueba', sin preapproval real (ver
    PREAPPROVAL_ID_PRUEBA_GRATIS), acceso_hasta a _DURACION_PRUEBA_GRATIS desde ahora. El caller
    debe haber chequeado existe_registro() antes — ON CONFLICT DO NOTHING acá es solo para no
    romper ante un doble clic simultáneo, no reemplaza ese chequeo (si ya hay fila, esta llamada
    no hace nada y el caller ya mostró el mensaje de "ya usaste tu prueba" con la fila vieja).

    acceso_hasta se calcula acá en Python (no `$4 + intervalo::interval` en el SQL) porque reusar
    el mismo parámetro $4 como timestamptz en unas columnas y dentro de una suma con interval en
    otra le genera a asyncpg un AmbiguousParameterError ("inconsistent types deduced for
    parameter $4: interval versus timestamp with time zone") — reproducido en vivo (mismo bug en
    bot/db.py::iniciar_prueba_gratis) con una cuenta nueva de verdad."""
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


async def listar_pruebas_por_vencer(pool: asyncpg.Pool, dias_aviso: int = 3) -> list[dict]:
    """Usado por pagos/reconciliacion.py para el aviso previo, exclusivo de la prueba gratis (a
    diferencia de una suscripción paga, cuyo cobro es automático y no tiene aviso previo): pruebas
    activas cuyo acceso_hasta cae dentro de los próximos `dias_aviso` días y que todavía no
    recibieron el DM de aviso. AND acceso_hasta > now() evita avisar a una que ya venció (esa la
    agarra listar_vencidas, no esta)."""
    async with pool.acquire() as con:
        filas = await con.fetch(
            """SELECT telegram_user_id, canal_id, acceso_hasta FROM suscripciones
               WHERE estado = 'prueba'
                 AND recordatorio_prueba_enviado_en IS NULL
                 AND acceso_hasta > now()
                 AND acceso_hasta <= now() + $1::interval""",
            timedelta(days=dias_aviso),
        )
    return [dict(f) for f in filas]


async def marcar_recordatorio_prueba_enviado(pool: asyncpg.Pool, telegram_user_id: int, canal_id: str) -> None:
    """Marca que ya se mandó el DM de aviso de vencimiento de prueba, para no repetirlo en la
    próxima corrida diaria de reconciliacion.py."""
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE suscripciones SET recordatorio_prueba_enviado_en = now()
               WHERE telegram_user_id = $1 AND canal_id = $2""",
            telegram_user_id, canal_id,
        )
