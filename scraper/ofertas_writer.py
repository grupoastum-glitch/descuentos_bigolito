"""Fusiona las fuentes de datos, decide qué se publica y escribe el estado por tienda (tabla
`productos` en Postgres — ver db.py) + el feed combinado web/data/ofertas.json (ver
construir_feed_web).

Modelo de publicación (reemplaza "cualquier baja de precio"): un producto solo se publica en
Telegram cuando es un récord real —

- Regla 1: el precio actual es el más bajo jamás registrado para ese producto.
- Regla 2: el % de descuento es el más alto visto, aunque el precio no sea el mínimo histórico.
- Regla 3: pasaron HORAS_REPUBLICACION_REGLA3 horas desde la última publicación y el producto
  sigue siendo récord (de precio o de descuento) sin haber cambiado desde entonces — evita que
  una oferta buena quede "enterrada" para suscriptores nuevos.

`historial` guarda un evento por cada publicación real (no cada scrape), usado para derivar el
mínimo histórico y el descuento máximo. Sin tope de publicaciones por día — un producto puede
volver a postearse el mismo día si vuelve a cumplir alguna de las Reglas 1/2/3 (ej. Regla 3 cada
HORAS_REPUBLICACION_REGLA3, varias veces en un mismo día).

El evento de historial de la corrida actual NO se persiste dentro de procesar() — recién se
aplica en registrar_evento_publicado(), llamado por telegram_publisher como callback una vez por
cada oferta que Telegram confirma que se mandó de verdad. Así, un producto sin canal activo (o
cuyo envío falló) no queda marcado como "ya publicado" sin que nadie lo haya visto — sigue
disponible para publicarse la próxima vez que corra el scraper. Y a diferencia de un post-proceso
al final de toda la corrida, cada evento queda durable en el instante en que se confirma el
envío — si la corrida se corta a mitad de camino (redeploy, crash), lo ya publicado no se pierde.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

import config
import db
from fuentes.falabella.parsing import calcular_descuento_pct  # noqa: F401 (reexportado para tests)

log = logging.getLogger("scraper.ofertas_writer")


def _id_estable(tienda_id: str, producto_id: str) -> str:
    return f"{tienda_id}_{producto_id}"


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StatsHistorial:
    precio_minimo: int
    fecha_precio_minimo: str
    descuento_max: int


def _stats_historial(historial: list[dict]) -> StatsHistorial:
    minimo = min(historial, key=lambda e: e["precio"])
    return StatsHistorial(
        precio_minimo=minimo["precio"],
        fecha_precio_minimo=minimo["fecha"],
        descuento_max=max(e["descuento_pct"] for e in historial),
    )


@dataclass(frozen=True)
class DecisionPublicacion:
    regla: str | None = None  # None | "regla_1" | "regla_2" | "regla_3"
    precio_minimo_anterior: int | None = None
    fecha_precio_minimo_anterior: str | None = None


def _horas_entre(fecha_iso_desde: str, fecha_iso_hasta: str) -> float:
    desde = datetime.strptime(fecha_iso_desde, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hasta = datetime.strptime(fecha_iso_hasta, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (hasta - desde).total_seconds() / 3600


def _evaluar_reglas(historial: list[dict], precio_actual: int, descuento_pct: int, ahora: str) -> DecisionPublicacion:
    """Función pura: decide si el estado actual amerita publicar, y por qué regla.
    No conoce Telegram ni de dónde viene el historial."""
    stats = _stats_historial(historial)
    ultimo = historial[-1]
    es_evento_nuevo = precio_actual != ultimo["precio"] or descuento_pct != ultimo["descuento_pct"]
    es_minimo_historico = precio_actual <= stats.precio_minimo
    es_descuento_max = descuento_pct >= stats.descuento_max

    if es_evento_nuevo and es_minimo_historico:
        return DecisionPublicacion(regla="regla_1")
    if es_evento_nuevo and es_descuento_max:
        return DecisionPublicacion(
            regla="regla_2",
            precio_minimo_anterior=stats.precio_minimo,
            fecha_precio_minimo_anterior=stats.fecha_precio_minimo,
        )

    sigue_siendo_record = es_minimo_historico or es_descuento_max
    if sigue_siendo_record and _horas_entre(ultimo["fecha"], ahora) >= config.HORAS_REPUBLICACION_REGLA3:
        if es_minimo_historico:
            return DecisionPublicacion(regla="regla_3")
        return DecisionPublicacion(
            regla="regla_3",
            precio_minimo_anterior=stats.precio_minimo,
            fecha_precio_minimo_anterior=stats.fecha_precio_minimo,
        )

    return DecisionPublicacion()


async def procesar(pool: asyncpg.Pool, items_detectados: list[dict], tienda: config.Tienda) -> list[dict]:
    """Actualiza el estado de `tienda` (tabla `productos`/`historial_precios` en Postgres) a
    partir de los items detectados en esta corrida. NO escribe ofertas.json — ver
    construir_feed_web(), que junta el estado de todas las tiendas en un solo feed (llamarla
    aparte, una vez por corrida).

    items_detectados: dicts con producto_id, titulo, marca, url, comercio, imagen,
    precio_actual, precio_normal, descuento_pct (de fuentes/<tienda>/...).

    Devuelve las ofertas que corresponde publicar en Telegram (ver Reglas 1/2/3 arriba).
    """
    ahora = _ahora_iso()

    # dedupe por producto_id: el mismo producto puede salir tanto del listado general
    # como de productos_seguidos.json en la misma corrida.
    por_id = {item["producto_id"]: item for item in items_detectados}
    claves = [_id_estable(tienda.id, pid) for pid in por_id]

    productos_estado = await db.cargar_productos_con_historial(pool, claves)

    ofertas_para_publicar = []
    registros_a_upsertear = []

    for producto_id, item in por_id.items():
        clave = _id_estable(tienda.id, producto_id)
        anterior = productos_estado.get(clave)

        historial = anterior["historial"] if anterior else []
        primera_deteccion = anterior["primera_deteccion"] if anterior else ahora

        if historial:
            decision = _evaluar_reglas(historial, item["precio_actual"], item["descuento_pct"], ahora)
        else:
            # producto 100% nuevo (o trackeado pero sin ninguna publicación confirmada todavía):
            # su primer precio ya "es" su mínimo histórico.
            decision = DecisionPublicacion(regla="regla_1")

        es_candidata = decision.regla is not None

        # el evento de esta corrida NO se persiste todavía (ver docstring del módulo) — solo se
        # arma en memoria para mostrarlo en el caption si es_candidata, y se guarda si/cuando
        # registrar_evento_publicado() confirme que el mensaje realmente se mandó.
        evento_nuevo = {
            "precio": item["precio_actual"],
            "descuento_pct": item["descuento_pct"],
            "fecha": ahora,
        }

        registro = {
            "id": clave,
            "tienda_id": tienda.id,
            "producto_id": producto_id,
            "url": item["url"],
            "titulo": item["titulo"],
            "marca": item.get("marca"),
            "imagen": item.get("imagen"),
            "precio_normal": item.get("precio_normal") or (anterior or {}).get("precio_normal"),
            "precio_actual": item["precio_actual"],
            "descuento_pct": item["descuento_pct"],
            "primera_deteccion": primera_deteccion,
            "ultima_actualizacion": ahora,
            "activo": True,
        }
        registros_a_upsertear.append(registro)

        if es_candidata:
            ofertas_para_publicar.append({
                "id": clave,
                "titulo": registro["titulo"],
                "url": registro["url"],
                "imagen": registro["imagen"],
                "precio_normal": registro["precio_normal"],
                "precio_actual": registro["precio_actual"],
                "descuento_pct": registro["descuento_pct"],
                "fecha": ahora,
                "regla": decision.regla,
                "precio_minimo_anterior": decision.precio_minimo_anterior,
                "fecha_precio_minimo_anterior": decision.fecha_precio_minimo_anterior,
                "historial": historial + [evento_nuevo],
                "canal": config.canal_para_descuento(registro["descuento_pct"]),
                "comercio": tienda.nombre,
            })

    await db.upsert_productos(pool, registros_a_upsertear)
    await db.marcar_inactivos(pool, tienda.id, claves)

    log.info(
        "%s: %s productos tocados. %s candidatas a publicar en Telegram.",
        tienda.nombre, len(registros_a_upsertear), len(ofertas_para_publicar),
    )

    return ofertas_para_publicar


async def construir_feed_web(repo_dir: Path, pool: asyncpg.Pool) -> None:
    """Junta el estado activo de TODAS las tiendas en un solo web/data/ofertas.json. Se llama
    una sola vez por corrida, después de procesar() todas las tiendas."""
    ahora = _ahora_iso()
    nombre_por_tienda_id = {tienda.id: tienda.nombre for tienda in config.TIENDAS}

    umbral_vip = config.TIERS_DESCUENTO[0][0]  # tramo más alto de la lista (mayor a menor) = VIP
    filas = await db.feed_activo(
        pool,
        config.DESCUENTO_MINIMO_WEB_PCT,
        umbral_vip,
        config.MAX_OFERTAS_WEB_TEASER - config.MAX_OFERTAS_VIP_WEB_TEASER,
        config.MAX_OFERTAS_VIP_WEB_TEASER,
    )
    feed = [{
        "id": fila["id"],
        "titulo": fila["titulo"],
        "descuento": f"{fila['descuento_pct']}%",
        "comercio": nombre_por_tienda_id[fila["tienda_id"]],
        "categoria": None,
        "cupon": None,
        "detalle": None,
        "url": fila["url"],
        "imagen": fila["imagen"],
        "fecha": fila["primera_deteccion"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "canal": config.canal_para_descuento(fila["descuento_pct"]),
    } for fila in filas]

    ruta_ofertas = repo_dir / config.RUTA_OFERTAS_JSON
    ruta_ofertas.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_ofertas, "w", encoding="utf-8") as archivo:
        json.dump({"actualizado": ahora, "ofertas": feed}, archivo, ensure_ascii=False, indent=2)

    log.info("ofertas.json: %s ofertas activas.", len(feed))


async def registrar_evento_publicado(pool: asyncpg.Pool, oferta: dict) -> None:
    """Callback para telegram_publisher.publicar_ofertas_nuevas(on_publicada=...) — se llama UNA
    vez por cada oferta que Telegram confirma que se mandó de verdad. Inserta su evento de
    historial de inmediato (ver docstring del módulo: esto es lo que hace que la corrida no
    pierda progreso si se corta a mitad de camino)."""
    await db.insertar_evento_historial(
        pool, oferta["id"], oferta["precio_actual"], oferta["descuento_pct"], oferta["fecha"],
    )
