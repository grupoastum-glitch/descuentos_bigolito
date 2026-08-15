"""Scrapea las ofertas de WePlay (weplay.cl).

Es Magento 2 (tema Smartwave/weplay) detrás de Cloudflare — un curl plano recibe un challenge
(403), pero FetcherSession(impersonate="chrome") pasa sin problema, igual que con Falabella.

A diferencia de LuffyToys/Geekz, WePlay no es una tienda 100% geek: vende videojuegos/consolas/
Funko/coleccionables, pero también tecnología genérica, computación y ropa. Por eso acá no se
recorre el catálogo completo (9528 productos) sino solo las categorías geek confirmadas
(_CATEGORIAS_GEEK abajo) — mantiene la identidad del canal Geek VIP en vez de diluirla con
audífonos o accesorios de computación genéricos (ver Tiendas/paginas.md).

WePlay expone una API GraphQL pública en /graphql, más confiable que scrapear HTML: da precio
regular/final, stock e imagen ya estructurados. Existe una categoría "OFERTA" (id 202) pero no
sirve como fuente única — de una muestra de 100 productos ahí, solo 10 tenían price/final_price
distintos de verdad, y los productos en esa categoría no traen su categoría "real" (Funko,
Coleccionables, etc.) en la respuesta. En cambio, se recorre cada categoría geek por separado y
se calcula el descuento comparando regular_price contra final_price de cada producto — mismo
criterio que el resto de las fuentes: no confiar en ningún campo de "% off" ni categoría propia
del sitio.

Hay preventas (precio de reserva sobre stock que no existe todavía, mismo problema que LuffyToys):
existe una categoría "PREVENTAS" chica, pero el título del producto siempre trae el prefijo
literal "PREVENTA " incluso cuando aparece en otras categorías — se descarta por ese prefijo, más
simple y más confiable que andar cruzando categorías.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import urllib.parse

import config

log = logging.getLogger("scraper.fuentes.weplay")

_BASE_URL = "https://www.weplay.cl"
_GRAPHQL_URL = f"{_BASE_URL}/graphql"
_PAGE_SIZE = 100

# (id, nombre) de cada categoría geek confirmada — Funko, Coleccionables, Juegos (videojuegos
# físicos), Juegos de mesa y cartas, Accesorios (verificado: gaming, no genéricos), Consolas,
# Lego, Arcade, Editorial. Deja afuera Tecnología/Computación/Ropa (genéricas) y Preventas
# (excluida aparte, ver arriba). Sumar una categoría nueva es agregar una línea acá.
_CATEGORIAS_GEEK = ["221", "116", "3", "132", "19", "35", "146", "189", "138"]

_QUERY_TEMPLATE = """
{
  products(filter: {category_id: {eq: "%s"}}, pageSize: %d, currentPage: %d) {
    page_info { total_pages }
    items {
      id
      name
      url_key
      stock_status
      image { url }
      price_range {
        minimum_price {
          regular_price { value }
          final_price { value }
        }
      }
    }
  }
}
"""


def _items_desde_productos(productos: list[dict]) -> list[dict]:
    items = []
    for p in productos:
        titulo = html.unescape(p["name"])
        if titulo.strip().upper().startswith("PREVENTA"):
            continue
        if p.get("stock_status") != "IN_STOCK":
            continue

        precios = p["price_range"]["minimum_price"]
        precio_normal = round(precios["regular_price"]["value"])
        precio_actual = round(precios["final_price"]["value"])
        if precio_normal <= precio_actual:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        imagen = (p.get("image") or {}).get("url")
        items.append({
            "producto_id": str(p["id"]),
            "titulo": titulo,
            "marca": None,  # no viene en esta consulta
            "url": f"{_BASE_URL}/{p['url_key']}.html",
            "comercio": "WePlay",
            "imagen": imagen,
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _fetch_pagina(sesion, categoria_id: str, pagina: int) -> dict:
    query = _QUERY_TEMPLATE % (categoria_id, _PAGE_SIZE, pagina)
    url = f"{_GRAPHQL_URL}?query={urllib.parse.quote(query)}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en categoría {categoria_id} página {pagina}")
    body = respuesta.body if isinstance(respuesta.body, str) else respuesta.body.decode("utf-8", errors="replace")
    datos = json.loads(body)
    if "errors" in datos:
        raise RuntimeError(f"GraphQL error en categoría {categoria_id} página {pagina}: {datos['errors']}")
    return datos["data"]["products"]


async def _crawl_categoria(sesion, semaforo: asyncio.Semaphore, categoria_id: str) -> list[dict] | None:
    """Devuelve la lista de ofertas de una categoría, o None si ni siquiera su primera página
    se pudo leer (mismo patrón que fuentes.paris.listado._fetch_hoja)."""
    async with semaforo:
        items: list[dict] = []
        pagina = 1
        total_paginas = None

        while total_paginas is None or pagina <= total_paginas:
            try:
                resultado = await _fetch_pagina(sesion, categoria_id, pagina)
            except Exception:
                log.exception("Falló la categoría %s de WePlay en la página %s", categoria_id, pagina)
                if pagina == 1:
                    return None
                break  # se queda con lo ya leído en páginas anteriores

            if total_paginas is None:
                total_paginas = resultado["page_info"]["total_pages"]
            items.extend(_items_desde_productos(resultado["items"]))

            pagina += 1
            if total_paginas is None or pagina <= total_paginas:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

        return items


async def obtener_ofertas_weplay(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, categorías leídas sin error — 0 significa que no se pudo sacar
    nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _crawl_categoria(sesion, semaforo, cid) for cid in _CATEGORIAS_GEEK
    ), return_exceptions=True)

    vistos: set[str] = set()
    items: list[dict] = []
    categorias_ok = 0
    for categoria_id, resultado in zip(_CATEGORIAS_GEEK, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de WePlay, se omite: %r", categoria_id, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        for item in resultado:
            if item["producto_id"] in vistos:
                continue  # un producto puede estar en más de una categoría geek
            vistos.add(item["producto_id"])
            items.append(item)

    if categorias_ok == 0:
        log.error("WePlay: no se pudo leer ninguna categoría geek")
        return [], 0

    log.info(
        "WePlay: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(_CATEGORIAS_GEEK),
    )
    return items, categorias_ok
