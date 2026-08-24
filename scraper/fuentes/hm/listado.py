"""Scrapea las ofertas de H&M (cl.hm.com) — se suma al Retail General (ofertas_40/ofertas_vip vía
canal_para_descuento(), no a un canal especial — ver scraper/config.py).

VTEX. El frontend visible es VTEX FastStore (headless, Next.js — header
`X-VTEX-Janus-Router-Backend-App: faststore-prod-...`), pero el **API clásico de búsqueda de
VTEX** (`/api/catalog_system/pub/...`) sigue funcionando directo sobre el dominio propio, sin
Cloudflare ni challenge alguno — fetch plano responde 200/206 en todos los endpoints usados acá.
Patrón nuevo para el proyecto: en vez de recorrer un árbol de categorías hoja por hoja
(Sodimac/Hites/ABC) o resolver fichas de producto (clubdeperrosygatos), esta API expone
paginación real con total confirmado por request, así que se lee directo.

Cosecha de "categorías": se pide una sola vez `/api/catalog_system/pub/category/tree/50` (~1MB
JSON) y se usan los **7 departamentos de primer nivel tal cual** (mujer, hombre, niños, bebés,
home, sport, beauty_all) como las únicas fuentes a recorrer — confirmado en vivo que pedir el path
raíz de cada departamento (ej. `/api/.../search/mujer`, sin subcategoría) ya agrega TODO el
catálogo de ese departamento (3030 productos para mujer), a diferencia de intentar sumar
sub-nodos "ver-todo" sueltos (que resultaron parciales: `ninos/kids_kids/ver-todo` solo cubre una
subcategoría de niños, no las ~2031 del departamento completo). No hace falta bajar a las 2607
categorías hoja reales del árbol para nada.

Paginación: `?_from=N&_to=M` de a 50 productos (`_to - _from` ≤ 49, tope real del endpoint), el
total real de cada departamento viene en el header de respuesta `resources: N-M/TOTAL` de la
primera página. **Tope duro de VTEX confirmado en vivo**: `_from` no puede superar 2500
("Parameter _from can't be greater than 2500" en HTTP 400) — un departamento con más de ~2550
productos (mujer, 3030) queda leído solo hasta ese techo (~84%), limitación aceptada del lado de
la plataforma, no del scraper (mismo espíritu que el tope de 48/categoría de Hites).

Precio actual/normal salen de `items[].sellers[].commertialOffer` (`Price`/`ListPrice`) — un mismo
producto puede tener varios `items` (tallas) con precios distintos; se toma el de mayor descuento
real disponible (`Price < ListPrice`), mismo criterio "usar el precio más bajo" que el resto del
proyecto. URL sale ya absoluta del campo `link` a nivel de producto
(`https://cl.hm.com/<linkText>/p`). Imagen: `items[].images[].imageUrl` del item elegido. El
`producto_id` es el `productId` de VTEX (estable entre tallas del mismo producto, evita duplicar
por talla).

Imagen — riesgo de bloqueo con el fetcher de Telegram sin confirmar todavía: a diferencia de
ABC/Hites (imagen en el propio dominio del sitio), acá sale de un subdominio de CDN separado
(`hmchile.vteximg.com.br`/`hmchile.vtexassets.com`), mismo patrón de riesgo que tuvo Sodimac
(`media.sodimac.cl`). No se puede confirmar con un curl de prueba (el bloqueo, si existe, actúa
sobre el fingerprint/IP real de Telegram). Se decide NO sumarla preventivamente a
`telegram_publisher._COMERCIOS_CON_CDN_BLOQUEADO` — esperar una corrida real primero.

Prueba end-to-end real (2026-08-24): 260 ofertas crudas (sin duplicados) en 7/7 departamentos
leídos con éxito (~133 requests), descuento 12%-50% (promedio 36.7%). Volumen mucho más bajo que
Falabella/Ripley/ABC (fast-fashion, márgenes más ajustados, tasa de descuento real baja: ~4% de
~6700 productos leídos), pero comparable o mejor que Mascotas/SuperZoo — no es un problema de
equilibrio porque Retail General ya maneja volumen grande entre varias tiendas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.hm")

_BASE_URL = "https://cl.hm.com"
_TREE_URL = f"{_BASE_URL}/api/catalog_system/pub/category/tree/50"
_SEARCH_URL = f"{_BASE_URL}/api/catalog_system/pub/products/search"

_PAGINA = 50  # tope real del endpoint (_to - _from <= 49)
_MAX_FROM = 2500  # tope duro de VTEX confirmado en vivo ("_from can't be greater than 2500")

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
        "comercio": "H&M",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _departamentos_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_TREE_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_TREE_URL}")
    arbol = json.loads(respuesta.body)
    departamentos = []
    for nodo in arbol:
        url = nodo.get("url") or ""
        path = url.removeprefix(_BASE_URL).strip("/")
        if path:
            departamentos.append(path)
    return departamentos


async def _fetch_pagina(sesion, path: str, desde: int, hasta: int) -> tuple[list[dict], dict] | None:
    url = f"{_SEARCH_URL}/{path}?_from={desde}&_to={hasta}"
    respuesta = await sesion.get(url)
    if respuesta.status == 400:
        return None  # tope duro de VTEX (_from > 2500) — fin de lo leíble en este departamento
    if respuesta.status not in (200, 206):
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    productos = json.loads(respuesta.body)
    items = [i for i in (_item_desde_producto(p) for p in productos) if i]
    return items, respuesta.headers


async def _fetch_departamento(sesion, semaforo: asyncio.Semaphore, path: str) -> list[dict] | None:
    async with semaforo:
        try:
            resultado = await _fetch_pagina(sesion, path, 0, _PAGINA - 1)
        except Exception:
            log.exception("Falló la primera página del departamento %s de H&M, se omite", path)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if resultado is None:
        log.warning("H&M: el departamento %s no devolvió nada en la primera página, se omite", path)
        return None
    items, headers = resultado
    total = _total_desde_headers(headers)
    if total is None:
        log.warning("H&M: el departamento %s no trajo header 'resources', se toma solo la primera página", path)
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
                log.exception("Falló una página (%s-%s) del departamento %s de H&M, se omite el resto", desde, hasta, path)
                break
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
        if resultado is None:
            break  # tope duro de VTEX alcanzado
        pagina_items, _ = resultado
        todos.extend(pagina_items)
        desde += _PAGINA

    if total > tope:
        log.info("H&M: departamento %s truncado por el tope de VTEX (%s de %s productos leídos)", path, tope, total)
    return todos


async def obtener_ofertas_hm(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, departamentos leídos sin error — 0 significa que no
    se pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        departamentos = await _departamentos_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga del árbol de categorías de H&M, se aborta")
        return [], 0

    if not departamentos:
        log.error("H&M: no se encontró ningún departamento en el árbol de categorías")
        return [], 0

    log.info("H&M: %s departamentos encontrados, se leen todos", len(departamentos))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_departamento(sesion, semaforo, path) for path in departamentos
    ), return_exceptions=True)

    departamentos_ok = 0
    todos: list[dict] = []
    for path, resultado in zip(departamentos, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en el departamento %s de H&M, se omite: %r", path, resultado)
            continue
        if resultado is None:
            continue
        departamentos_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("H&M: no se detectó ninguna oferta en los %s departamentos", len(departamentos))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "H&M: %s ofertas crudas (sin duplicados) en %s/%s departamentos leídos con éxito",
        len(items), departamentos_ok, len(departamentos),
    )
    return items, departamentos_ok
