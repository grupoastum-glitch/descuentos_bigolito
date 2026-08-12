"""Fusiona las fuentes de datos, decide qué se publica y escribe el estado por tienda (ver
config.TIENDAS) + el feed combinado web/data/ofertas.json (ver construir_feed_web).

Modelo de publicación (reemplaza "cualquier baja de precio"): un producto solo se publica en
Telegram cuando es un récord real —

- Regla 1: el precio actual es el más bajo jamás registrado para ese producto.
- Regla 2: el % de descuento es el más alto visto, aunque el precio no sea el mínimo histórico.
- Regla 3: pasaron DIAS_REPUBLICACION_REGLA3 días desde la última publicación y el producto
  sigue siendo récord (de precio o de descuento) sin haber cambiado desde entonces — evita que
  una oferta buena quede "enterrada" para suscriptores nuevos.

`historial` guarda un evento por cada publicación real (no cada scrape), usado para derivar el
mínimo histórico y el descuento máximo. Máximo una publicación por producto por día calendario.

El evento de historial de la corrida actual NO se persiste dentro de procesar() — recién se
aplica en confirmar_publicaciones(), después de que telegram_publisher confirma cuáles ofertas
realmente se mandaron. Así, un producto sin canal activo (o cuyo envío falló) no queda marcado
como "ya publicado" sin que nadie lo haya visto — sigue disponible para publicarse la próxima
vez que corra el scraper.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config
from fuentes.falabella.parsing import calcular_descuento_pct  # noqa: F401 (reexportado para tests)

log = logging.getLogger("scraper.ofertas_writer")


def _id_estable(tienda_id: str, producto_id: str) -> str:
    return f"{tienda_id}_{producto_id}"


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cargar_json(ruta: Path, valor_por_defecto):
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return valor_por_defecto


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


def _dias_entre(fecha_iso_desde: str, fecha_iso_hasta: str) -> int:
    desde = datetime.strptime(fecha_iso_desde, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hasta = datetime.strptime(fecha_iso_hasta, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (hasta - desde).days


def _evaluar_reglas(historial: list[dict], precio_actual: int, descuento_pct: int, ahora: str) -> DecisionPublicacion:
    """Función pura: decide si el estado actual amerita publicar, y por qué regla.
    No conoce Telegram ni el dedup por día calendario (ver _ya_publicado_hoy)."""
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
    if sigue_siendo_record and _dias_entre(ultimo["fecha"], ahora) >= config.DIAS_REPUBLICACION_REGLA3:
        if es_minimo_historico:
            return DecisionPublicacion(regla="regla_3")
        return DecisionPublicacion(
            regla="regla_3",
            precio_minimo_anterior=stats.precio_minimo,
            fecha_precio_minimo_anterior=stats.fecha_precio_minimo,
        )

    return DecisionPublicacion()


def _ya_publicado_hoy(historial: list[dict], ahora: str) -> bool:
    # todas las fechas del sistema son UTC vía _ahora_iso() con el mismo formato fijo —
    # comparar el substring de fecha (los primeros 10 caracteres) es seguro, sin parsear.
    return bool(historial) and historial[-1]["fecha"][:10] == ahora[:10]


def procesar(repo_dir: Path, items_detectados: list[dict], tienda: config.Tienda) -> list[dict]:
    """Actualiza el estado de `tienda` (scraper/estado_precios_<tienda>.json) a partir de los
    items detectados en esta corrida. NO escribe ofertas.json — ver construir_feed_web(), que
    junta el estado de todas las tiendas en un solo feed (llamarla aparte, una vez por corrida).

    items_detectados: dicts con producto_id, titulo, marca, url, comercio, imagen,
    precio_actual, precio_normal, descuento_pct (de fuentes/<tienda>/...).

    Devuelve las ofertas que corresponde publicar en Telegram (ver Reglas 1/2/3 arriba).
    """
    ruta_estado = repo_dir / tienda.ruta_estado

    estado = _cargar_json(ruta_estado, {"productos": {}})
    productos_estado = estado.setdefault("productos", {})

    ahora = _ahora_iso()
    ofertas_para_publicar = []

    # dedupe por producto_id: el mismo producto puede salir tanto del listado general
    # como de productos_seguidos.json en la misma corrida.
    por_id = {item["producto_id"]: item for item in items_detectados}

    for producto_id, item in por_id.items():
        clave = _id_estable(tienda.id, producto_id)
        anterior = productos_estado.get(clave)

        # migración: productos guardados con el modelo viejo no tienen la CLAVE historial (a
        # diferencia de un producto nuevo del modelo actual, que sí la tiene pero puede estar
        # vacía mientras su primera publicación no se confirma — ver docstring del módulo, no
        # tratar ambos casos igual). Se les siembra un evento, fechado HOY (no con la fecha real
        # vieja) para que Regla 3 no dispare de inmediato para toda la base el día del deploy.
        if anterior and "historial" not in anterior:
            anterior = dict(anterior)
            anterior["historial"] = [{
                "precio": anterior["precio_actual"],
                "descuento_pct": anterior["descuento_pct"],
                "fecha": ahora,
            }]

        historial = list(anterior["historial"]) if anterior else []
        primera_deteccion = anterior["primera_deteccion"] if anterior else ahora

        if historial:
            decision = _evaluar_reglas(historial, item["precio_actual"], item["descuento_pct"], ahora)
        else:
            # producto 100% nuevo: su primer precio ya "es" su mínimo histórico.
            decision = DecisionPublicacion(regla="regla_1")

        es_candidata = decision.regla is not None and not _ya_publicado_hoy(historial, ahora)

        # el evento de esta corrida NO se persiste todavía (ver docstring del módulo) — solo
        # se arma en memoria para mostrarlo en el caption si es_candidata, y se guarda si/cuando
        # confirmar_publicaciones() confirme que el mensaje realmente se mandó.
        evento_nuevo = {
            "precio": item["precio_actual"],
            "descuento_pct": item["descuento_pct"],
            "fecha": ahora,
        }

        registro = {
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
            "historial": historial,
        }
        productos_estado[clave] = registro

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

    # un producto que ya no aparece en esta corrida se marca inactivo (dejó de estar en oferta)
    ids_vistos_hoy = {_id_estable(tienda.id, pid) for pid in por_id}
    for clave, registro in productos_estado.items():
        if clave not in ids_vistos_hoy:
            registro["activo"] = False

    ruta_estado.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_estado, "w", encoding="utf-8") as archivo:
        json.dump(estado, archivo, ensure_ascii=False, indent=2)

    log.info(
        "%s: %s productos en estado. %s candidatas a publicar en Telegram.",
        tienda.nombre, len(productos_estado), len(ofertas_para_publicar),
    )

    return ofertas_para_publicar


def construir_feed_web(repo_dir: Path, tiendas: list[config.Tienda]) -> None:
    """Junta el estado de TODAS las tiendas en un solo web/data/ofertas.json. Se llama una sola
    vez por corrida, después de procesar() todas las tiendas — si cada tienda escribiera el feed
    completo por su cuenta, la última en correr pisaría los productos de las demás."""
    ahora = _ahora_iso()
    feed = []

    for tienda in tiendas:
        ruta_estado = repo_dir / tienda.ruta_estado
        estado = _cargar_json(ruta_estado, {"productos": {}})
        for clave, registro in estado.get("productos", {}).items():
            if not registro.get("activo"):
                continue
            if registro["descuento_pct"] < config.DESCUENTO_MINIMO_WEB_PCT:
                continue
            feed.append({
                "id": clave,
                "titulo": registro["titulo"],
                "descuento": f"{registro['descuento_pct']}%",
                "comercio": tienda.nombre,
                "categoria": None,
                "cupon": None,
                "detalle": None,
                "url": registro["url"],
                "imagen": registro.get("imagen"),
                "fecha": registro["primera_deteccion"],
                "canal": config.canal_para_descuento(registro["descuento_pct"]),
            })

    feed.sort(key=lambda oferta: oferta["fecha"], reverse=True)
    feed = feed[: config.MAX_OFERTAS_WEB_TEASER]

    ruta_ofertas = repo_dir / config.RUTA_OFERTAS_JSON
    ruta_ofertas.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_ofertas, "w", encoding="utf-8") as archivo:
        json.dump({"actualizado": ahora, "ofertas": feed}, archivo, ensure_ascii=False, indent=2)

    log.info("ofertas.json: %s ofertas activas de %s tienda(s).", len(feed), len(tiendas))


def confirmar_publicaciones(
    repo_dir: Path, candidatas: list[dict], ids_confirmados: set[str], tienda: config.Tienda
) -> None:
    """Aplica el evento de historial SOLO para las candidatas de `procesar()` cuyo `id` está en
    `ids_confirmados` (las que telegram_publisher.publicar_ofertas_nuevas confirma que
    realmente se mandaron). Llamar después de intentar el envío, con su resultado.

    Las candidatas que no se confirman (sin canal activo, o cuyo envío falló del todo) quedan
    sin tocar — su historial no cambia, así que siguen disponibles para publicarse en una
    corrida futura en vez de quedar marcadas como "ya publicadas" sin que nadie las haya visto.
    """
    if not ids_confirmados:
        return

    ruta_estado = repo_dir / tienda.ruta_estado
    estado = _cargar_json(ruta_estado, {"productos": {}})
    productos_estado = estado.setdefault("productos", {})

    confirmadas_de_esta_tienda = 0
    for oferta in candidatas:
        if oferta["id"] not in ids_confirmados:
            continue
        confirmadas_de_esta_tienda += 1
        registro = productos_estado.get(oferta["id"])
        if registro is None:
            continue  # no debería pasar — defensivo, ej. si el registro se borró entre medio
        registro["historial"] = registro["historial"] + [{
            "precio": oferta["precio_actual"],
            "descuento_pct": oferta["descuento_pct"],
            "fecha": oferta["fecha"],
        }]

    ruta_estado.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_estado, "w", encoding="utf-8") as archivo:
        json.dump(estado, archivo, ensure_ascii=False, indent=2)

    # ids_confirmados llega compartido entre todas las tiendas (una sola llamada a
    # publicar_ofertas_nuevas para todas juntas) — contar cuántas de LAS CANDIDATAS DE ESTA
    # TIENDA quedaron confirmadas, no el tamaño del set combinado (ver incidencia 2026-08-11:
    # "Falabella: 94/88" era el conteo global de ambas tiendas, no el de Falabella sola).
    log.info("%s: historial confirmado para %s/%s candidatas publicadas en Telegram.",
              tienda.nombre, confirmadas_de_esta_tienda, len(candidatas))
