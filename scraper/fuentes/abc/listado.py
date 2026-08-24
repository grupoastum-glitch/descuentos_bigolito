"""Scrapea las ofertas de ABC (abc.cl, fusión de La Polar + Abcdin) — se suma al Retail General
(ofertas_40/ofertas_vip vía canal_para_descuento(), no a un canal especial — ver scraper/config.py).

Salesforce Commerce Cloud / Demandware, mismo motor que `fuentes.hites.listado` y
`fuentes.superzoo.listado` (confirmado por `Sites-Abc-Site` en las rutas y las cookies
`dwac_`/`cqcid`/`sid`). Detrás de Cloudflare, pero responde 200 con fetch plano, sin challenge.

Tienda por departamento grande: 454 categorías hoja reales confirmadas en vivo en el menú de la
home (13 departamentos — belleza, deportes, dormitorio, hogar, hombre, linea-blanca, muebles,
mujer, ninos, otras-lineas, tecnologia, zapatillas, zapatos). Se excluyen del árbol las rutas de
cuenta/checkout (únicas con mayúscula inicial en este sitio: `/Bolsa/`, `/Iniciar-Sesion/`,
`/Seguimiento-Despacho/`) y las páginas "ver-todo-*" (alias que redirige 301 a la categoría padre
— igual cobertura que recorrer sus hojas, contarla como categoría aparte sería leer productos
duplicados). Sin un hub único de ofertas cross-departamento — se muestrean `_MAX_CATEGORIAS` al
azar por corrida, mismo orden de magnitud que Sodimac/Hites cuando el árbol es grande.

Mismo mecanismo AJAX que Hites/SuperZoo (`Search-UpdateGrid?cgid=<id>&start=0&sz=<N>`), pero el
`cgid` real casi nunca coincide con el slug de la URL amigable (confirmado: slug
`botas-botines` → cgid real `botas-y-botines`) — se resuelve visitando la página amigable de la
categoría y buscando el link `Search-UpdateGrid?cgid=...` que trae embebido, igual que
Hites/SuperZoo. A diferencia de Hites (tope fijo de 48 por request) y de SuperZoo (hay que resolver
un total real vía texto "N Resultados"), acá el endpoint no tiene tope visible ni texto de total:
probado con `sz=999` en una categoría de 88 productos reales y devolvió los 88 completos sin
truncar — alcanza un solo request por categoría con un `sz` generoso (300).

Precio actual/normal salen de las tarjetas de producto (`.product-tile__item`): el precio "Internet"
(`.js-internet-price .price-value`, atributo `data-value`) siempre está presente; el precio
"Normal" tachado (`.js-normal-price .price-value`) solo aparece cuando el producto está realmente
en oferta (mismo criterio de presencia = descuento real que SuperZoo) — confirmado en una
categoría real: 14 de 88 tarjetas con precio normal. URL/título salen del link `.pdp-link a`
(atributo `href` + texto), marca de `.brand-name`, imagen del `src` de `.tile-image` (ya viene con
dominio absoluto `www.abc.cl/dw/image/...` — mismo dominio que el sitio, no un CDN aparte, sin
bloqueo confirmado para el fetcher de Telegram, mismo caso que Hites). El product_id sale del
sufijo numérico de la URL (`/<slug>/<id>.html`).

Prueba end-to-end real (2026-08-24): 3308 ofertas crudas (sin duplicados) en 54/60 categorías
muestreadas, descuento 1%-87% (promedio 43.1%) — volumen mucho más alto que Hites/Sodimac, más
cercano a Falabella/Ripley/Paris. No es un problema de equilibrio de canal: Retail General ya
maneja volumen grande y estable entre esas tiendas.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.abc")

_BASE_URL = "https://www.abc.cl"
_HOME_URL = f"{_BASE_URL}/"
_AJAX_URL = f"{_BASE_URL}/on/demandware.store/Sites-Abc-Site/es_CL/Search-UpdateGrid"

_PRODUCTOS_POR_CATEGORIA = 300  # sz generoso: probado sz=999 en una categoría de 88 productos
# reales y devolvió los 88 completos sin truncar — no hay tope de página visible en este sitio.
_MAX_CATEGORIAS = 60  # ~13% de las 454 categorías hoja reales — mismo orden de magnitud que
# Sodimac/Hites (60 de ~800/827). Revisar fallos_tiendas.json los primeros días.

_CATEGORIA_RE = re.compile(r'href="(/[a-zA-Z0-9/-]+/)"')
_CGID_RE = re.compile(r"Search-UpdateGrid\?cgid=([a-zA-Z0-9_-]+)")
_PRODUCTO_ID_RE = re.compile(r"/(\d+)\.html")


def _categorias_hoja(paths: set[str]) -> list[str]:
    # rutas de cuenta/checkout: únicas con mayúscula inicial en este sitio (`/Bolsa/`,
    # `/Iniciar-Sesion/`, `/Seguimiento-Despacho/`). "ver-todo-*" es un alias que redirige a la
    # categoría padre — se excluye para no leer productos duplicados.
    paths = {p for p in paths if not p[1:2].isupper() and "ver-todo" not in p}
    return [p for p in paths if not any(o != p and o.startswith(p[:-1] + "/") for o in paths)]


def _item_desde_card(card) -> dict | None:
    actual_nodo = card.css(".js-internet-price .price-value")
    if not actual_nodo:
        return None
    normal_nodo = card.css(".js-normal-price .price-value")
    if not normal_nodo:
        return None  # sin precio "Normal" tachado, este producto no está en oferta real

    try:
        precio_actual = round(float(actual_nodo[0].attrib.get("data-value")))
        precio_normal = round(float(normal_nodo[0].attrib.get("data-value")))
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
    titulo = link_nodo[0].attrib.get("data-product-name") or link_nodo[0].get_all_text(strip=True)
    if url.startswith("/"):
        url = f"{_BASE_URL}{url}"

    marca_nodo = card.css(".brand-name")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css(".tile-image")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None
    if imagen and imagen.startswith("/"):
        imagen = f"{_BASE_URL}{imagen}"

    return {
        "producto_id": m.group(1),
        "titulo": titulo,
        "marca": marca,
        "url": url,
        "comercio": "ABC",
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
            log.exception("Falló resolver la categoría %s de ABC, se omite", path)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if cgid is None:
        log.warning("ABC: la categoría %s no tiene cgid resoluble, se omite", path)
        return None

    async with semaforo:
        try:
            grid_url = f"{_AJAX_URL}?cgid={cgid}&start=0&sz={_PRODUCTOS_POR_CATEGORIA}"
            respuesta = await sesion.get(grid_url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {grid_url}")
            items = []
            for card in respuesta.css(".product-tile__item"):
                item = _item_desde_card(card)
                if item:
                    items.append(item)
        except Exception:
            log.exception("Falló la grilla de la categoría %s (cgid=%s) de ABC, se omite", path, cgid)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_abc(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, categorías leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    try:
        categorias_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga de la home de ABC para cosechar categorías, se aborta")
        return [], 0

    if not categorias_disponibles:
        log.error("ABC: no se encontró ninguna categoría hoja en la home")
        return [], 0

    categorias = random.sample(categorias_disponibles, min(_MAX_CATEGORIAS, len(categorias_disponibles)))
    log.info("ABC: %s categorías hoja disponibles, muestreando %s", len(categorias_disponibles), len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, path) for path in categorias
    ), return_exceptions=True)

    categorias_ok = 0
    todos: list[dict] = []
    for path, resultado in zip(categorias, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en la categoría %s de ABC, se omite: %r", path, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("ABC: no se detectó ninguna oferta en las %s categorías muestreadas", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "ABC: %s ofertas crudas (sin duplicados) en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok
