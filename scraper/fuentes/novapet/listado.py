"""Scrapea las ofertas de NovaPet (novapet.cl) — cuarta tienda del rubro Mascotas.

Shopify estándar, mismo patrón que `fuentes.pethome.listado`: `/products.json?page=N&limit=250`
sin bloqueo. Precio actual/normal salen de `price`/`compare_at_price` de la primera variante.
Catálogo confirmado en vivo: **3188 productos, 13 páginas**. Sin tags "Preventa" y sin contenido
fuera de tema (Accesorios, Arenas, Farmacia, Latas, Sacos de Alimento, Snacks — 100% mascotas) —
no hace falta ningún filtro de tags ni categoría.

**Mismo caso que PetHome**: volumen de descuento real alto, 2065/3188 (64.8%), parejo entre
páginas salvo las últimas 2 (26-28% vs 55-84% en el resto) — merchandising de la tienda, no un
artefacto de datos (mismo criterio ya confirmado en vivo para PetHome). Decisión del usuario: se
sortean `_PAGINAS_POR_CORRIDA` páginas al azar de las `total_paginas` reales — mismo número
absoluto de páginas que PetHome (3), en vez de recorrerlas todas.

`/products.json` no expone un conteo total, pero `/collections/all` (HTML) sí — el texto
"N productos" aparece ahí, confirmado en vivo. Se pide esa página una vez por corrida (liviana)
para derivar `total_paginas = ceil(N/250)` y sortear del rango real (mismo mecanismo que PetHome).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.novapet")

_BASE_URL = "https://www.novapet.cl"
_COLLECTION_URL = f"{_BASE_URL}/collections/all"
_PRODUCTS_JSON_URL = f"{_BASE_URL}/products.json"
_LIMIT = 250
_PAGINAS_POR_CORRIDA = 7  # ~54% de cobertura — subido de 3 (~23%) el 2026-08-22 a pedido del
# usuario, sin haber visto señales de bloqueo con la cobertura anterior.

_TOTAL_RE = re.compile(r"([\d.,]+)\s*productos", re.I)


def _item_desde_producto(producto: dict) -> dict | None:
    variantes = producto.get("variants") or []
    if not variantes:
        return None
    variante = variantes[0]

    try:
        precio_actual = float(variante["price"])
        precio_normal = float(variante.get("compare_at_price") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not precio_actual or precio_normal <= precio_actual:
        return None
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    imagenes = producto.get("images") or []
    return {
        "producto_id": str(producto["id"]),
        "titulo": producto.get("title"),
        "marca": producto.get("vendor"),
        "url": f"{_BASE_URL}/products/{producto['handle']}",
        "comercio": "NovaPet",
        "imagen": imagenes[0]["src"] if imagenes else None,
        "precio_actual": round(precio_actual),
        "precio_normal": round(precio_normal),
        "descuento_pct": descuento_pct,
    }


async def _total_paginas(sesion) -> int:
    respuesta = await sesion.get(_COLLECTION_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_COLLECTION_URL}")
    html_ = respuesta.body if isinstance(respuesta.body, str) else respuesta.body.decode("utf-8", errors="replace")
    m = _TOTAL_RE.search(html_)
    if not m:
        raise RuntimeError(f"No se encontró el conteo de productos en {_COLLECTION_URL}")
    total = int(m.group(1).replace(".", "").replace(",", ""))
    return max(1, math.ceil(total / _LIMIT))


async def _fetch_pagina(sesion, pagina: int) -> list[dict]:
    url = f"{_PRODUCTS_JSON_URL}?page={pagina}&limit={_LIMIT}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    body = respuesta.body if isinstance(respuesta.body, str) else respuesta.body.decode("utf-8", errors="replace")
    datos = json.loads(body)

    items = []
    for producto in datos.get("products", []):
        item = _item_desde_producto(producto)
        if item:
            items.append(item)
    return items


async def obtener_ofertas_novapet(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, páginas leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        total_paginas = await _total_paginas(sesion)
    except Exception:
        log.exception("Falló la carga de %s para sacar el total de NovaPet, se aborta", _COLLECTION_URL)
        return [], 0

    paginas = random.sample(range(1, total_paginas + 1), min(_PAGINAS_POR_CORRIDA, total_paginas))
    log.info("NovaPet: %s páginas reales, sorteando %s", total_paginas, paginas)

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async def _fetch_con_semaforo(pagina: int) -> list[dict] | None:
        async with semaforo:
            try:
                return await _fetch_pagina(sesion, pagina)
            except Exception:
                log.exception("Falló la página %s de NovaPet, se omite", pagina)
                return None
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    resultados = await asyncio.gather(*(_fetch_con_semaforo(p) for p in paginas), return_exceptions=True)

    paginas_ok = 0
    todos: list[dict] = []
    for pagina, resultado in zip(paginas, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la página %s de NovaPet, se omite: %r", pagina, resultado)
            continue
        if resultado is None:
            continue
        paginas_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("NovaPet: no se detectó ninguna oferta en las %s páginas sorteadas", len(paginas))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "NovaPet: %s ofertas crudas (sin duplicados) en %s/%s páginas sorteadas leídas con éxito",
        len(items), paginas_ok, len(paginas),
    )
    return items, paginas_ok
