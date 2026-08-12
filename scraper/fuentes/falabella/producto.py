"""Scrapea páginas de producto puntuales (las listadas en productos_seguidos.json) para
detectar bajas de precio, incluso en productos que no aparecen en el listado general de ofertas.
"""
from __future__ import annotations

import asyncio
import logging
import random

import config
from fuentes.falabella.parsing import calcular_descuento_pct, extraer_next_data, precios_desde_lista

log = logging.getLogger("scraper.fuentes.producto")


def _variante_actual(producto_data: dict) -> dict | None:
    variante_id = producto_data.get("currentVariant")
    for variante in producto_data.get("variants", []):
        if variante.get("id") == variante_id:
            return variante
    variantes = producto_data.get("variants") or []
    return variantes[0] if variantes else None


async def _scrapear_producto(sesion, url: str) -> dict | None:
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html = respuesta.body.decode("utf-8", errors="replace")
    datos = extraer_next_data(html)
    producto_data = datos["props"]["pageProps"]["productData"]

    variante = _variante_actual(producto_data)
    if not variante:
        return None

    precio_actual, precio_normal = precios_desde_lista(variante.get("prices", []))
    if precio_actual is None or not precio_normal or precio_actual >= precio_normal:
        return None  # no está en oferta ahora mismo

    badge = (variante.get("discountBadge") or {}).get("label")
    descuento_pct = calcular_descuento_pct(precio_actual, precio_normal, badge)

    return {
        "producto_id": str(producto_data["id"]),
        "titulo": producto_data.get("name") or "",
        "marca": producto_data.get("brandName"),
        "url": url,
        "comercio": "Falabella",
        "imagen": None,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def obtener_ofertas_productos_seguidos(sesion, productos_seguidos: list[dict]) -> list[dict]:
    items = []
    activos = [p for p in productos_seguidos if p.get("activo")]
    for producto in activos:
        try:
            item = await _scrapear_producto(sesion, producto["url"])
            if item:
                items.append(item)
        except Exception:
            log.exception("Falló el scrape del producto seguido %s", producto.get("url"))
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    log.info("Productos seguidos: %s/%s con oferta activa detectada", len(items), len(activos))
    return items
