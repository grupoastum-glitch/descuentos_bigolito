"""Scrapea las ofertas de PCFactory (pcfactory.cl) — tercera tienda del rubro Tech.

Plataforma Modyo (CMS/experiencia digital chileno) — a diferencia del resto de las tiendas del
proyecto, el HTML que devuelve el servidor es el mismo "shell" vacío para cualquier ruta (SPA:
confirmado que `/computadores-y-tablets/notebooks/notebooks` y la home devuelven bytes casi
idénticos), así que no hay nada que parsear en el HTML — todo el catálogo se sirve por API REST
propia. El endpoint de productos no está linkeado en ningún lado del sitio ni documentado: se
encontró grabando las respuestas de red reales que hace el navegador al abrir una página de
categoría (`AsyncStealthySession` + listener de `response`, ver sesión 2026-08-15), y después se
confirmó que responde igual con un fetch plano (sin Cloudflare/challenge de por medio, a
diferencia de Ripley/WePlay que sí necesitan navegador headless).

Dos endpoints:
1. `https://api.pcfactory.cl/api-dex-catalog/v1/catalog/category/PCF/menu` (ya se usaba para el
   menú de categorías del sitio): árbol completo, se recorre `childCategories` recursivamente
   hasta las hojas (sin hijos) — 265 categorías hoja confirmadas.
2. `https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo/productos/query?page=N&size=48&categorias=<id>`
   — `categorias` es obligatorio (sin él da 0 resultados); `size` tiene un tope real de 48
   (pedir más no trae más). Responde `content.items[]` + `content.pageable.totalPages`.

265 categorías es demasiado para recorrer entero cada corrida — mismo criterio que Sodimac: se
muestrean `_MAX_CATEGORIAS` al azar, y de cada una página 0 (ancla, da el total real) +
`_PAGINAS_ALEATORIAS_POR_CATEGORIA` páginas al azar hasta un techo.

Cada producto trae 3 precios (`precio.efectivo` < `precio.normal` < `precio.referencia`).
A diferencia del precio CMR de Falabella/Xiaomi o el "Transferencias" de SPDigital (que sí se
descartan por requerir un medio de pago restrictivo — tarjeta propia con aprobación previa),
`efectivo` es débito/transferencia/efectivo real: un medio al alcance de cualquier comprador, no
una restricción. Se usa `efectivo` como precio_actual y `referencia` como precio_normal — de
paso coincide con el % de descuento que el propio sitio destaca. Se recalcula igual siempre,
nunca se confía en un badge de texto.

La imagen (`thumbnail`, ruta relativa) no sirve sobre `www.pcfactory.cl` (redirige a home) —
sirve sobre el subdominio de assets, `https://assets.pcfactory.cl<thumbnail>` (confirmado).
"""
from __future__ import annotations

import asyncio
import logging
import random

import config

log = logging.getLogger("scraper.fuentes.pcfactory")

_CATEGORIAS_URL = "https://api.pcfactory.cl/api-dex-catalog/v1/catalog/category/PCF/menu"
_PRODUCTOS_URL = "https://api.pcfactory.cl/pcfactory-services-catalogo/v1/catalogo/productos/query"
_ASSETS_BASE = "https://assets.pcfactory.cl"
_PRODUCTO_BASE = "https://www.pcfactory.cl/producto"

_PRODUCTOS_POR_PAGINA = 48  # tope real del endpoint, confirmado contra el sitio real
_MAX_CATEGORIAS = 60  # mismo criterio que fuentes.sodimac._MAX_CATEGORIAS: recorrer las 265
# categorías hoja enteras sería excesivo cada corrida
_MAX_PAGINAS_POR_CATEGORIA = 10
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 0 (siempre se pide)


def _item_desde_producto(producto: dict) -> dict | None:
    precio = producto.get("precio") or {}
    try:
        precio_actual = float(precio["efectivo"])
        precio_normal = float(precio["referencia"])
    except (KeyError, TypeError, ValueError):
        return None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    slug = producto.get("slug")
    producto_id = producto.get("id")
    if not slug or producto_id is None:
        return None
    thumbnail = producto.get("thumbnail")

    return {
        "producto_id": str(producto_id),
        "titulo": producto.get("nombre"),
        "marca": producto.get("marca"),
        "url": f"{_PRODUCTO_BASE}/{slug}",
        "comercio": "PCFactory",
        "imagen": f"{_ASSETS_BASE}{thumbnail}" if thumbnail else None,
        "precio_actual": round(precio_actual),
        "precio_normal": round(precio_normal),
        "descuento_pct": descuento_pct,
    }


def _items_desde_lista(productos: list[dict]) -> list[dict]:
    items = []
    for producto in productos:
        item = _item_desde_producto(producto)
        if item:
            items.append(item)
    return items


def _categorias_hoja(nodos: list[dict]) -> list[int]:
    hojas = []
    for nodo in nodos:
        hijos = nodo.get("childCategories") or []
        if hijos:
            hojas.extend(_categorias_hoja(hijos))
        elif nodo.get("id") is not None:
            hojas.append(nodo["id"])
    return hojas


async def _categorias_disponibles(sesion) -> list[int]:
    respuesta = await sesion.get(_CATEGORIAS_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_CATEGORIAS_URL}")
    return _categorias_hoja(respuesta.json() or [])


async def _fetch_pagina_categoria(sesion, categoria_id: int, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, totalPages real de la categoría)."""
    url = f"{_PRODUCTOS_URL}?page={pagina}&size={_PRODUCTOS_POR_PAGINA}&categorias={categoria_id}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    contenido = (respuesta.json() or {}).get("content") or {}
    total_paginas = (contenido.get("pageable") or {}).get("totalPages", 1)
    return _items_desde_lista(contenido.get("items") or []), total_paginas


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, categoria_id: int) -> list[dict] | None:
    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina_categoria(sesion, categoria_id, 0)
        except Exception:
            log.exception("Falló la página 0 de la categoría %s de PCFactory, se omite", categoria_id)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    techo = min(total_paginas or 1, _MAX_PAGINAS_POR_CATEGORIA)
    candidatas = list(range(1, techo))  # páginas 1..techo-1 (page 0 ya se pidió)
    extra_paginas = random.sample(candidatas, min(_PAGINAS_ALEATORIAS_POR_CATEGORIA, len(candidatas)))

    for pagina in extra_paginas:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina_categoria(sesion, categoria_id, pagina)
                items.extend(extra_items)
            except Exception:
                log.exception("Falló la página %s de la categoría %s de PCFactory, se omite esa página", pagina, categoria_id)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_pcfactory(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no
    se pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        categorias_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga del árbol de categorías de PCFactory (%s), se aborta", _CATEGORIAS_URL)
        return [], 0

    if not categorias_disponibles:
        log.error("PCFactory: el árbol de categorías no tiene ninguna hoja utilizable")
        return [], 0

    categorias = random.sample(categorias_disponibles, min(_MAX_CATEGORIAS, len(categorias_disponibles)))
    log.info("PCFactory: %s categorías hoja disponibles, muestreando %s", len(categorias_disponibles), len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, cid) for cid in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for cid, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de PCFactory, se omite: %r", cid, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("PCFactory: no se detectó ninguna oferta en las %s categorías muestreadas", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "PCFactory: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
