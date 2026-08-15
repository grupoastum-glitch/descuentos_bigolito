"""Scrapea las ofertas de Bestmart (bestmart.cl) — primera tienda del rubro Tech.

Shopify confirmado (dominio de imágenes/CDN `cdn.shopify.com`, `/cdn/shop/`). Usa el endpoint
JSON público del catálogo (`/products.json?limit=250&page=N`, sin necesidad de sesión ni token) —
no hace falta parsear HTML. Catálogo chico (~920 productos, ~4 páginas de 250): se recorre
completo cada corrida, sin muestreo aleatorio (mismo criterio que fuentes/geekz — catálogos que
entran enteros en pocas páginas se leen enteros, el muestreo con ancla+aleatorias es solo para
catálogos de miles de productos como Easy/Sodimac).

Descuento: cada variante trae `price` y `compare_at_price` — si `compare_at_price > price` es una
oferta real; se recalcula el % siempre desde esos dos precios crudos (no se confía en ningún
badge del sitio). Se descartan productos "Preventa" (título empieza con esa palabra, mismo
criterio que LuffyToys/WePlay) — son pre-pedidos sin stock físico todavía, no ofertas reales
aunque alguno llegue a traer `compare_at_price`.
"""
from __future__ import annotations

import asyncio
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.bestmart")

_BASE_URL = "https://bestmart.cl"
_PRODUCTOS_POR_PAGINA = 250  # máximo que acepta el endpoint público de Shopify


def _item_desde_producto(producto: dict) -> dict | None:
    titulo = producto.get("title") or ""
    if titulo.strip().lower().startswith("preventa"):
        return None  # pre-pedido sin stock físico todavía, no es una oferta real

    variantes = producto.get("variants") or []
    if not variantes:
        return None
    variante = variantes[0]

    try:
        precio_actual = float(variante.get("price") or 0)
        precio_normal = float(variante.get("compare_at_price") or 0)
    except (TypeError, ValueError):
        return None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    handle = producto.get("handle")
    if not handle:
        return None
    imagenes = producto.get("images") or []

    return {
        "producto_id": str(producto.get("id")),
        "titulo": titulo,
        "marca": producto.get("vendor"),
        "url": f"{_BASE_URL}/products/{handle}",
        "comercio": "Bestmart",
        "imagen": imagenes[0]["src"] if imagenes else None,
        "precio_actual": round(precio_actual),
        "precio_normal": round(precio_normal),
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> list[dict]:
    url = f"{_BASE_URL}/products.json?limit={_PRODUCTOS_POR_PAGINA}&page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    return respuesta.json().get("products") or []


async def obtener_ofertas_bestmart(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, páginas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    items: list[dict] = []
    paginas_ok = 0
    pagina = 1

    while True:
        try:
            productos = await _fetch_pagina(sesion, pagina)
        except Exception:
            log.exception("Falló la página %s de Bestmart, se corta ahí", pagina)
            break

        paginas_ok += 1
        if not productos:
            break  # fin del catálogo

        for producto in productos:
            item = _item_desde_producto(producto)
            if item:
                items.append(item)

        if len(productos) < _PRODUCTOS_POR_PAGINA:
            break  # última página (parcial)

        pagina += 1
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if paginas_ok == 0:
        log.error("Bestmart: no se pudo leer ninguna página del catálogo")
        return [], 0

    log.info("Bestmart: %s ofertas crudas en %s páginas leídas", len(items), paginas_ok)
    return items, paginas_ok
