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
from urllib.parse import quote

import asyncpg

import config
import db
from fuentes.falabella.parsing import calcular_descuento_pct  # noqa: F401 (reexportado para tests)

log = logging.getLogger("scraper.ofertas_writer")

_HISTORIAL_MAX_EN_OFERTA = 5  # tope de eventos de historial que se guardan en la oferta que va a
# cola_publicacion — el caption de Telegram solo muestra los últimos 3
# (telegram_publisher.MAX_EVENTOS_HISTORIAL_EN_CAPTION), guardar el historial completo del
# producto ahí era peso muerto que infló cola_publicacion a 115MB (sesión 2026-08-20). No afecta
# a historial_precios (la tabla real/permanente, sigue completa) — solo esta copia transitoria.


def afiliar_url(url: str, comercio: str) -> str:
    """Envuelve `url` con el deeplink de afiliado de Soicos configurado para `comercio` en
    config.SOICOS_DEEPLINK_TEMPLATES. Sin plantilla para esa tienda, devuelve `url` sin cambios
    (passthrough) — así una tienda sin programa aprobado en Soicos sigue publicando su URL
    nativa sin que nadie tenga que tocar código."""
    plantilla = config.SOICOS_DEEPLINK_TEMPLATES.get(comercio)
    if not plantilla or not url:
        return url
    return plantilla.format(url=quote(url, safe=""))


def _id_estable(tienda_id: str, producto_id: str) -> str:
    return f"{tienda_id}_{producto_id}"


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class StatsHistorial:
    precio_minimo: int
    fecha_precio_minimo: str
    id_precio_minimo: int
    descuento_max: int


def _stats_historial(historial: list[dict]) -> StatsHistorial:
    minimo = min(historial, key=lambda e: e["precio"])
    return StatsHistorial(
        precio_minimo=minimo["precio"],
        fecha_precio_minimo=minimo["fecha"],
        id_precio_minimo=minimo["id"],
        descuento_max=max(e["descuento_pct"] for e in historial),
    )


@dataclass(frozen=True)
class DecisionPublicacion:
    regla: str | None = None  # None | "regla_1" | "regla_2" | "regla_3"
    precio_minimo_anterior: int | None = None
    fecha_precio_minimo_anterior: str | None = None
    # Solo relevante para regla_3: True si la última fila de historial puede reusarse (UPDATE de
    # su fecha) en vez de insertar una fila nueva — ver _evaluar_reglas.
    puede_reusar_fila: bool = False


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
        # "ultimo" solo puede ser la fila de origen del mínimo histórico cuando es_minimo_historico
        # es True (si no, su precio es estrictamente mayor a stats.precio_minimo). Reusar esa fila
        # perdería la fecha real del récord (se usa en el caption de Regla 2/3 como
        # fecha_precio_minimo_anterior); de la 2da repetición en adelante ya es una fila duplicada
        # distinta de la de origen y se puede reusar sin perder nada.
        puede_reusar_fila = ultimo["id"] != stats.id_precio_minimo
        if es_minimo_historico:
            return DecisionPublicacion(regla="regla_3", puede_reusar_fila=puede_reusar_fila)
        return DecisionPublicacion(
            regla="regla_3",
            precio_minimo_anterior=stats.precio_minimo,
            fecha_precio_minimo_anterior=stats.fecha_precio_minimo,
            puede_reusar_fila=puede_reusar_fila,
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
    regla3_admitidas = 0  # ver config.MAX_REGLA3_POR_TIENDA_POR_CORRIDA más abajo

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
        if decision.regla == "regla_3":
            # techo por tienda por corrida — sin esto, el pool de productos "récord eterno"
            # crece sin límite a medida que se trackean más productos (ver
            # config.MAX_REGLA3_POR_TIENDA_POR_CORRIDA). Regla 1/2 (récords genuinos) nunca se
            # cortan acá. Lo descartado no se pierde: sigue siendo récord y vuelve a evaluarse
            # en la próxima corrida, mismo mecanismo que una candidata sin canal activo.
            if regla3_admitidas >= config.MAX_REGLA3_POR_TIENDA_POR_CORRIDA:
                es_candidata = False
            else:
                regla3_admitidas += 1

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
                "url": afiliar_url(registro["url"], tienda.nombre),
                "imagen": registro["imagen"],
                "precio_normal": registro["precio_normal"],
                "precio_actual": registro["precio_actual"],
                "descuento_pct": registro["descuento_pct"],
                "fecha": ahora,
                "regla": decision.regla,
                "precio_minimo_anterior": decision.precio_minimo_anterior,
                "fecha_precio_minimo_anterior": decision.fecha_precio_minimo_anterior,
                "historial": (historial + [evento_nuevo])[-_HISTORIAL_MAX_EN_OFERTA:],
                "canal": config.canal_para_oferta(tienda.id, registro["descuento_pct"]),
                "comercio": tienda.nombre,
                "ultimo_historial_id": historial[-1]["id"] if historial else None,
                "puede_reusar_fila": decision.puede_reusar_fila,
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
    feed = []
    for fila in filas:
        comercio = nombre_por_tienda_id[fila["tienda_id"]]
        feed.append({
            "id": fila["id"],
            "titulo": fila["titulo"],
            "descuento": f"{fila['descuento_pct']}%",
            "comercio": comercio,
            "categoria": None,
            "cupon": None,
            "detalle": None,
            "url": afiliar_url(fila["url"], comercio),
            "imagen": fila["imagen"],
            "fecha": fila["primera_deteccion"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "canal": config.canal_para_oferta(fila["tienda_id"], fila["descuento_pct"]),
        })

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
    if oferta["puede_reusar_fila"] and oferta["ultimo_historial_id"] is not None:
        await db.actualizar_fecha_evento_historial(
            pool, oferta["ultimo_historial_id"], oferta["fecha"],
        )
    else:
        await db.insertar_evento_historial(
            pool, oferta["id"], oferta["precio_actual"], oferta["descuento_pct"], oferta["fecha"],
        )
