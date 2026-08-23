"""Scrapea las ofertas de Hites (hites.com) — se suma al Retail General (ofertas_40/ofertas_vip
vía canal_para_descuento(), no a un canal especial — ver scraper/config.py).

Salesforce Commerce Cloud / Demandware, mismo motor que `fuentes.superzoo.listado` (confirmado por
`Sites-HITES-Site` en las rutas y las cookies `dwac_`/`dwanonymous_`/`dwsid`). Detrás de
Cloudflare, pero responde 200 con fetch plano, sin challenge.

A diferencia de SuperZoo (tienda de mascotas chica, 42 categorías), Hites es una tienda por
departamento grande: 827 categorías hoja reales confirmadas en vivo en el menú de la home (18
departamentos — tecnología, electro-hogar, muebles, hombre, mujer, accesorios, deportes,
construcción, etc.). Sin un hub único de ofertas cross-departamento — se muestrean
`_MAX_CATEGORIAS` al azar por corrida (mismo orden de magnitud que Sodimac, ~800 categorías),
igual que Sodimac/Easy/MyShop cuando el árbol es demasiado grande para recorrerlo entero.

Mismo mecanismo AJAX que SuperZoo (`Search-UpdateGrid?cgid=<id>&start=0&sz=<N>`), el `cgid` real
se resuelve visitando la página amigable de la categoría y buscando el link
`Search-UpdateGrid?cgid=...` que trae embebido. A diferencia de SuperZoo, **no existe un texto de
total real** ("N Resultados") en este sitio — en cambio el endpoint devuelve un tope fijo de
exactamente 48 productos por request (confirmado en 10 categorías reales), y pedir `start=48`/`96`
siempre devolvió 0 productos adicionales en esas mismas categorías. Se adopta el mismo criterio
que Bestmart/Dust2 (una sola lectura por fuente, sin resolver un total aparte): un solo request
`sz=48` por categoría. Limitación aceptada: una categoría con más de 48 productos reales quedaría
con su cola permanentemente sin ver (mismo tipo de techo ya aceptado en otras fuentes).

Precio/descuento salen de un JSON limpio embebido en cada tarjeta (atributo
`data-gtmselectitem`, HTML-entity-encoded): `item.price` (precio actual) e `item.discount` (monto
de descuento en pesos, 0 si no hay oferta real) → `precio_normal = price + discount`. Confirmado
contra un producto real (price=36990, discount=9000 → normal=45990, coincide con el precio tachado
visible). URL del producto sale del `href` del link de imagen de la tarjeta; imagen, del `src` del
mismo bloque (dominio propio `www.hites.com/dw/image/...`, confirmado sin bloqueo para el fetcher
de Telegram).

Volumen de descuento real altísimo: 410/480 (85.4%) en una muestra de 10 categorías reales al
azar — más alto que PetHome/NovaPet. A diferencia de Mascotas, acá no es un problema de equilibrio
de canal: Retail General ya reparte volumen grande y estable entre Falabella/Ripley/Paris.

Hites es un marketplace (`marketplace` trae el nombre del vendedor tercero, ej. "Riqui Spa") —
no requiere manejo especial, mismo caso que Falabella/Ripley/Paris.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.hites")

_BASE_URL = "https://www.hites.com"
_HOME_URL = f"{_BASE_URL}/"
_AJAX_URL = f"{_BASE_URL}/on/demandware.store/Sites-HITES-Site/es_CL/Search-UpdateGrid"

_PRODUCTOS_POR_CATEGORIA = 48  # tope real del endpoint, confirmado en vivo (start=48/96 no trae
# productos adicionales en ninguna de las 10 categorías reales probadas)
_MAX_CATEGORIAS = 60  # ~7% de las 827 categorías hoja reales — mismo orden de magnitud que
# Sodimac (60 de ~800). Revisar fallos_tiendas.json los primeros días.

_CATEGORIA_RE = re.compile(r'href="(/[a-zA-Z0-9/-]+/)"')
_CGID_RE = re.compile(r"Search-UpdateGrid\?cgid=([a-zA-Z0-9_-]+)")
_GTM_RE = re.compile(r'data-gtmselectitem="([^"]*)"')
_IMAGEN_RE = re.compile(r'class="image-item[^"]*"\s+href="([^"]+)">\s*<img[^>]*src="([^"]+)"')

# rutas de la home que no son categorías de catálogo — quedarían como "hoja" igual que una
# categoría real porque ningún otro path las tiene como prefijo (mismo criterio que MyShop).
_NO_CATALOGO_PREFIJOS = (
    "/cuenta", "/login", "/carro", "/checkout", "/ayuda", "/contacto", "/nosotros",
    "/terminos", "/privacidad", "/blog", "/tienda", "/sucursal", "/servicio-al-cliente",
    "/seguimiento", "/especiales", "/llega-manana", "/retira-en-3-horas", "/nuevas-categorias",
)


def _categorias_hoja(paths: set[str]) -> list[str]:
    paths = {p for p in paths if not any(p.startswith(pre) for pre in _NO_CATALOGO_PREFIJOS)}
    return [p for p in paths if not any(o != p and o.startswith(p[:-1] + "/") for o in paths)]


def _item_desde_tarjeta(gtm_json: str, bloque: str) -> dict | None:
    try:
        datos = json.loads(html.unescape(gtm_json))
    except (json.JSONDecodeError, ValueError):
        return None
    item = datos.get("item") or {}

    producto_id = item.get("item_id")
    titulo = item.get("item_name")
    if not producto_id or not titulo:
        return None

    try:
        precio_actual = float(item["price"])
        descuento_monto = float(item.get("discount") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if not precio_actual or descuento_monto <= 0:
        return None
    precio_normal = precio_actual + descuento_monto
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    m_link = _IMAGEN_RE.search(bloque)
    if not m_link:
        return None
    url, imagen = (html.unescape(g) for g in m_link.groups())
    if url.startswith("/"):
        url = f"{_BASE_URL}{url}"

    return {
        "producto_id": str(producto_id),
        "titulo": html.unescape(titulo),
        "marca": item.get("item_brand") or None,
        "url": url,
        "comercio": "Hites",
        "imagen": imagen,
        "precio_actual": round(precio_actual),
        "precio_normal": round(precio_normal),
        "descuento_pct": descuento_pct,
    }


def _items_desde_grid(html_: str) -> list[dict]:
    matches = list(_GTM_RE.finditer(html_))
    items = []
    for i, m in enumerate(matches):
        # el link/imagen de la tarjeta vienen DESPUÉS del atributo data-gtmselectitem (que está en
        # la apertura del propio div.product-tile) — la ventana va desde el final de este match
        # hasta el próximo, así nunca cruza hacia la tarjeta anterior ni la siguiente.
        inicio = m.end()
        fin = matches[i + 1].start() if i + 1 < len(matches) else len(html_)
        bloque = html_[inicio:fin]
        item = _item_desde_tarjeta(m.group(1), bloque)
        if item:
            items.append(item)
    return items


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_HOME_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_HOME_URL}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    paths = {m.group(1) for m in _CATEGORIA_RE.finditer(html_)}
    return _categorias_hoja(paths)


async def _resolver_cgid(sesion, path: str) -> str | None:
    url = f"{_BASE_URL}{path}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    m = _CGID_RE.search(html_)
    return m.group(1) if m else None


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, path: str) -> list[dict] | None:
    async with semaforo:
        try:
            cgid = await _resolver_cgid(sesion, path)
        except Exception:
            log.exception("Falló resolver la categoría %s de Hites, se omite", path)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if cgid is None:
        log.warning("Hites: la categoría %s no tiene cgid resoluble, se omite", path)
        return None

    async with semaforo:
        try:
            grid_url = f"{_AJAX_URL}?cgid={cgid}&start=0&sz={_PRODUCTOS_POR_CATEGORIA}"
            respuesta = await sesion.get(grid_url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {grid_url}")
            html_ = respuesta.body.decode("utf-8", errors="replace")
            items = _items_desde_grid(html_)
        except Exception:
            log.exception("Falló la grilla de la categoría %s (cgid=%s) de Hites, se omite", path, cgid)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_hites(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        categorias_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga de la home de Hites para cosechar categorías, se aborta")
        return [], 0

    if not categorias_disponibles:
        log.error("Hites: no se encontró ninguna categoría hoja en la home")
        return [], 0

    categorias = random.sample(categorias_disponibles, min(_MAX_CATEGORIAS, len(categorias_disponibles)))
    log.info("Hites: %s categorías hoja disponibles, muestreando %s", len(categorias_disponibles), len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, path) for path in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for path, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de Hites, se omite: %r", path, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("Hites: no se detectó ninguna oferta en las %s categorías muestreadas", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "Hites: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
