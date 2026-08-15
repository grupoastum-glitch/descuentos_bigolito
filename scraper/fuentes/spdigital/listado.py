"""Scrapea las ofertas de SPDigital (spdigital.cl) — segunda tienda del rubro Tech.

Gatsby (sitio estático, confirmado por `<meta name="generator" content="Gatsby ...">`) — a
diferencia de Falabella/Easy/Sodimac (Next.js con un bloque `__NEXT_DATA__`), acá los productos
vienen server-renderizados directo como HTML de verdad en cada página de categoría (tarjetas
`Fractal-ProductCard__productcard--container`, confirmado con `curl` y con scrapling), así que se
parsea con selectores CSS (`Response.css()` de scrapling) en vez de extraer JSON embebido.

7 categorías reales del menú (`_CATEGORIAS`, confirmadas a mano navegando el sitio — no hay
página "ofertas" propia). Paginación real confirmada por URL (no por query param):
`/categories/<slug>/` es la página 1, `/categories/<slug>/<N>/` las siguientes (ej.
`/categories/gaming-y-streaming/2/`, con productos distintos a la página 1). El total de páginas
sale de "0 - 48 de 406 productos" en `[class*=paginationInfo]`. Mismo muestreo que Easy/Sodimac
(catálogo de cientos de productos por categoría, no cabe entero cada corrida): página 1 siempre
(ancla) + `_PAGINAS_ALEATORIAS_POR_CATEGORIA` páginas al azar hasta un techo.

Cada tarjeta trae hasta 3 precios: uno tachado (precio normal, `Fractal-Price__price--strikethrough`,
solo si el producto está en oferta) y dos vigentes con su propia etiqueta ("Transferencias" /
"Otros medios de pago" — descuento extra por pagar con transferencia bancaria). Se usa "Otros
medios de pago" como precio_actual (el que aplica a cualquier comprador, sin depender de un medio
de pago específico) contra el precio tachado como precio_normal — mismo criterio ya aplicado a
Falabella/Xiaomi de no usar precios que no aplican a cualquier comprador (ver
Tiendas/paginas.md / memoria del proyecto sobre precio CMR). Se recalcula el % siempre desde esos
2 precios crudos, nunca desde el badge "N% DCTO." que muestra el sitio.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.spdigital")

_BASE_URL = "https://www.spdigital.cl"

_CATEGORIAS = [
    "audio-y-musica", "componentes", "computacion", "conectividad-y-redes",
    "gaming-y-streaming", "hogar-y-oficina", "otras-categorias",
]

_PRODUCTOS_POR_PAGINA = 48  # observado, confirmado contra el sitio real
_MAX_PAGINAS_POR_CATEGORIA = 10  # techo de páginas consideradas para el sorteo, por categoría
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)

_TOTAL_PRODUCTOS_RE = re.compile(r"de\s+([\d.]+)\s+productos", re.I)
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _item_desde_card(card) -> dict | None:
    link = card.css("a.Fractal-ProductCard--image")
    href = link[0].attrib.get("href") if link else None
    if not href:
        return None

    viejo = card.css("[class*=strikethrough]")
    precio_normal = _parse_precio(viejo[0].get_all_text(strip=True)) if viejo else None
    if not precio_normal:
        return None  # sin precio tachado, este producto no está en oferta

    precios_por_medio = {}
    for variante in card.css("[class*=priceVariantContainer]"):
        spans = variante.css("span")
        if len(spans) < 2:
            continue
        precio = _parse_precio(spans[0].get_all_text(strip=True))
        etiqueta = spans[1].get_all_text(strip=True).strip().lower()
        if precio:
            precios_por_medio[etiqueta] = precio
    precio_actual = precios_por_medio.get("otros medios de pago") or precios_por_medio.get("transferencias")
    if not precio_actual or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    titulo_nodo = card.css("[class*=productDescriptionTextContainer] a")
    titulo = titulo_nodo[0].get_all_text(strip=True) if titulo_nodo else None
    marca_nodo = card.css("[class*=productDetailsContainer] > span > a")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css("img")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None
    if imagen and imagen.startswith("//"):
        imagen = f"https:{imagen}"

    return {
        "producto_id": href.strip("/"),
        "titulo": titulo,
        "marca": marca,
        "url": f"{_BASE_URL}{href}",
        "comercio": "SPDigital",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


def _url_categoria(slug: str, pagina: int) -> str:
    if pagina == 1:
        return f"{_BASE_URL}/categories/{slug}/"
    return f"{_BASE_URL}/categories/{slug}/{pagina}/"


async def _fetch_pagina_categoria(sesion, slug: str, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de productos real de la categoría — usado para saber
    cuántas páginas hay)."""
    url = _url_categoria(slug, pagina)
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css(".Fractal-ProductCard__productcard--container"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    total = 0
    info = respuesta.css("[class*=paginationInfo]")
    if info:
        m = _TOTAL_PRODUCTOS_RE.search(info[0].get_all_text(strip=True))
        if m:
            total = int(m.group(1).replace(".", ""))
    return items, total


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, slug: str) -> list[dict] | None:
    async with semaforo:
        try:
            items, total = await _fetch_pagina_categoria(sesion, slug, 1)
        except Exception:
            log.exception("Falló la página 1 de la categoría %s de SPDigital, se omite", slug)
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
                log.exception("Falló la página %s de la categoría %s de SPDigital, se omite esa página", pagina, slug)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_spdigital(sesion) -> tuple[list[dict], int]:
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
            log.error("Excepción no anticipada en la categoría %s de SPDigital, se omite: %r", slug, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("SPDigital: no se detectó ninguna oferta en las %s categorías", len(_CATEGORIAS))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "SPDigital: %s ofertas crudas (sin duplicados) desde %s/%s categorías",
        len(items), categorias_ok, len(_CATEGORIAS),
    )
    return items, categorias_ok
