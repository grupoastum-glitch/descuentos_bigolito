"""Scrapea las ofertas de Sodimac Chile (sodimac.cl).

Sodimac corre sobre la misma plataforma que Falabella — confirmado en vivo: `atgBaseUrl`/
`catalystBaseUrl`/`baseUrl` en su propio `__NEXT_DATA__` apuntan los tres a
`https://www.falabella.com`, con `businessUnit: "falabella"` y `store: "so_com"` (es otro
"tenant" del mismo backend/frontend Next.js, no una plataforma distinta). Por eso reusa
`fuentes.falabella.parsing` sin cambios — mismo shape de `props.pageProps.results`
(`productId`/`displayName`/`url`/`brand`/`mediaUrls`/`prices`/`discountBadge`), confirmado
contra una categoría real (Baño: 10.356 productos, descuentos calculados correctos sin ajustar
nada de la lógica de Falabella).

A diferencia de Falabella, no se le encontró una página de ofertas/descuentos curada (bloqueado
en sesión anterior — rutas típicas devuelven 404 o redirigen a home). En cambio se recorre el
árbol real de categorías: la home (`https://www.sodimac.cl/sodimac-cl`) linkea cientos de URLs
`/lista/CATG<id>/<slug>` en su menú de navegación — se cosechan del HTML con regex (mismo
criterio que `fuentes.falabella.listado` con el hub de descuentos, sin necesidad de parsear el
árbol de menú a mano). Se muestrean hasta `_MAX_CATEGORIAS` al azar (recorrerlas todas — cientos
— sería excesivo), y de cada una: página 1 siempre (ancla, da `pagination.count` para saber
cuántas páginas tiene esa categoría) + `_PAGINAS_ALEATORIAS_POR_CATEGORIA` páginas al azar hasta
un techo — mismo patrón exacto que `fuentes.easy.listado` (page1-only se descartó a propósito:
repetiría siempre los mismos productos entre corridas; el fetch en sí es rápido, ~0.25s medido
contra el sitio real pese a que cada página pesa ~4.4MB, así que no hay ahorro real en pedir
menos páginas — lo que domina el tiempo de la corrida es la pausa anti-detección entre
requests, no el tamaño de cada página).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re

import config
from fuentes.falabella.parsing import calcular_descuento_pct, extraer_next_data, precios_desde_lista

log = logging.getLogger("scraper.fuentes.sodimac")

_HOME_URL = "https://www.sodimac.cl/sodimac-cl"
_CATEGORIA_RE = re.compile(r"/sodimac-cl/lista/([A-Za-z0-9]+)/([A-Za-z0-9%-]+)")

_PRODUCTOS_POR_PAGINA = 56  # observado contra el sitio real (pagination.totalPerPage)
_MAX_CATEGORIAS = 60  # techo de categorías muestreadas por corrida — mismo criterio que
# config.MAX_CATEGORIAS_DESCUENTOS (Falabella): recorrer las ~800 URLs de categoría encontradas
# en el menú sería excesivo, esto es una constante local (no en config.py) porque es específica
# de esta fuente.
_MAX_PAGINAS_POR_CATEGORIA = 10
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)


def _item_desde_producto(producto: dict) -> dict | None:
    precio_actual, precio_normal = precios_desde_lista(producto.get("prices", []))
    if precio_actual is None:
        return None
    badge = (producto.get("discountBadge") or {}).get("label")
    descuento_pct = calcular_descuento_pct(precio_actual, precio_normal, badge)
    if descuento_pct <= 0:
        return None

    return {
        "producto_id": str(producto["productId"]),
        "titulo": producto.get("displayName") or "",
        "marca": producto.get("brand"),
        "url": producto["url"],
        "comercio": "Sodimac",
        "imagen": (producto.get("mediaUrls") or [None])[0],
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


def _items_desde_lista(productos: list[dict]) -> list[dict]:
    items = []
    for producto in productos:
        item = _item_desde_producto(producto)
        if item:
            items.append(item)
    return items


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_HOME_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_HOME_URL}")
    html = respuesta.body.decode("utf-8", errors="replace")
    urls = {f"https://www.sodimac.cl{m.group(0)}" for m in _CATEGORIA_RE.finditer(html)}
    return sorted(urls)


async def _fetch_pagina_categoria(sesion, url_categoria: str, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, pagination.count — total real de productos de la
    categoría, usado para saber cuántas páginas hay)."""
    url = f"{url_categoria}?page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html = respuesta.body.decode("utf-8", errors="replace")
    data = extraer_next_data(html)
    pp = data["props"]["pageProps"]
    total = (pp.get("pagination") or {}).get("count", 0)
    return _items_desde_lista(pp.get("results") or []), total


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, url_categoria: str) -> list[dict] | None:
    async with semaforo:
        try:
            items, total = await _fetch_pagina_categoria(sesion, url_categoria, 1)
        except Exception:
            log.exception("Falló la página 1 de %s, se omite", url_categoria)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    total_paginas = math.ceil(total / _PRODUCTOS_POR_PAGINA) if total else 1
    techo = min(total_paginas, _MAX_PAGINAS_POR_CATEGORIA)
    candidatas = list(range(2, techo + 1))
    extra_paginas = random.sample(candidatas, min(_PAGINAS_ALEATORIAS_POR_CATEGORIA, len(candidatas)))

    for pagina in extra_paginas:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina_categoria(sesion, url_categoria, pagina)
                items.extend(extra_items)
            except Exception:
                log.exception("Falló la página %s de %s, se omite esa página", pagina, url_categoria)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_sodimac(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no
    se pudo sacar nada de esta fuente). Mismo contrato que
    fuentes.easy.listado.obtener_ofertas_easy."""
    try:
        categorias_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga del menú de categorías de Sodimac (%s), se aborta", _HOME_URL)
        return [], 0

    if not categorias_disponibles:
        log.error("Sodimac: el menú de navegación no tiene ninguna categoría /lista/ utilizable")
        return [], 0

    categorias = random.sample(categorias_disponibles, min(_MAX_CATEGORIAS, len(categorias_disponibles)))
    log.info("Sodimac: %s categorías disponibles, muestreando %s", len(categorias_disponibles), len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: una excepción no anticipada en una sola categoría no debe tumbar
    # la corrida completa (mismo criterio que fuentes.falabella.listado/fuentes.easy.listado).
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, url) for url in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for url, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de Sodimac, se omite: %r", url, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("Sodimac: no se detectó ninguna oferta en las %s categorías muestreadas", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "Sodimac: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
