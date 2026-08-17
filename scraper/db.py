"""Acceso a PostgreSQL (addon de Railway) para el estado de productos/historial de precios.
Reemplaza scraper/estado_precios_<tienda>.json — ver ofertas_writer.py.

Pool de conexiones creado una vez por corrida (ver main._correr) y cerrado al final. Sin
herramienta de migraciones: dos tablas, esquema bootstrapeado con CREATE TABLE IF NOT EXISTS en
el primer connect (ver conectar()) — no hace falta Alembic para esto.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

import config

log = logging.getLogger("scraper.db")

_DDL = """
CREATE TABLE IF NOT EXISTS productos (
    id                   TEXT PRIMARY KEY,
    tienda_id            TEXT NOT NULL,
    producto_id          TEXT NOT NULL,
    url                  TEXT NOT NULL,
    titulo               TEXT NOT NULL,
    marca                TEXT,
    imagen               TEXT,
    precio_normal        INTEGER,
    precio_actual        INTEGER NOT NULL,
    descuento_pct        INTEGER NOT NULL,
    primera_deteccion    TIMESTAMPTZ NOT NULL,
    ultima_actualizacion TIMESTAMPTZ NOT NULL,
    activo               BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS ix_productos_tienda_activo
    ON productos (tienda_id, activo);

CREATE INDEX IF NOT EXISTS ix_productos_activo_descuento
    ON productos (activo, descuento_pct) WHERE activo;

CREATE TABLE IF NOT EXISTS historial_precios (
    id            BIGSERIAL PRIMARY KEY,
    producto_id   TEXT NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    precio        INTEGER NOT NULL,
    descuento_pct INTEGER NOT NULL,
    fecha         TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_historial_producto_fecha
    ON historial_precios (producto_id, fecha);
"""

_pool: asyncpg.Pool | None = None


async def conectar() -> asyncpg.Pool:
    """Idempotente: si ya hay un pool abierto en esta corrida, lo reusa."""
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


def _parse_iso(fecha_iso: str) -> datetime:
    return datetime.strptime(fecha_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fila_a_dict_historial(fila: asyncpg.Record) -> dict:
    return {
        "id": fila["id"],
        "precio": fila["precio"],
        "descuento_pct": fila["descuento_pct"],
        "fecha": fila["fecha"].strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def cargar_productos_con_historial(pool: asyncpg.Pool, claves: list[str]) -> dict[str, dict]:
    """Trae, en 2 queries, SOLO los productos pedidos (los detectados HOY para una tienda) + su
    historial completo — a diferencia del JSON, que cargaba el archivo entero de la tienda
    aunque la mayoría de sus productos no se hubieran detectado en esta corrida.
    Devuelve {clave: {...columnas de productos..., "historial": [...]}}."""
    if not claves:
        return {}
    async with pool.acquire() as con:
        filas_productos = await con.fetch(
            "SELECT * FROM productos WHERE id = ANY($1::text[])", claves,
        )
        filas_historial = await con.fetch(
            """SELECT id, producto_id, precio, descuento_pct, fecha FROM historial_precios
               WHERE producto_id = ANY($1::text[]) ORDER BY producto_id, fecha, id""",
            claves,
        )

    historial_por_id: dict[str, list[dict]] = {}
    for fila in filas_historial:
        historial_por_id.setdefault(fila["producto_id"], []).append(_fila_a_dict_historial(fila))

    resultado = {}
    for fila in filas_productos:
        registro = dict(fila)
        registro["primera_deteccion"] = fila["primera_deteccion"].strftime("%Y-%m-%dT%H:%M:%SZ")
        registro["ultima_actualizacion"] = fila["ultima_actualizacion"].strftime("%Y-%m-%dT%H:%M:%SZ")
        registro["historial"] = historial_por_id.get(fila["id"], [])
        resultado[fila["id"]] = registro
    return resultado


async def upsert_productos(pool: asyncpg.Pool, registros: list[dict]) -> None:
    """UPSERT bulk de los productos tocados en esta corrida. NO toca historial_precios."""
    if not registros:
        return
    filas = [
        (
            r["id"], r["tienda_id"], r["producto_id"], r["url"], r["titulo"], r["marca"],
            r["imagen"], r["precio_normal"], r["precio_actual"], r["descuento_pct"],
            _parse_iso(r["primera_deteccion"]), _parse_iso(r["ultima_actualizacion"]), r["activo"],
        )
        for r in registros
    ]
    async with pool.acquire() as con:
        await con.executemany(
            """INSERT INTO productos (id, tienda_id, producto_id, url, titulo, marca, imagen,
                                       precio_normal, precio_actual, descuento_pct,
                                       primera_deteccion, ultima_actualizacion, activo)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (id) DO UPDATE SET
                   url = EXCLUDED.url, titulo = EXCLUDED.titulo, marca = EXCLUDED.marca,
                   imagen = EXCLUDED.imagen, precio_normal = EXCLUDED.precio_normal,
                   precio_actual = EXCLUDED.precio_actual, descuento_pct = EXCLUDED.descuento_pct,
                   ultima_actualizacion = EXCLUDED.ultima_actualizacion, activo = EXCLUDED.activo""",
            filas,
        )


async def marcar_inactivos(pool: asyncpg.Pool, tienda_id: str, ids_vistos_hoy: list[str]) -> None:
    """Equivalente al loop de antes que ponía activo=False a lo que no está en ids_vistos_hoy."""
    async with pool.acquire() as con:
        await con.execute(
            """UPDATE productos SET activo = FALSE
               WHERE tienda_id = $1 AND activo = TRUE AND NOT (id = ANY($2::text[]))""",
            tienda_id, ids_vistos_hoy,
        )


async def insertar_evento_historial(
    pool: asyncpg.Pool, producto_id: str, precio: int, descuento_pct: int, fecha_iso: str
) -> None:
    """Un solo INSERT — usado por el callback on_publicada de telegram_publisher, una vez por
    oferta confirmada. Barato: no hace falta batchear (a diferencia de un commit/push a git)."""
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO historial_precios (producto_id, precio, descuento_pct, fecha) VALUES ($1,$2,$3,$4)",
            producto_id, precio, descuento_pct, _parse_iso(fecha_iso),
        )


async def actualizar_fecha_evento_historial(pool: asyncpg.Pool, historial_id: int, fecha_iso: str) -> None:
    """UPDATE en vez de INSERT — usado por registrar_evento_publicado cuando Regla 3 se cumple
    pero precio/descuento no cambiaron desde la última fila guardada Y esa fila ya no es la que
    sostiene el mínimo histórico (ver DecisionPublicacion.puede_reusar_fila en ofertas_writer.py).
    Evita que historial_precios crezca sin límite con productos evergreen que se re-publican cada
    HORAS_REPUBLICACION_REGLA3 para siempre."""
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE historial_precios SET fecha = $1 WHERE id = $2",
            _parse_iso(fecha_iso), historial_id,
        )


async def feed_activo(
    pool: asyncpg.Pool, descuento_minimo: int, umbral_vip: int, limite_gratis: int, limite_vip: int
) -> list[dict]:
    """Arma el teaser priorizando el tramo gratis: trae hasta `limite_gratis` de por debajo del
    umbral VIP y hasta `limite_vip` del tramo VIP por separado, para que las VIP (que suelen
    salir en tandas) no le ganen el lugar a las gratis — ver MAX_OFERTAS_VIP_WEB_TEASER en
    config.py. Se combinan y reordenan por fecha para el feed final."""
    columnas = "id, tienda_id, titulo, descuento_pct, url, imagen, primera_deteccion"
    async with pool.acquire() as con:
        gratis = await con.fetch(
            f"""SELECT {columnas} FROM productos
                WHERE activo = TRUE AND descuento_pct >= $1 AND descuento_pct < $2
                ORDER BY primera_deteccion DESC LIMIT $3""",
            descuento_minimo, umbral_vip, limite_gratis,
        )
        vip = await con.fetch(
            f"""SELECT {columnas} FROM productos
                WHERE activo = TRUE AND descuento_pct >= $1
                ORDER BY primera_deteccion DESC LIMIT $2""",
            umbral_vip, limite_vip,
        )
    filas = sorted([*gratis, *vip], key=lambda f: f["primera_deteccion"], reverse=True)
    return [dict(f) for f in filas]
