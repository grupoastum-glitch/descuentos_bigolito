"""Scrapea las ofertas de Dust2.gg (dust2.gg) — cuarta tienda del rubro Tech.

WordPress + WooCommerce confirmado (`wp-json`, `wp-content/plugins/woocommerce`). Usa la Store API
pública de WooCommerce (`/wp-json/wc/store/v1/products?on_sale=true`, sin auth ni token) — no hace
falta parsear HTML. Con `on_sale=true` el catálogo completo de ofertas entra en 3 páginas de 100
(~261 productos confirmado en vivo), así que se recorre entero cada corrida, sin muestreo aleatorio
(mismo criterio que fuentes.bestmart/fuentes.geekz: catálogos que entran enteros en pocas páginas
se leen enteros).

Cada producto trae `prices.price` y `prices.regular_price` ya en pesos enteros
(`currency_minor_unit: 0` — a diferencia de otras tiendas WooCommerce que usan centavos, acá no hay
que dividir por 100, confirmado contra el sitio real). Se recalcula el % siempre desde esos 2
precios, nunca desde el `on_sale`/badge del sitio. Se descartan productos "Preventa" (el nombre
empieza con esa palabra, mismo criterio que LuffyToys/WePlay/Bestmart) — son pre-pedidos sin stock
físico todavía, no ofertas reales.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.dust2")

_BASE_URL = "https://dust2.gg"
_API_URL = f"{_BASE_URL}/wp-json/wc/store/v1/products"
_PRODUCTOS_POR_PAGINA = 100  # máximo aceptado por la Store API de WooCommerce


def _item_desde_producto(producto: dict) -> dict | None:
    nombre = html.unescape(producto.get("name") or "")
    if nombre.strip().lower().startswith("preventa"):
        return None  # pre-pedido sin stock físico todavía, no es una oferta real

    precios = producto.get("prices") or {}
    try:
        precio_actual = float(precios["price"])
        precio_normal = float(precios["regular_price"])
    except (KeyError, TypeError, ValueError):
        return None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    producto_id = producto.get("id")
    permalink = producto.get("permalink")
    if producto_id is None or not permalink:
        return None
    imagenes = producto.get("images") or []

    return {
        "producto_id": str(producto_id),
        "titulo": nombre,
        "marca": None,
        "url": permalink,
        "comercio": "Dust2.gg",
        "imagen": imagenes[0]["src"] if imagenes else None,
        "precio_actual": round(precio_actual),
        "precio_normal": round(precio_normal),
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> list[dict]:
    url = f"{_API_URL}?on_sale=true&per_page={_PRODUCTOS_POR_PAGINA}&page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    return respuesta.json() or []


async def obtener_ofertas_dust2(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, páginas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    items: list[dict] = []
    paginas_ok = 0
    pagina = 1

    while True:
        try:
            productos = await _fetch_pagina(sesion, pagina)
        except Exception:
            log.exception("Falló la página %s de Dust2.gg, se corta ahí", pagina)
            break

        paginas_ok += 1
        if not productos:
            break  # fin del catálogo en oferta

        for producto in productos:
            item = _item_desde_producto(producto)
            if item:
                items.append(item)

        if len(productos) < _PRODUCTOS_POR_PAGINA:
            break  # última página (parcial)

        pagina += 1
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if paginas_ok == 0:
        log.error("Dust2.gg: no se pudo leer ninguna página del catálogo en oferta")
        return [], 0

    log.info("Dust2.gg: %s ofertas crudas en %s páginas leídas", len(items), paginas_ok)
    return items, paginas_ok
