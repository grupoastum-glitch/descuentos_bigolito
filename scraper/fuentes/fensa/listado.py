"""Scrapea las ofertas de Fensa (tiendafensa.cl, línea blanca/electrodomésticos) — se suma al
Retail General (ofertas_40/ofertas_vip vía canal_para_descuento(), no a un canal especial — ver
scraper/config.py).

VTEX. Misma API clásica que `fuentes.hm.listado` (`/api/catalog_system/pub/...`), funcionando
directo sobre el dominio propio, sin Cloudflare ni challenge. Mismo mecanismo de cosecha: se pide
una sola vez `/api/catalog_system/pub/category/tree/50` y se recorren los nodos de primer nivel
tal cual, sin bajar a sub-categorías — acá el árbol ya viene chico y específico por tipo de
producto (Refrigerador, Lavadora, Horno, Secadora, Campana, Estufa, Calefont, etc.), a diferencia
de H&M donde los departamentos son grandes y genéricos (mujer, hombre, etc.).

**Filtro de categorías no-producto**: a diferencia de H&M, acá el árbol trae categorías de primer
nivel que NO son electrodomésticos en oferta: el nodo raíz vacío ("Category"), "Garantía
Extendida" (pólizas), "Servicios" (confirmado en vivo: el primer resultado de una búsqueda de
prueba en "Refrigerador" fue "Visita y conexión refrigerador", un servicio de instalación, no un
producto) y "Repuestos" (piezas sueltas). Se excluyen por path (`_PATHS_EXCLUIDOS`) antes de
recorrer.

Catálogo chico: ~725 productos crudos sumando las ~45 categorías válidas (confirmado en vivo vía
el header `resources`), muy por debajo del tope de paginación de VTEX (2500, ver `_MAX_FROM`) —
se lee el 100% cada corrida, sin sortear ni truncar (mismo espíritu que PC Express/MyShop).

Precio/URL/imagen/producto_id: mismos campos que H&M (`items[].sellers[].commertialOffer`,
campo `link`, `items[].images[].imageUrl`, `productId`). Descuento real confirmado en vivo: 35 de
50 productos en la categoría "Refrigerador" (70% en esa muestra).

Imagen — mismo riesgo que H&M/Sodimac sin confirmar todavía: Fensa es marca de Electrolux, las
imágenes salen de un CDN separado (`electroluxcl.vteximg.com.br`), no del dominio del sitio. No
se suma preventivamente a `telegram_publisher._COMERCIOS_CON_CDN_BLOQUEADO` — esperar una corrida
real primero (mismo criterio que ABC/H&M).

Prueba end-to-end real (2026-08-24): 199 ofertas crudas (sin duplicados) en 45/45 categorías
leídas con éxito, descuento 7%-58% (promedio 32.6%) — sin productos de "Garantía Extendida"/
"Servicios"/"Repuestos" colados, el filtro de categorías funcionó. Volumen bajo (catálogo chico de
una marca propia), comparable a Mascotas/SuperZoo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.fensa")

_BASE_URL = "https://www.tiendafensa.cl"
_TREE_URL = f"{_BASE_URL}/api/catalog_system/pub/category/tree/50"
_SEARCH_URL = f"{_BASE_URL}/api/catalog_system/pub/products/search"

_PAGINA = 50  # tope real del endpoint (_to - _from <= 49)
_MAX_FROM = 2500  # tope duro de VTEX (ver fuentes.hm.listado) — ninguna categoría de Fensa se
# acerca a esto, se deja por consistencia/seguridad futura.

_PATHS_EXCLUIDOS = {"category", "garantia-extendida", "servicios", "repuestos"}

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
        "comercio": "Fensa",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_TREE_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_TREE_URL}")
    arbol = json.loads(respuesta.body)
    categorias = []
    for nodo in arbol:
        url = nodo.get("url") or ""
        path = url.removeprefix(_BASE_URL).strip("/")
        if path and path not in _PATHS_EXCLUIDOS:
            categorias.append(path)
    return categorias


async def _fetch_pagina(sesion, path: str, desde: int, hasta: int) -> tuple[list[dict], dict] | None:
    url = f"{_SEARCH_URL}/{path}?_from={desde}&_to={hasta}"
    respuesta = await sesion.get(url)
    if respuesta.status == 400:
        return None  # tope duro de VTEX (_from > 2500) — fin de lo leíble en esta categoría
    if respuesta.status not in (200, 206):
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    productos = json.loads(respuesta.body)
    items = [i for i in (_item_desde_producto(p) for p in productos) if i]
    return items, respuesta.headers


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, path: str) -> list[dict] | None:
    async with semaforo:
        try:
            resultado = await _fetch_pagina(sesion, path, 0, _PAGINA - 1)
        except Exception:
            log.exception("Falló la primera página de la categoría %s de Fensa, se omite", path)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if resultado is None:
        log.warning("Fensa: la categoría %s no devolvió nada en la primera página, se omite", path)
        return None
    items, headers = resultado
    total = _total_desde_headers(headers)
    if total is None:
        log.warning("Fensa: la categoría %s no trajo header 'resources', se toma solo la primera página", path)
        return items

    tope = min(total, _MAX_FROM + _PAGINA)
    todos = list(items)
    desde = _PAGINA
    while desde < tope:
        hasta = min(desde + _PAGINA - 1, tope - 1)
        async with semaforo:
            try:
                resultado = await _fetch_pagina(sesion, path, desde, hasta)
            except Exception:
                log.exception("Falló una página (%s-%s) de la categoría %s de Fensa, se omite el resto", desde, hasta, path)
                break
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
        if resultado is None:
            break  # tope duro de VTEX alcanzado
        pagina_items, _ = resultado
        todos.extend(pagina_items)
        desde += _PAGINA

    if total > tope:
        log.info("Fensa: categoría %s truncada por el tope de VTEX (%s de %s productos leídos)", path, tope, total)
    return todos


async def obtener_ofertas_fensa(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        categorias = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga del árbol de categorías de Fensa, se aborta")
        return [], 0

    if not categorias:
        log.error("Fensa: no se encontró ninguna categoría válida en el árbol")
        return [], 0

    log.info("Fensa: %s categorías encontradas, se leen todas", len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, path) for path in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for path, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de Fensa, se omite: %r", path, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("Fensa: no se detectó ninguna oferta en las %s categorías", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "Fensa: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
