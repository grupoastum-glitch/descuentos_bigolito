"""Scrapea las ofertas de Ripley Chile (simple.ripley.cl).

Es Next.js SSR igual que Falabella (`<script id="__NEXT_DATA__">`, ver
`fuentes.falabella.parsing.extraer_next_data`, reusada acá tal cual). La raíz del catálogo de
ofertas es `/outlet` (facet real de descuentos, pese al nombre — no es solo la sección
"outlet"): `props.pageProps.findabilityProps.data` trae ahí el listado de productos y, en
`props.pageProps.catalog.categories`, el árbol completo de subcategorías de esa vitrina.

Los query params `offset`/`limit` de la URL NO paginan del lado del servidor (comprobado: pedir
`?offset=48` devuelve los mismos productos que `offset=0` — el árbol `findabilityProps.queryParams`
que Ripley realmente usó para pedir los datos ignora el offset pedido). En vez de pelear con eso,
se usa el mismo approach que `fuentes.xiaomi.listado`: recorrer las categorías hoja del árbol
(`/outlet/<categoria>/<subcategoria>`) y juntar los productos de cada una — cada hoja ya viene sin
paginar (≤48-58 productos).

El `discount` que Ripley reporta por producto no siempre es confiable: en productos con
`"hasInitialPrice": true` se vio un `discount` que no correspondía a `price`/`oldPrice` (ej. 33%
publicado vs. ~20% real comparando ambos precios) — probablemente calculado contra un precio de
referencia distinto al `oldPrice` mostrado. Por eso acá el % se recalcula siempre desde
precio_actual/precio_normal, igual que hace Xiaomi con price_min/market_price_min.

El JSON de cada producto tampoco trae la URL del producto — solo aparece en el `href` que Ripley
renderiza en el HTML, con forma `/<slug>-<parentProductID en minúscula>?<params de tracking>`. Se
extrae de ahí en vez de reconstruirla a mano, para no arriesgar mismatches de slugificación.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import config
from fuentes.falabella.parsing import extraer_next_data

log = logging.getLogger("scraper.fuentes.ripley")

_BASE_URL = "https://simple.ripley.cl"
_CATALOGO_URL = f"{_BASE_URL}/outlet"

# une el slug renderizado con el id numérico (sku o parentProductID) que Ripley le agrega al
# final del href, justo antes de la query de tracking (?s=...&pos=...&cat=...)
_HREF_PRODUCTO_RE = re.compile(r'href="(/[a-z0-9%-]+-(\d{6,})[pP])(?:\?[^"]*)?"', re.I)


def _urls_por_id(html: str) -> dict[str, str]:
    urls: dict[str, str] = {}
    for coincidencia in _HREF_PRODUCTO_RE.finditer(html):
        ruta, id_numerico = coincidencia.groups()
        urls.setdefault(id_numerico, f"{_BASE_URL}{ruta}")
    return urls


def _categorias_hoja(categorias: list[dict]) -> list[str]:
    hojas: list[str] = []

    def _recorrer(nodos: list[dict]) -> None:
        for nodo in nodos:
            hijos = nodo.get("categories") or []
            if hijos:
                _recorrer(hijos)
            else:
                parents = nodo.get("parents") or []
                if parents:
                    hojas.append("/".join(parents))

    _recorrer(categorias)
    return hojas


def _parsear_precio_clp(texto: str | None) -> int | None:
    """'$579.990' -> 579990. None/vacío -> None (sin precio de referencia utilizable)."""
    if not texto:
        return None
    digitos = re.sub(r"[^\d]", "", texto)
    return int(digitos) if digitos else None


def _items_desde_productos(html: str, productos: list[dict]) -> list[dict]:
    urls = _urls_por_id(html)
    items = []
    for producto in productos:
        sku = producto.get("sku")
        if not sku:
            continue
        id_href = str(producto.get("parentProductID") or sku).rstrip("Pp")
        url = urls.get(id_href) or urls.get(str(sku))
        if not url:
            continue  # no se pudo ubicar el link real del producto, se prefiere omitir a inventar uno

        precio_actual = producto.get("priceNumber")
        precio_normal = _parsear_precio_clp(producto.get("oldPrice"))
        if not isinstance(precio_actual, int) or not precio_normal or precio_normal <= 0:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        items.append({
            "producto_id": str(sku),
            "titulo": producto.get("name") or "",
            "marca": producto.get("brand"),
            "url": url,
            "comercio": "Ripley",
            "imagen": producto.get("primaryImage"),
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, ruta: str) -> list[dict] | None:
    url = f"{_BASE_URL}/{ruta}"
    async with semaforo:
        try:
            respuesta = await sesion.get(url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {url}")
            html = respuesta.body.decode("utf-8", errors="replace")
            next_data = extraer_next_data(html)
            productos = next_data["props"]["pageProps"]["findabilityProps"]["data"]["products"]
            resultado = _items_desde_productos(html, productos)
        except Exception:
            log.exception("Falló %s del catálogo de ofertas de Ripley, se omite", url)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
    return resultado


async def _calentar_sesion(sesion) -> None:
    """GET a la home antes de pedir /outlet, para que cualquier cookie de tienda/región que
    Ripley setee ahí viaje también en la request al catálogo (mismo motivo que
    fuentes.falabella, ver main._calentar_sesion — acá no hace falta reusar el HTML)."""
    respuesta = await sesion.get(_BASE_URL + "/")
    log.info("Warm-up: %s -> %s (status %s)", _BASE_URL + "/", respuesta.url, respuesta.status)


async def _fetch_con_reintento(sesion, url: str):
    """Un reintento (con una pausa corta) para el único fetch cuya falla aborta toda la
    tienda — un bloqueo/timeout transitorio no debería tirar la corrida completa (ver
    incidencia 2026-08-12: Ripley devolvió 403 en /outlet y se recuperó solo, minutos después,
    sin ningún cambio de nuestro lado)."""
    for intento in range(2):
        try:
            respuesta = await sesion.get(url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {url}")
            return respuesta
        except Exception:
            if intento == 1:
                raise
            log.warning("Falló %s (intento 1/2), se reintenta en %ss", url, config.REINTENTO_FETCH_INICIAL_SEGUNDOS)
            await asyncio.sleep(config.REINTENTO_FETCH_INICIAL_SEGUNDOS)


async def obtener_ofertas_ripley(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, categorías leídas sin error — 0 significa que no se pudo sacar
    nada de esta fuente). Mismo contrato que fuentes.falabella.listado.obtener_ofertas_listado
    y fuentes.xiaomi.listado.obtener_ofertas_xiaomi."""
    try:
        await _calentar_sesion(sesion)
        respuesta = await _fetch_con_reintento(sesion, _CATALOGO_URL)
        html = respuesta.body.decode("utf-8", errors="replace")
        next_data = extraer_next_data(html)
        categorias = next_data["props"]["pageProps"]["catalog"]["categories"]
        hojas = _categorias_hoja(categorias)
    except Exception:
        log.exception("Falló la carga del árbol de categorías de ofertas de Ripley (%s), se aborta", _CATALOGO_URL)
        return [], 0

    log.info("Ripley: %s categorías hoja de ofertas encontradas", len(hojas))
    if not hojas:
        log.error("Ripley: el árbol de categorías de /outlet no tiene ninguna hoja utilizable")
        return [], 0

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: mismo motivo que en Falabella/Xiaomi — una excepción no anticipada
    # en una sola categoría no debe tumbar la corrida completa (incidencia 2026-08-11).
    resultados = await asyncio.gather(*(
        _fetch_categoria(sesion, semaforo, ruta) for ruta in hojas
    ), return_exceptions=True)

    items: list[dict] = []
    categorias_ok = 0
    for ruta, resultado in zip(hojas, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en %s del catálogo de Ripley, se omite: %r", ruta, resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        items.extend(resultado)

    log.info(
        "Ripley: %s ofertas crudas en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(hojas),
    )
    return items, categorias_ok
