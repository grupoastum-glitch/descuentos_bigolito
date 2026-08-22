"""Scrapea las ofertas de Sparta (sparta.cl) — segunda tienda del rubro Fitness.

Magento/Adobe Commerce (confirmado por el script `text/x-magento-init` con
`Magento_PageCache/js/form-key-provider` y el script de Adobe Helix RUM) — primera tienda del
proyecto sobre esta plataforma. Sin Cloudflare, responde 200 con fetch plano.

Distribuidor de Trek (bicicletas) y tienda de deportes en general — todo su árbol de categorías
(ciclismo, running, fitness, deportes de equipo/raqueta, outdoor, zapatillas, etc.) encaja con el
canal, así que a diferencia de GymPro acá no hace falta filtrar por categoría.

Se usa `/sale.html/`, la página de ofertas curada del propio sitio: **2572 productos, 36 por
página, ~72 páginas** (total real sale del texto "Artículos 1-36 de 2572" en `.toolbar-amount`).
Decisión del usuario: sin muestreo, se leen las 72 páginas completas cada corrida (a diferencia
del resto de fuentes de catálogo grande, que sortean páginas/categorías).

Cada tarjeta (`li.product-item`, dentro de `ol.products.list.items.product-items` — hay que
acotar a ese contenedor porque la página trae además un carrusel de productos relacionados que
también matchea `li.product-item` suelto) trae precio actual y precio normal ya numéricos y
limpios en atributos `data-price-amount` (nada que parsear, poco común en el resto de fuentes):
`#old-price-<id>[data-price-type="oldPrice"]` = normal, `#product-price-<id>
[data-price-type="finalPrice"]` = actual. Solo aparece el nodo `old-price` si el producto está
realmente en oferta (mismo criterio de detección que Decathlon/SuperZoo).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.sparta")

_BASE_URL = "https://sparta.cl"
_SALE_URL = f"{_BASE_URL}/sale.html/"

_PRODUCTOS_POR_PAGINA = 36  # confirmado en vivo, tamaño de página por defecto del sitio

_TOTAL_RE = re.compile(
    r'toolbar-amount"[^>]*>.*?de\s*<span class="toolbar-number">([\d.,]+)</span>', re.S,
)
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _item_desde_card(card) -> dict | None:
    box_nodo = card.css(".price-box[data-product-id]")
    if not box_nodo:
        return None
    producto_id = box_nodo[0].attrib.get("data-product-id")
    if not producto_id:
        return None

    viejo_nodo = card.css('[data-price-type="oldPrice"]')
    if not viejo_nodo:
        return None  # sin precio "Precio habitual" tachado, este producto no está en oferta
    precio_normal = _parse_precio(viejo_nodo[0].attrib.get("data-price-amount"))

    nuevo_nodo = card.css('[data-price-type="finalPrice"]')
    precio_actual = _parse_precio(nuevo_nodo[0].attrib.get("data-price-amount")) if nuevo_nodo else None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    link_nodo = card.css(".product-item-link")
    if not link_nodo:
        return None
    titulo = link_nodo[0].get_all_text(strip=True)
    url = link_nodo[0].attrib.get("href")
    if not url:
        return None

    marca_nodo = card.css(".sp-product-brand")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css("img.product-item-photo")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": marca,
        "url": url,
        "comercio": "Sparta",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de páginas real de la fuente — se deriva del total
    de productos del texto "Artículos X-Y de Z")."""
    url = _SALE_URL if pagina == 1 else f"{_SALE_URL}?p={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    grid_nodo = respuesta.css("ol.products.list.items.product-items")
    items = []
    if grid_nodo:
        for card in grid_nodo[0].css("li.product-item"):
            item = _item_desde_card(card)
            if item:
                items.append(item)

    html_ = respuesta.body.decode("utf-8", errors="replace")
    m = _TOTAL_RE.search(html_)
    total_productos = int(m.group(1).replace(".", "").replace(",", "")) if m else 0
    total_paginas = math.ceil(total_productos / _PRODUCTOS_POR_PAGINA) if total_productos else 1
    return items, total_paginas


async def obtener_ofertas_sparta(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, páginas leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina(sesion, 1)
        except Exception:
            log.exception("Falló la página 1 de las ofertas de Sparta, se aborta")
            return [], 0
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    paginas_ok = 1
    todos: list[dict] = list(items)

    async def _fetch_extra(pagina: int) -> list[dict] | None:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina(sesion, pagina)
                return extra_items
            except Exception:
                log.exception("Falló la página %s de las ofertas de Sparta, se omite esa página", pagina)
                return None
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if total_paginas > 1:
        resultados = await asyncio.gather(*(
            _fetch_extra(pagina) for pagina in range(2, total_paginas + 1)
        ), return_exceptions=True)
        for pagina, resultado in zip(range(2, total_paginas + 1), resultados):
            if isinstance(resultado, BaseException):
                log.error("Excepción no anticipada en la página %s de Sparta, se omite: %r", pagina, resultado)
                continue
            if resultado is None:
                continue
            paginas_ok += 1
            todos.extend(resultado)

    if not todos:
        log.error("Sparta: no se detectó ninguna oferta en las %s páginas", total_paginas)
        return [], 0

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items_finales.append(item)

    log.info(
        "Sparta: %s ofertas crudas (sin duplicados) desde %s/%s páginas",
        len(items_finales), paginas_ok, total_paginas,
    )
    return items_finales, paginas_ok
