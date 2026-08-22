"""Scrapea las ofertas de Sipoonline (sipoonline.cl) — octava tienda del rubro Tech.

Jumpseller (confirmado por el header `content-security-policy: frame-ancestors ...
https://*.jumpseller.com` y cientos de menciones de "jumpseller" en el HTML) — primera tienda del
proyecto sobre esta plataforma. Sin endpoint `/products.json` (probado, 404) ni página de
ofertas/liquidación curada.

A diferencia de PC Express/MyShop/PCFactory/Sodimac, acá **no hace falta cosechar ni muestrear el
árbol de categorías**: las categorías raíz ya incluyen transitivamente todos los productos de sus
subcategorías (confirmado en vivo: los 21 productos de `/componentes-pc/tarjeta-de-video` están
completos dentro de los 129 de `/componentes-pc`, recorriendo sus 4 páginas). Alcanza con pedir
las 9 categorías raíz reales cada corrida (`_CATEGORIAS`, mismo patrón de lista fija que
`fuentes.spdigital`) — el menú linkea a slugs "de marketing" para SEO (ej.
`/componentes-pc-chile-todo-para-armar-tu-pc-sipo`), pero el slug plano que usan las
subcategorías como prefijo (`/componentes-pc`) también resuelve y es la página de listado real.

Paginación por `?page=N`, 40 productos fijos por página. El total real sale del texto
"1-40 de 129 productos" en `.theme-filters__count` — `total_paginas = ceil(total/40)` (a
diferencia de PC Express, que exponía el total de páginas directo). Mismo muestreo ancla+aleatorio
de siempre: página 1 (ancla) + 2 al azar por categoría, hasta un techo de 10.

Cada tarjeta (`<article class="product-block" data-product-id="N">`) trae un solo par de precios,
sin la complicación de medios de pago de PC Express/SPDigital/MyShop: `.sipo-price-old` (normal) y
`.sipo-price-new` (actual) — aparece en prácticamente todos los productos observados (parece ser
el precio "de referencia" que la tienda muestra siempre, no una señal exclusiva de oferta puntual),
así que se recalcula el % siempre y se filtra por el piso del canal, sin necesidad de detectar
"está en oferta" aparte. `producto_id`, título+url, marca e imagen vienen todos en la tarjeta del
listado, sin visitar la ficha de producto.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.sipoonline")

_BASE_URL = "https://sipoonline.cl"

_CATEGORIAS = [
    "componentes-pc", "audio-y-musica", "monitores", "pc-armadas",
    "equipos-ia-y-servidores", "computacion-y-gamers", "redes-y-conectividad",
    "marcas", "otras-categorias",
]

_PRODUCTOS_POR_PAGINA = 40  # observado, confirmado contra el sitio real
_MAX_PAGINAS_POR_CATEGORIA = 10
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)

_TOTAL_PRODUCTOS_RE = re.compile(r"de\s+([\d.]+)\s+productos?", re.I)
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _item_desde_card(card) -> dict | None:
    producto_id = card.attrib.get("data-product-id")
    if not producto_id:
        return None

    viejo = card.css(".sipo-price-old")
    nuevo = card.css(".sipo-price-new")
    precio_normal = _parse_precio(viejo[0].get_all_text(strip=True)) if viejo else None
    precio_actual = _parse_precio(nuevo[0].get_all_text(strip=True)) if nuevo else None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    nombre_nodo = card.css(".product-block__name")
    if not nombre_nodo:
        return None
    titulo = nombre_nodo[0].get_all_text(strip=True)
    url = nombre_nodo[0].attrib.get("href")
    if not url:
        return None

    marca_nodo = card.css(".product-block__brand")
    marca = (marca_nodo[0].get_all_text(strip=True) or None) if marca_nodo else None
    imagen_nodo = card.css(".product-block__image")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": marca,
        "url": f"{_BASE_URL}{url}",
        "comercio": "Sipoonline",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina_categoria(sesion, slug: str, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de productos real de la categoría — usado para saber
    cuántas páginas hay)."""
    url = f"{_BASE_URL}/{slug}?page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css("article.product-block"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    total = 0
    contador = respuesta.css(".theme-filters__count")
    if contador:
        m = _TOTAL_PRODUCTOS_RE.search(contador[0].get_all_text(strip=True))
        if m:
            total = int(m.group(1).replace(".", ""))
    return items, total


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, slug: str) -> list[dict] | None:
    async with semaforo:
        try:
            items, total = await _fetch_pagina_categoria(sesion, slug, 1)
        except Exception:
            log.exception("Falló la página 1 de la categoría %s de Sipoonline, se omite", slug)
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
                log.exception("Falló la página %s de la categoría %s de Sipoonline, se omite esa página", pagina, slug)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_sipoonline(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, slug) for slug in _CATEGORIAS
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for slug, resultado in zip(_CATEGORIAS, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de Sipoonline, se omite: %r", slug, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("Sipoonline: no se detectó ninguna oferta en las %s categorías", len(_CATEGORIAS))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "Sipoonline: %s ofertas crudas (sin duplicados) desde %s/%s categorías",
        len(items), categorias_ok, len(_CATEGORIAS),
    )
    return items, categorias_ok
