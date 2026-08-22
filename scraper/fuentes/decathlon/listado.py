"""Scrapea las ofertas de Decathlon Chile (decathlon.cl) — primera tienda del rubro Fitness.

VTEX (con Algolia para el buscador, pero el listado de productos viene server-rendered en HTML
normal — no hace falta la API de Algolia). Detrás de Cloudflare.

**Cloudflare bloquea la home (`/`) y las fichas de producto (`/p/<slug>.html`) con un challenge
real (403 "Just a moment...", confirmado incluso con `FetcherSession(impersonate="chrome")`),
pero NO las páginas de categoría/listado** (`/5968-ofertas`, con paginación `?page=N`, probado
200 en 5/5 intentos). Como el resto de fuentes.<tienda>.listado nunca visita la home ni la ficha
de producto para armar una oferta, este bloqueo parcial no es un impedimento — a diferencia de
Ripley/WePlay (bloqueados también en las páginas que sí se necesitaban). Por eso este módulo NO
debe pedir nunca `_BASE_URL` pelado ni `/p/*.html`.

Se usa `/5968-ofertas`, la página de ofertas curadas de Decathlon (cubre los 65 deportes que
vende, no solo fitness/gimnasio — decisión del usuario: el canal es para deportes/actividad
física en general). A diferencia de una categoría normal, esta página está compuesta 100% por
productos en oferta real (confirmado: 40/40 tarjetas de una página de muestra con precio
tachado) — se recorre completa cada corrida, sin sorteo (solo ~11 páginas totales, catálogo
chico, mismo criterio que SuperZoo con sus categorías).

Cada tarjeta (`article.product-card`, `data-sku="<uuid>"`) trae precio actual con valor numérico
ya limpio en `data-value` y precio antes del descuento como texto plano (`.price_barred-amount`,
sin `data-value`) — se recalcula el % siempre desde esos 2, nunca desde el badge `.price_discount`
que ya trae el HTML.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.decathlon")

_BASE_URL = "https://www.decathlon.cl"
_OFERTAS_URL = f"{_BASE_URL}/5968-ofertas"

_TOTAL_PAGINAS_RE = re.compile(r'data-testid="pagination-info">\s*\d+\s*<span>of</span>\s*(\d+)')
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _item_desde_card(card) -> dict | None:
    producto_id = card.attrib.get("data-sku")
    if not producto_id:
        return None

    actual_nodo = card.css(".price_amount")
    if not actual_nodo:
        return None
    try:
        precio_actual = int(actual_nodo[0].attrib.get("data-value"))
    except (TypeError, ValueError):
        return None

    normal_nodo = card.css(".price_barred-amount")
    if not normal_nodo:
        return None  # sin precio tachado, este producto no está en oferta real
    precio_normal = _parse_precio(normal_nodo[0].get_all_text(strip=True))
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    link_nodo = card.css(".product-card_header a")
    if not link_nodo:
        return None
    url = link_nodo[0].attrib.get("href")
    if not url:
        return None
    titulo_nodo = link_nodo[0].css("h2")
    titulo = titulo_nodo[0].get_all_text(strip=True) if titulo_nodo else link_nodo[0].get_all_text(strip=True)

    marca_nodo = card.css('[data-testid="product-card-brand"]')
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css(".product-card_image img")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    return {
        "producto_id": producto_id,
        "titulo": titulo,
        "marca": marca,
        "url": url,
        "comercio": "Decathlon",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de páginas real de la fuente, sale directo del
    texto "N of M" del paginador)."""
    url = _OFERTAS_URL if pagina == 1 else f"{_OFERTAS_URL}?page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css("article.product-card"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    html_ = respuesta.body.decode("utf-8", errors="replace")
    m = _TOTAL_PAGINAS_RE.search(html_)
    total_paginas = int(m.group(1)) if m else 1
    return items, total_paginas


async def obtener_ofertas_decathlon(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, páginas leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina(sesion, 1)
        except Exception:
            log.exception("Falló la página 1 de las ofertas de Decathlon, se aborta")
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
                log.exception("Falló la página %s de las ofertas de Decathlon, se omite esa página", pagina)
                return None
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if total_paginas > 1:
        resultados = await asyncio.gather(*(
            _fetch_extra(pagina) for pagina in range(2, total_paginas + 1)
        ), return_exceptions=True)
        for pagina, resultado in zip(range(2, total_paginas + 1), resultados):
            if isinstance(resultado, BaseException):
                log.error("Excepción no anticipada en la página %s de Decathlon, se omite: %r", pagina, resultado)
                continue
            if resultado is None:
                continue
            paginas_ok += 1
            todos.extend(resultado)

    if not todos:
        log.error("Decathlon: no se detectó ninguna oferta en las %s páginas", total_paginas)
        return [], 0

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items_finales.append(item)

    log.info(
        "Decathlon: %s ofertas crudas (sin duplicados) desde %s/%s páginas",
        len(items_finales), paginas_ok, total_paginas,
    )
    return items_finales, paginas_ok
