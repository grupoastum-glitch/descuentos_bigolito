"""Scrapea las ofertas de LuffyToys (luffytoys.cl).

Es una tienda Shopify estándar, con el endpoint público `/products.json` paginado por
`page`/`limit` (confirmado: ~1127 productos en 5 páginas de `limit=250`, sin bloqueo ni necesidad
de headless/stealth) — a diferencia de Falabella/Ripley/Paris, no hace falta reversar ningún
framework ni recorrer un árbol de categorías, la paginación server-side funciona tal cual.

La colección propia `/collections/ofertas` del sitio existe y su metadata dice
`"products_count": 15`, pero su endpoint `/collections/ofertas/products.json` siempre devuelve
`{"products":[]}` (confirmado con requests directas) — no sirve como fuente. En su lugar se
recorre el catálogo completo y se calcula el descuento de cada producto a partir de
`price`/`compare_at_price` de su primera variante: acá no existe ningún campo de "% off" del
sitio del que desconfiar (a diferencia de Ripley/Paris), directamente no lo hay.

Se excluyen los productos con tag "Preventa": son reservas de productos que todavía no están en
stock (a veces con más de un año de espera) con un descuento fijo (~20%) sobre un precio de
referencia futuro que nunca cambia — no es una oferta real, y al no cambiar nunca dispararía la
Regla 3 (republicación por antigüedad, ver ofertas_writer.py) cada 6h indefinidamente. También se
excluyen variantes sin stock (`available: false`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.luffytoys")

_BASE_URL = "https://luffytoys.cl"
_PRODUCTS_JSON_URL = f"{_BASE_URL}/products.json"
_LIMIT = 250


def _items_desde_pagina(productos: list[dict]) -> list[dict]:
    items = []
    for producto in productos:
        if "Preventa" in (producto.get("tags") or []):
            continue

        variantes = producto.get("variants") or []
        if not variantes:
            continue
        variante = variantes[0]
        if not variante.get("available"):
            continue

        precio_normal_raw = variante.get("compare_at_price")
        if not precio_normal_raw:
            continue
        precio_actual = int(float(variante["price"]))
        precio_normal = int(float(precio_normal_raw))
        if precio_normal <= precio_actual:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        imagenes = producto.get("images") or []
        items.append({
            "producto_id": str(producto["id"]),
            "titulo": producto["title"],
            "marca": producto.get("vendor"),
            "url": f"{_BASE_URL}/products/{producto['handle']}",
            "comercio": "LuffyToys",
            "imagen": imagenes[0]["src"] if imagenes else None,
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _fetch_pagina(sesion, pagina: int) -> list[dict]:
    url = f"{_PRODUCTS_JSON_URL}?page={pagina}&limit={_LIMIT}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    body = respuesta.body if isinstance(respuesta.body, str) else respuesta.body.decode("utf-8", errors="replace")
    datos = json.loads(body)
    return datos.get("products", [])


async def obtener_ofertas_luffytoys(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, páginas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    items: list[dict] = []
    pagina = 1
    paginas_ok = 0

    while True:
        try:
            productos = await _fetch_pagina(sesion, pagina)
        except Exception:
            log.exception("Falló la página %s de LuffyToys, se corta ahí", pagina)
            break

        paginas_ok += 1
        items.extend(_items_desde_pagina(productos))

        if len(productos) < _LIMIT:
            break  # última página del catálogo

        pagina += 1
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if paginas_ok == 0:
        log.error("LuffyToys: no se pudo leer ninguna página del catálogo")
        return [], 0

    log.info("LuffyToys: %s ofertas crudas en %s páginas leídas", len(items), paginas_ok)
    return items, paginas_ok
