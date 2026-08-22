"""Scrapea las ofertas de SuperZoo (superzoo.cl) — primera tienda del rubro Mascotas.

Salesforce Commerce Cloud / Demandware (primera tienda del proyecto sobre esta plataforma) —
confirmado por las cookies `dwac_`/`dwanonymous_`/`dwsid` y las rutas
`/on/demandware.store/Sites-SuperZoo-Site/...`. Detrás de Cloudflare, pero responde igual con un
fetch plano, sin challenge.

Categorías reales anidadas bajo `/gato/...` y `/perro/...` (ej. `/gato/alimentos/alimento-seco`),
cosechadas del menú de la home igual que Sodimac/Sipoonline — se filtran las hojas (ninguna otra
ruta cosechada la tiene como prefijo). Solo ~42 categorías hoja en total, catálogo chico: se piden
todas cada corrida, sin sorteo.

Hallazgo clave: cada categoría hoja se lee completa en un solo request al endpoint AJAX interno
que usa el propio sitio para su paginación (`Search-UpdateGrid?cgid=<id>&start=0&sz=<N>`, sin
sesión/cookie previa). El `cgid` real (ej. `alimentos-seco-gato`) NO coincide con el slug de la
URL amigable (`alimento-seco`) ni siempre con el `data-cat` del primer producto (ese atributo es
por-producto y puede pertenecer a otra sub-categoría si la página mezcla varias) — la forma
confiable de resolverlo es el link `Search-UpdateGrid?cgid=...` que el propio dropdown de orden
trae embebido en cualquier página de categoría. El total real sale del texto "N Resultados". Se
usa `sz` = exactamente ese total (nunca un número inflado "por si acaso": probado que pedir un
`sz` mucho mayor al total real devuelve productos repetidos/de otras categorías como relleno, en
vez de fallar limpio).

No alcanza con las categorías de descuento curadas del sitio (`super-ofertas`,
`landing-liquidacion`, `mas-x-menos`, `precios-bomba`) — se confirmó un producto con descuento
real en una categoría normal que no aparece en `super-ofertas` — así que se recorre el árbol de
categorías igual que Easy/MyShop, no las curadas.

Cada tarjeta (`.product-tile`) trae como máximo 2 precios con valor numérico ya limpio en el
atributo `content` (sin parsear "$" ni puntos): el precio normal solo aparece (envuelto en
`<del>`) si el producto está realmente en oferta — a diferencia de Sipoonline, acá si es una señal
genuina de descuento puntual. El producto_id sale del sufijo `_m.html` de la URL.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.superzoo")

_BASE_URL = "https://www.superzoo.cl"
_HOME_URL = f"{_BASE_URL}/"
_AJAX_URL = f"{_BASE_URL}/on/demandware.store/Sites-SuperZoo-Site/es_CL/Search-UpdateGrid"

_CATEGORIA_RE = re.compile(r'href="https://www\.superzoo\.cl(/(?:gato|perro)/[a-zA-Z0-9/-]+)"')
_TOTAL_RE = re.compile(r"(\d+)\s+Resultados", re.I)
_CGID_RE = re.compile(r"Search-UpdateGrid\?cgid=([a-zA-Z0-9_-]+)")
_PRODUCTO_ID_RE = re.compile(r"/(\d+)_m\.html")


def _categorias_hoja(paths: set[str]) -> list[str]:
    return [p for p in paths if not any(o != p and o.startswith(p + "/") for o in paths)]


def _item_desde_card(card) -> dict | None:
    viejo = card.css("del .value")
    if not viejo:
        return None  # sin precio tachado, este producto no está en oferta real
    nuevo = card.css(".sales .value")
    if not nuevo:
        return None

    try:
        precio_normal = int(viejo[0].attrib.get("content"))
        precio_actual = int(nuevo[0].attrib.get("content"))
    except (TypeError, ValueError):
        return None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    link_nodo = card.css(".pdp-link a")
    if not link_nodo:
        return None
    url = link_nodo[0].attrib.get("href")
    if not url:
        return None
    m = _PRODUCTO_ID_RE.search(url)
    if not m:
        return None
    titulo_nodo = link_nodo[0].css("h2")
    titulo = titulo_nodo[0].get_all_text(strip=True) if titulo_nodo else link_nodo[0].get_all_text(strip=True)

    marca_nodo = card.css(".product-brand")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css(".tile-image")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None
    if imagen and imagen.startswith("/"):
        imagen = f"{_BASE_URL}{imagen}"

    return {
        "producto_id": m.group(1),
        "titulo": titulo,
        "marca": marca,
        "url": f"{_BASE_URL}{url}",
        "comercio": "SuperZoo",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_HOME_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_HOME_URL}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    paths = {m.group(1) for m in _CATEGORIA_RE.finditer(html_)}
    return _categorias_hoja(paths)


async def _resolver_categoria(sesion, path: str) -> tuple[str, int] | None:
    """Visita la página amigable de la categoría para sacar su `cgid` real (no siempre coincide
    con el slug de la URL) y el total real de productos. Devuelve (cgid, total) o None si la
    categoría está vacía / no se pudo resolver."""
    url = f"{_BASE_URL}{path}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html_ = respuesta.body.decode("utf-8", errors="replace")

    m_cgid = _CGID_RE.search(html_)
    m_total = _TOTAL_RE.search(html_)
    if not m_cgid or not m_total:
        return None
    total = int(m_total.group(1))
    if total <= 0:
        return None
    return m_cgid.group(1), total


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, path: str) -> list[dict] | None:
    async with semaforo:
        try:
            resuelto = await _resolver_categoria(sesion, path)
        except Exception:
            log.exception("Falló resolver la categoría %s de SuperZoo, se omite", path)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if resuelto is None:
        log.warning("SuperZoo: la categoría %s no tiene cgid/total utilizable, se omite", path)
        return None
    cgid, total = resuelto

    async with semaforo:
        try:
            grid_url = f"{_AJAX_URL}?cgid={cgid}&start=0&sz={total}"
            respuesta = await sesion.get(grid_url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {grid_url}")
            items = []
            for card in respuesta.css(".product-tile"):
                item = _item_desde_card(card)
                if item:
                    items.append(item)
        except Exception:
            log.exception("Falló la grilla de la categoría %s (cgid=%s) de SuperZoo, se omite", path, cgid)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_superzoo(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        categorias = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga de la home de SuperZoo para cosechar categorías, se aborta")
        return [], 0

    if not categorias:
        log.error("SuperZoo: no se encontró ninguna categoría hoja en la home")
        return [], 0

    log.info("SuperZoo: %s categorías hoja encontradas, se piden todas", len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, path) for path in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for path, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de SuperZoo, se omite: %r", path, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("SuperZoo: no se detectó ninguna oferta en las %s categorías", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "SuperZoo: %s ofertas crudas (sin duplicados) desde %s/%s categorías",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
