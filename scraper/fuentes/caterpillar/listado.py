"""Scrapea las ofertas de Caterpillar (shopcaterpillar.cl, ropa/calzado/accesorios de la marca,
no herramientas) — se suma al Retail General (ofertas_40/ofertas_vip vía canal_para_descuento(),
no a un canal especial — ver scraper/config.py).

Primera tienda del proyecto sobre **Shopify** (el resto son VTEX o Demandware/Salesforce).
Shopify expone una API JSON pública y sin autenticación, `/products.json`, que no requiere
resolver árbol de categorías ni cgid: se pagina directo con `?limit=250&page=N` hasta que
devuelve una lista vacía (confirmado en vivo: página 7 vacía, catálogo completo en 6 páginas).
Cloudflare presente pero no bloquea nada — responde 200 con un fetch plano.

Catálogo chico (1371 productos), muy por debajo de cualquier tope de paginación conocido en el
proyecto — se lee el 100% cada corrida, sin sortear ni truncar.

Cada producto trae `variants[]` (una por talla/color) con `price`/`compare_at_price`; en la
muestra revisada el precio y el descuento son iguales en todas las variantes de un mismo
producto — se toma igual el mínimo con descuento real (`compare_at_price > price`) por
consistencia con el resto de fuentes. Imagen: `images[0].src`, ya en CDN de Shopify
(`cdn.shopify.com`), sin antecedentes de bloqueo con el fetcher de Telegram — no se suma
preventivamente a `telegram_publisher._COMERCIOS_CON_CDN_BLOQUEADO`, se espera confirmar con una
corrida real (mismo criterio que ABC/H&M/Fensa/Nike).

Prueba end-to-end real (2026-08-24): de 1371 productos leídos, 810 con descuento real (59.1%,
la tasa más alta del proyecto hasta ahora), descuento 20%-71% (promedio 42.8%). Único vendor
("catcl") en todo el catálogo — confirma que es la tienda oficial de Caterpillar, sin productos
de terceros mezclados.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.caterpillar")

_BASE_URL = "https://www.shopcaterpillar.cl"
_PRODUCTS_URL = f"{_BASE_URL}/products.json"
_PAGINA = 250  # tope real del endpoint Shopify


def _item_desde_producto(producto: dict) -> dict | None:
    producto_id = producto.get("id")
    titulo = producto.get("title")
    handle = producto.get("handle")
    if not producto_id or not titulo or not handle:
        return None
    url = f"{_BASE_URL}/products/{handle}"

    imagenes = producto.get("images") or []
    imagen = imagenes[0].get("src") if imagenes else None

    mejor: tuple[int, int] | None = None  # (precio_actual, precio_normal)
    for variante in producto.get("variants") or []:
        try:
            actual = float(variante["price"])
            normal = float(variante.get("compare_at_price") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if normal <= actual:
            continue
        if mejor is None or actual < mejor[0]:
            mejor = (round(actual), round(normal))

    if mejor is None:
        return None
    precio_actual, precio_normal = mejor
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": "Caterpillar",
        "url": url,
        "comercio": "Caterpillar",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> list[dict]:
    url = f"{_PRODUCTS_URL}?limit={_PAGINA}&page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    productos = json.loads(respuesta.body).get("products") or []
    items = [i for i in (_item_desde_producto(p) for p in productos) if i]
    return items, len(productos)


async def obtener_ofertas_caterpillar(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, 1 si se leyó algo / 0 si no — mismo contrato que el
    resto de fuentes.<tienda>.listado, acá con paginación simple en vez de árbol de categorías)."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    todos: list[dict] = []
    pagina = 1
    while True:
        async with semaforo:
            try:
                items, cantidad_cruda = await _fetch_pagina(sesion, pagina)
            except Exception:
                log.exception("Falló la página %s de Caterpillar, se corta ahí", pagina)
                break
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
        if cantidad_cruda == 0:
            break
        todos.extend(items)
        if cantidad_cruda < _PAGINA:
            break
        pagina += 1

    if not todos:
        log.error("Caterpillar: no se detectó ninguna oferta")
        return [], 0

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items_finales.append(item)

    log.info("Caterpillar: %s ofertas crudas (sin duplicados) en %s páginas leídas", len(items_finales), pagina)
    return items_finales, 1
