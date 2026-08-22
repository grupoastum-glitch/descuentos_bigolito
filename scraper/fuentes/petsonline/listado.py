"""Scrapea las ofertas de PetsOnline (petsonline.cl) — quinta tienda del rubro Mascotas.

Shopify estándar, mismo patrón que `fuentes.luffytoys.listado`: `/products.json?page=N&limit=250`
sin bloqueo. Precio actual/normal salen de `price`/`compare_at_price` de la primera variante.
Catálogo chico confirmado en vivo: **454 productos, 2 páginas** — a diferencia de PetHome/NovaPet,
no hace falta sortear páginas, se recorre completo cada corrida (mismo criterio que
SuperZoo/Bestmart/Dust2.gg: catálogos chicos no necesitan sorteo).

Catálogo con variedad veterinaria (medicamentos, antiparasitarios, cremas dermatológicas) además
de alimento/accesorios, pero 100% mascotas — sin nada fuera de tema, sin tags "Preventa".

Volumen de descuento real normal (30/454, 6.6% — en línea con SuperZoo ~4.7%, no con el patrón de
PetHome/NovaPet), así que no hace falta ningún mecanismo de "ancla"/sorteo, alcanza con el bucle
secuencial simple.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.petsonline")

_BASE_URL = "https://petsonline.cl"
_PRODUCTS_JSON_URL = f"{_BASE_URL}/products.json"
_LIMIT = 250


def _items_desde_pagina(productos: list[dict]) -> list[dict]:
    items = []
    for producto in productos:
        variantes = producto.get("variants") or []
        if not variantes:
            continue
        variante = variantes[0]

        try:
            precio_actual = float(variante["price"])
            precio_normal = float(variante.get("compare_at_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not precio_actual or precio_normal <= precio_actual:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        imagenes = producto.get("images") or []
        items.append({
            "producto_id": str(producto["id"]),
            "titulo": producto.get("title"),
            "marca": producto.get("vendor"),
            "url": f"{_BASE_URL}/products/{producto['handle']}",
            "comercio": "PetsOnline",
            "imagen": imagenes[0]["src"] if imagenes else None,
            "precio_actual": round(precio_actual),
            "precio_normal": round(precio_normal),
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


async def obtener_ofertas_petsonline(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, páginas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    items: list[dict] = []
    pagina = 1
    paginas_ok = 0

    while True:
        try:
            productos = await _fetch_pagina(sesion, pagina)
        except Exception:
            log.exception("Falló la página %s de PetsOnline, se corta ahí", pagina)
            break

        paginas_ok += 1
        items.extend(_items_desde_pagina(productos))

        if len(productos) < _LIMIT:
            break  # última página del catálogo

        pagina += 1
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if paginas_ok == 0:
        log.error("PetsOnline: no se pudo leer ninguna página del catálogo")
        return [], 0

    log.info("PetsOnline: %s ofertas crudas en %s páginas leídas", len(items), paginas_ok)
    return items, paginas_ok
