"""Scrapea las ofertas de Easy Chile (easy.cl) — primera tienda del rubro Ferretería.

Easy es Next.js Pages Router (un único bloque `__NEXT_DATA__` por página, ver
`fuentes.falabella.parsing`, no el RSC fragmentado de Paris) sobre VTEX (confirmado: dominio de
imágenes `easycl.vteximg.com.br`, nomenclatura `linkText`/`productId`/`sku`).

Dos fuentes combinadas:

1. `https://www.easy.cl/ofertas` (encontrada vía su `sitemap.xml` de landings, no linkeada en el
   menú principal — mismo caso que la página de descuentos de Falabella): trae ~40 productos
   curados por el propio sitio en `props.pageProps.dehydratedState.queries` (caché de
   react-query serializada en el HTML). Barata (1 fetch) pero fija — `?page=N` no pagina de
   verdad acá (mismo caso que `/outlet/` de Paris).
2. `https://www.easy.cl/search/<categoria>?page=N`: el árbol real de categorías del sitio (18
   grupos, ver `_CATEGORIAS`), con paginación de verdad (confirmado: page 1 y page 2 no
   comparten productos) y miles de productos por categoría — demasiado para recorrer entera cada
   corrida. Se muestrea: página 1 siempre (ancla — de paso da el total real en
   `serverProductsResponse.recordsFiltered`) más `_PAGINAS_ALEATORIAS_POR_CATEGORIA` páginas al
   azar entre 2 y `_MAX_PAGINAS_POR_CATEGORIA`, mismo patrón que ya usa Falabella en modo hub
   (ver `descubrimiento`/`config.PAGINAS_ALEATORIAS_POR_CATEGORIA`) para no bajar el catálogo
   completo en cada corrida.

Ambas fuentes devuelven productos con la misma forma (`sku`/`productName`/`linkText`/
`imageUrl`/`prices.normalPrice`/`prices.offerPrice`) pese a venir de bloques distintos del JSON
(`dehydratedState` vs `serverProductsResponse`) — se combinan y deduplican por `sku`.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random

import config
from fuentes.falabella.parsing import extraer_next_data

log = logging.getLogger("scraper.fuentes.easy")

_OFERTAS_URL = "https://www.easy.cl/ofertas"

# grupos de categoría reales del sitio, confirmados vía sitemap.xml (excluye sitemaps que no son
# de catálogo: cliente-easy-pro, servicios, blog, custom, filters, landings, home).
_CATEGORIAS = [
    "electrohogar-y-climatizacion", "muebles", "proyectos-organizacion", "jardin-y-aire-libre",
    "dormitorio", "organizacion-y-limpieza", "decoracion-e-iluminacion", "cocina-y-comedor",
    "bano", "mascotas", "puertas-y-ventanas", "pisos", "pinturas", "herramientas", "automovil",
    "electricidad-y-seguridad", "ferreteria-y-gasfiteria", "materiales-de-construccion",
    "salud-y-hogar",
]

_PRODUCTOS_POR_PAGINA = 40  # observado, confirmado contra el sitio real
_MAX_PAGINAS_POR_CATEGORIA = 10  # techo de páginas consideradas para el sorteo, por categoría
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)


def _item_desde_producto(producto: dict) -> dict | None:
    prices = producto.get("prices") or {}
    precio_actual = prices.get("offerPrice")
    precio_normal = prices.get("normalPrice")
    if not precio_actual or not precio_normal:
        return None  # sin offerPrice no es una oferta real, solo precio de lista
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    link_text = producto.get("linkText")
    sku = producto.get("sku")
    if not link_text or not sku:
        return None

    return {
        "producto_id": sku,
        "titulo": producto.get("productName"),
        "marca": producto.get("brand"),
        "url": f"https://www.easy.cl/{link_text}",
        "comercio": "Easy",
        "imagen": producto.get("imageUrl"),
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


async def _fetch_ofertas(sesion) -> list[dict]:
    respuesta = await sesion.get(_OFERTAS_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_OFERTAS_URL}")
    html = respuesta.body.decode("utf-8", errors="replace")
    data = extraer_next_data(html)
    queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    productos = next(
        (q["state"]["data"] for q in queries if q.get("queryKey", [None])[0] == "products"),
        None,
    )
    return _items_desde_lista(productos or [])


async def _fetch_pagina_categoria(sesion, slug: str, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, recordsFiltered — total real de productos de la
    categoría, usado para saber cuántas páginas hay)."""
    url = f"https://www.easy.cl/search/{slug}?page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html = respuesta.body.decode("utf-8", errors="replace")
    data = extraer_next_data(html)
    spr = data["props"]["pageProps"].get("serverProductsResponse") or {}
    return _items_desde_lista(spr.get("productList") or []), spr.get("recordsFiltered", 0)


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, slug: str) -> list[dict] | None:
    async with semaforo:
        try:
            items, total = await _fetch_pagina_categoria(sesion, slug, 1)
        except Exception:
            log.exception("Falló la página 1 de la categoría %s de Easy, se omite", slug)
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
                extra_items, _ = await _fetch_pagina_categoria(sesion, slug, pagina)
                items.extend(extra_items)
            except Exception:
                log.exception("Falló la página %s de la categoría %s de Easy, se omite esa página", pagina, slug)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_easy(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, fuentes leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que
    fuentes.paris.listado.obtener_ofertas_paris."""
    try:
        items_ofertas = await _fetch_ofertas(sesion)
    except Exception:
        log.exception("Falló la carga de %s, se continúa solo con categorías", _OFERTAS_URL)
        items_ofertas = []

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: una excepción no anticipada en una sola categoría no debe tumbar
    # la corrida completa (mismo criterio que fuentes.paris.listado).
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, slug) for slug in _CATEGORIAS
    ), return_exceptions=True)

    categorias_ok = 0
    todos = list(items_ofertas)
    for slug, resultado in zip(_CATEGORIAS, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de Easy, se omite: %r", slug, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("Easy: no se detectó ninguna oferta (ni en /ofertas ni en las %s categorías)", len(_CATEGORIAS))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    fuentes_ok = categorias_ok + (1 if items_ofertas else 0)
    log.info(
        "Easy: %s ofertas crudas (sin duplicados) desde %s/%s categorías + /ofertas (%s items)",
        len(items), categorias_ok, len(_CATEGORIAS), len(items_ofertas),
    )
    return items, fuentes_ok
