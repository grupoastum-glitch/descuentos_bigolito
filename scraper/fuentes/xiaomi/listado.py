"""Scrapea la tienda oficial de Xiaomi Chile (mi.com/cl).

A diferencia de Falabella, el home (`/cl/store/`) es una SPA React sin datos embebidos — pero
las páginas de categoría (`/cl/product-list/<categoria>`) sí son server-rendered y traen
`window.__PRELOADED_STATE__` con el árbol de categorías (`cate_list`) y, para la categoría
pedida, sus productos (`product_list.dataProvider.data`). Mismo patrón que `__NEXT_DATA__` de
Falabella, blob distinto.

No hay un facet de "solo ofertas" como en Falabella: el descuento se calcula acá comparando
`price_min` (precio actual) contra `market_price_min` (precio de referencia) de cada `commodity`
(variante de color/almacenamiento — se trackea cada variante como una oferta independiente,
porque varias variantes de un mismo producto pueden compartir la misma `item_link`).

Tampoco hay paginación server-side que funcione (`?page=N` siempre devuelve la página 1) — en vez
de eso se cosechan las categorías hoja del árbol de navegación (embebido en el mismo JSON) y se
pide la página 1 de cada una, que en la práctica cubre casi todo el catálogo (chico: ~400
productos repartidos en ~100 categorías).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.xiaomi")

_HOME_URL = "https://www.mi.com/cl/product-list/"
_PRELOADED_STATE_RE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.S)


def _extraer_preloaded_state(html: str) -> dict:
    coincidencia = _PRELOADED_STATE_RE.search(html)
    if not coincidencia:
        raise ValueError("No se encontró window.__PRELOADED_STATE__ en la página")
    return json.loads(coincidencia.group(1))


def _categorias_hoja(cate_list: list[dict]) -> list[str]:
    hojas: list[str] = []

    def _recorrer(nodos: list[dict]) -> None:
        for nodo in nodos:
            hijos = nodo.get("children") or []
            if hijos:
                _recorrer(hijos)
            else:
                url = nodo.get("url_pc")
                if url:
                    hojas.append(url if url.endswith("/") else f"{url}/")

    _recorrer(cate_list)
    return hojas


def _items_desde_dataprovider(data: list[dict] | None) -> list[dict]:
    items = []
    for producto in data or []:  # una categoría sin productos trae "data": null, no []
        for commodity in producto.get("commodity", []):
            try:
                precio_actual = int(float(commodity["price_min"]))
                precio_normal = int(float(commodity["market_price_min"]))
            except (KeyError, TypeError, ValueError):
                continue
            if precio_normal <= 0:
                continue
            descuento_pct = round((1 - precio_actual / precio_normal) * 100)
            if descuento_pct <= 0:
                continue

            items.append({
                "producto_id": str(commodity["id"]),
                "titulo": commodity.get("name") or "",
                "marca": None,
                "url": commodity.get("item_link"),
                "comercio": "Xiaomi",
                "imagen": commodity.get("image"),
                "precio_actual": precio_actual,
                "precio_normal": precio_normal,
                "descuento_pct": descuento_pct,
            })
    return items


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, url: str) -> list[dict] | None:
    async with semaforo:
        try:
            respuesta = await sesion.get(url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {url}")
            html = respuesta.body.decode("utf-8", errors="replace")
            estado = _extraer_preloaded_state(html)
            data = estado["pagedata"]["product_list"]["dataProvider"]["data"]
            resultado = _items_desde_dataprovider(data)
        except Exception:
            log.exception("Falló %s del catálogo de Xiaomi, se omite", url)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
    return resultado


async def _fetch_con_reintento(sesion, url: str):
    """Un reintento (con una pausa corta) para el único fetch cuya falla aborta toda la
    tienda — un bloqueo/timeout transitorio no debería tirar la corrida completa (ver
    incidencia 2026-08-12: mismo punto único de falla en Ripley, que se recuperó solo minutos
    después sin ningún cambio de nuestro lado)."""
    for intento in range(2):
        try:
            respuesta = await sesion.get(url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {url}")
            return respuesta
        except Exception:
            if intento == 1:
                raise
            log.warning("Falló %s (intento 1/2), se reintenta en %ss", url, config.REINTENTO_FETCH_INICIAL_SEGUNDOS)
            await asyncio.sleep(config.REINTENTO_FETCH_INICIAL_SEGUNDOS)


async def obtener_ofertas_xiaomi(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, categorías leídas sin error — 0 significa que no se pudo sacar
    nada de esta fuente). Mismo contrato que fuentes.falabella.listado.obtener_ofertas_listado."""
    try:
        respuesta = await _fetch_con_reintento(sesion, _HOME_URL)
        html = respuesta.body.decode("utf-8", errors="replace")
        estado = _extraer_preloaded_state(html)
        categorias = _categorias_hoja(estado["pagedata"]["cate_list"])
    except Exception:
        log.exception("Falló la carga del árbol de categorías de Xiaomi (%s), se aborta", _HOME_URL)
        return [], 0

    log.info("Xiaomi: %s categorías hoja encontradas", len(categorias))
    if not categorias:
        log.error("Xiaomi: el árbol de categorías no tiene ninguna hoja utilizable")
        return [], 0

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: una excepción no prevista en una sola categoría (algo que el
    # try/except de _fetch_categoria no haya anticipado) no debe tumbar la corrida completa —
    # ver la incidencia del 2026-08-11 donde un `data: null` sin manejar canceló todo el proceso
    # antes de llegar a procesar Falabella o publicar en Telegram.
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, categoria_url) for categoria_url in categorias
    ), return_exceptions=True)

    items: list[dict] = []
    categorias_ok = 0
    for categoria_url, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en %s del catálogo de Xiaomi, se omite: %r", categoria_url, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        items.extend(resultado)

    log.info(
        "Xiaomi: %s ofertas crudas en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
