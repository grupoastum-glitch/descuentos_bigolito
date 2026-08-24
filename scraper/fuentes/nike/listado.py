"""Scrapea las ofertas de Nike (nike.cl) — se suma al Canal Fitness (junto a Decathlon/Sparta/
GymPro, vía `TIENDAS_FITNESS`/`_CANAL_ESPECIAL_POR_TIENDA` — ver scraper/config.py), no a Retail
General: decisión explícita del usuario, encaja temáticamente con indumentaria/zapatillas
deportivas (a diferencia de H&M/Moda, que se solapaba con todo Retail General).

VTEX, misma API clásica que `fuentes.hm.listado`/`fuentes.fensa.listado`
(`/api/catalog_system/pub/...`), funcionando directo sobre el dominio propio pese a Cloudflare
(cookie `__cf_bm` presente pero no bloquea un fetch plano).

Integración más simple del proyecto: el árbol de categorías solo trae 2 nodos reales, `nike`
(2253 productos, la tienda real) y `snkrs` (184 productos, la vitrina de lanzamientos
exclusivos/hype) — confirmado en vivo que `snkrs` no tiene descuentos reales (0/50 en una muestra,
15/50 sin stock: es para hype de drops, no para rebajas) y `nike` sí (5/50 en una muestra, ~10%).
No hace falta pedir el árbol de categorías: se usa directo la constante `_CATEGORIA = "nike"`.

2253 productos está muy por debajo del tope de paginación de VTEX (2500, ver
`fuentes.hm.listado`) — se lee el 100% del catálogo cada corrida en una sola pasada, sin sortear
ni truncar.

Precio/URL/imagen/producto_id: mismos campos que H&M/Fensa (`items[].sellers[].commertialOffer`,
campo `link`, `items[].images[].imageUrl`, `productId`). Imagen en CDN separado
(`nikeclprod.vteximg.com.br`), mismo riesgo de bloqueo con el fetcher de Telegram que
H&M/Fensa/Sodimac — no sumada preventivamente a `_COMERCIOS_CON_CDN_BLOQUEADO`, pendiente de
confirmar con una corrida real.

Prueba end-to-end real (2026-08-24): 590 ofertas crudas (sin duplicados) de 2253 productos leídos
(~26% con descuento real, más alto que el 10% de la muestra chica de 50), descuento 13%-50%
(promedio 33%) — volumen sorprendentemente alto para una sola marca, comparable a Falabella/
Ripley, muy por encima de Decathlon/Sparta/GymPro dentro del propio Canal Fitness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.nike")

_BASE_URL = "https://www.nike.cl"
_SEARCH_URL = f"{_BASE_URL}/api/catalog_system/pub/products/search"
_CATEGORIA = "nike"  # única categoría con descuentos reales, confirmado en vivo — "snkrs" (la
# otra categoría del árbol) es la vitrina de lanzamientos, sin descuentos reales.

_PAGINA = 50  # tope real del endpoint (_to - _from <= 49)
_MAX_FROM = 2500  # tope duro de VTEX (ver fuentes.hm.listado) — nike.cl (~2253 productos) no se
# acerca a esto, se deja por consistencia/seguridad futura.

_RESOURCES_RE = re.compile(r"(\d+)-(\d+)/(\d+)")


def _total_desde_headers(headers: dict) -> int | None:
    for clave, valor in headers.items():
        if clave.lower() != "resources":
            continue
        m = _RESOURCES_RE.search(valor)
        if m:
            return int(m.group(3))
    return None


def _item_desde_producto(producto: dict) -> dict | None:
    producto_id = producto.get("productId")
    titulo = producto.get("productName")
    url = producto.get("link")
    if not producto_id or not titulo or not url:
        return None

    mejor: tuple[int, int, str | None] | None = None  # (precio_actual, precio_normal, imagen)
    for item in producto.get("items") or []:
        imagen_nodo = item.get("images") or []
        imagen = imagen_nodo[0].get("imageUrl") if imagen_nodo else None
        for seller in item.get("sellers") or []:
            oferta = seller.get("commertialOffer") or {}
            try:
                actual = float(oferta["Price"])
                normal = float(oferta["ListPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if normal <= actual:
                continue
            if mejor is None or actual < mejor[0]:
                mejor = (round(actual), round(normal), imagen)

    if mejor is None:
        return None
    precio_actual, precio_normal, imagen = mejor
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": producto.get("brand") or None,
        "url": url,
        "comercio": "Nike",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, desde: int, hasta: int) -> tuple[list[dict], dict] | None:
    url = f"{_SEARCH_URL}/{_CATEGORIA}?_from={desde}&_to={hasta}"
    respuesta = await sesion.get(url)
    if respuesta.status == 400:
        return None  # tope duro de VTEX (_from > 2500) — fin de lo leíble
    if respuesta.status not in (200, 206):
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    productos = json.loads(respuesta.body)
    items = [i for i in (_item_desde_producto(p) for p in productos) if i]
    return items, respuesta.headers


async def obtener_ofertas_nike(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, 1 si se leyó algo / 0 si no — mismo contrato que el
    resto de fuentes.<tienda>.listado, acá con una sola categoría en vez de una lista)."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async with semaforo:
        try:
            resultado = await _fetch_pagina(sesion, 0, _PAGINA - 1)
        except Exception:
            log.exception("Falló la primera página de Nike, se aborta")
            return [], 0
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if resultado is None:
        log.error("Nike: la primera página no devolvió nada, se aborta")
        return [], 0
    items, headers = resultado
    total = _total_desde_headers(headers)
    if total is None:
        log.warning("Nike: no trajo header 'resources', se toma solo la primera página")
        total = len(items)

    tope = min(total, _MAX_FROM + _PAGINA)
    todos = list(items)
    desde = _PAGINA
    while desde < tope:
        hasta = min(desde + _PAGINA - 1, tope - 1)
        async with semaforo:
            try:
                resultado = await _fetch_pagina(sesion, desde, hasta)
            except Exception:
                log.exception("Falló una página (%s-%s) de Nike, se corta ahí", desde, hasta)
                break
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
        if resultado is None:
            break  # tope duro de VTEX alcanzado
        pagina_items, _ = resultado
        todos.extend(pagina_items)
        desde += _PAGINA

    if total > tope:
        log.info("Nike: catálogo truncado por el tope de VTEX (%s de %s productos leídos)", tope, total)

    if not todos:
        log.error("Nike: no se detectó ninguna oferta en %s productos leídos", total)
        return [], 0

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items_finales.append(item)

    log.info("Nike: %s ofertas crudas (sin duplicados) de %s productos leídos", len(items_finales), total)
    return items_finales, 1
