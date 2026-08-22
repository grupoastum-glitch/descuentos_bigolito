"""Scrapea las ofertas de Club de Perros y Gatos (clubdeperrosygatos.cl) — segunda tienda del
rubro Mascotas.

WordPress + WooCommerce, tema Woodmart, detrás de Cloudflare — pero un fetch plano responde 200
con contenido real (`cf-cache-status: DYNAMIC`, sin challenge JS), a diferencia de Ripley/WePlay.
**Sin Store API pública** (a diferencia de Dust2.gg): `/wp-json/wc/store/v1/products` devuelve 403,
hay que scrapear HTML.

`/tienda/` es el catálogo completo renderizado server-side (a diferencia de `/categorias/<slug>/`,
que carga distinto vía JS — no usar esa ruta). Soporta `?per_page=100` (tope real confirmado) +
paginación `?paged=N`. Confirmado en vivo: **1695 productos, 17 páginas** — volumen chico, se leen
todas cada corrida, sin sorteo (mismo criterio que Sparta/GymPro/Easy).

El tema oculta el precio tachado en la grilla (`hide-larger-price`, confirmado en el body): cada
tarjeta (`div.product-grid-item[data-id]`) trae el precio actual, pero **no** el precio normal
cuando el producto está en oferta — solo un badge `span.onsale.product-label` como bandera de
candidato. El precio normal real vive en la ficha del producto individual, en un bloque JSON-LD de
Yoast SEO (`script.yoast-schema-graph--woo`): `@graph[].offers[].priceSpecification` trae una
entrada sin `priceType` (precio actual) y otra con `priceType` terminado en `ListPrice` (precio
normal) — confirmado en vivo: 5980 vs 8970 (33%). Si no aparece la entrada ListPrice, el badge
"Oferta" es un falso positivo (ej. promo "Pack 3x2" sin baja de precio real) y se descarta.

Optimización para productos variables (`product-type-variable`): la propia tarjeta trae un
`data-product_variations` con el JSON de cada variación (`display_price`/`display_regular_price`)
— si alguna variación ya muestra descuento ahí, se usa directo sin visitar la ficha. Si no, cae al
mismo camino que los simples: se visita la ficha del producto.

El volumen real de requests por corrida lo dominan las fichas de detalle (candidatos con badge
"Oferta"), no las 17 páginas de listado — confirmado en vivo: ~265 candidatos en todo el catálogo.

Imágenes (`wp-content/uploads/...`, mismo dominio) confirmadas SIN bloqueo para el fetcher de
Telegram (a diferencia de Sparta/Sodimac) — no hace falta sumarla a
`telegram_publisher._COMERCIOS_CON_CDN_BLOQUEADO`.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.clubdeperrosygatos")

_BASE_URL = "https://www.clubdeperrosygatos.cl"
_TIENDA_URL = f"{_BASE_URL}/tienda/"

_PRODUCTOS_POR_PAGINA = 100  # tope real del catálogo, confirmado en vivo

_TOTAL_RE = re.compile(r"de\s*([\d.,]+)\s*resultados", re.I)
_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json" class="[^"]*yoast-schema-graph--woo[^"]*">(.*?)</script>',
    re.S,
)


def _total_paginas(html_: str) -> int:
    m = _TOTAL_RE.search(html_)
    if not m:
        return 1
    total = int(m.group(1).replace(".", "").replace(",", ""))
    return max(1, math.ceil(total / _PRODUCTOS_POR_PAGINA))


def _precio_desde_variaciones(card) -> tuple[int, int] | None:
    """Para productos variables: busca en el JSON embebido de sus variaciones alguna con precio
    de oferta real (display_price < display_regular_price) y devuelve la de menor precio. None si
    no hay JSON de variaciones o ninguna variación muestra descuento ahí."""
    form_nodo = card.css("form.variations_form")
    if not form_nodo:
        return None
    crudo = form_nodo[0].attrib.get("data-product_variations")
    if not crudo:
        return None
    try:
        variaciones = json.loads(html.unescape(crudo))
    except (json.JSONDecodeError, ValueError):
        return None

    mejor: tuple[int, int] | None = None
    for variacion in variaciones:
        try:
            actual = float(variacion["display_price"])
            normal = float(variacion["display_regular_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if normal <= actual:
            continue
        if mejor is None or actual < mejor[0]:
            mejor = (round(actual), round(normal))
    return mejor


def _item_desde_card(card) -> dict | None:
    producto_id = card.attrib.get("data-id")
    if not producto_id:
        return None

    if not card.css("span.onsale.product-label"):
        return None  # sin badge "Oferta", no es candidato

    link_nodo = card.css("a.product-image-link")
    if not link_nodo:
        return None
    url = link_nodo[0].attrib.get("href")
    if not url:
        return None
    imagen_nodo = link_nodo[0].css("img")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    clases = card.attrib.get("class") or ""
    precios = _precio_desde_variaciones(card) if "product-type-variable" in clases else None

    return {
        "producto_id": str(producto_id),
        "titulo": None,  # se completa al resolver la ficha, o queda vacío si ya vino resuelto
        "marca": None,
        "url": url,
        "comercio": "Club de Perros y Gatos",
        "imagen": imagen,
        "precio_actual": precios[0] if precios else None,
        "precio_normal": precios[1] if precios else None,
        "descuento_pct": None,
    }


async def _fetch_pagina(sesion, pagina: int) -> tuple[list[dict], int]:
    url = f"{_TIENDA_URL}?per_page={_PRODUCTOS_POR_PAGINA}&paged={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css("div.product-grid-item"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    html_ = respuesta.body.decode("utf-8", errors="replace")
    return items, _total_paginas(html_)


def _precios_desde_jsonld(html_: str) -> tuple[int, int] | None:
    m = _JSONLD_RE.search(html_)
    if not m:
        return None
    try:
        datos = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None

    grafo = datos.get("@graph") or []
    producto = next((n for n in grafo if n.get("@type") == "Product"), None)
    if not producto:
        return None
    ofertas = producto.get("offers") or []
    if isinstance(ofertas, dict):
        ofertas = [ofertas]
    for oferta in ofertas:
        specs = oferta.get("priceSpecification") or []
        actual = normal = None
        for spec in specs:
            try:
                precio = float(spec["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if spec.get("priceType", "").endswith("ListPrice"):
                normal = precio
            else:
                actual = precio
        if actual is not None and normal is not None and normal > actual:
            return round(actual), round(normal)
    return None  # badge "Oferta" sin ListPrice real (ej. promo tipo "Pack 3x2"), falso positivo


async def _resolver_detalle(sesion, semaforo: asyncio.Semaphore, item: dict) -> dict | None:
    async with semaforo:
        try:
            respuesta = await sesion.get(item["url"])
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {item['url']}")
            html_ = respuesta.body.decode("utf-8", errors="replace")
        except Exception:
            log.warning("Falló la ficha de producto %s de Club de Perros y Gatos, se omite", item["url"], exc_info=True)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    titulo_m = re.search(r'"name":"((?:[^"\\]|\\.)*)"', html_)
    if titulo_m:
        item["titulo"] = json.loads(f'"{titulo_m.group(1)}"')

    precios = _precios_desde_jsonld(html_)
    if not precios:
        return None
    item["precio_actual"], item["precio_normal"] = precios
    return item


async def obtener_ofertas_clubdeperrosygatos(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, páginas de listado leídas sin error — 0 significa
    que no se pudo sacar nada de esta fuente). Mismo contrato que el resto de
    fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina(sesion, 1)
        except Exception:
            log.exception("Falló la página 1 del catálogo de Club de Perros y Gatos, se aborta")
            return [], 0
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    paginas_ok = 1
    todos: list[dict] = list(items)

    async def _fetch_extra(pagina: int) -> list[dict] | None:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina(sesion, pagina)
                return extra_items
            except Exception:
                log.exception("Falló la página %s del catálogo de Club de Perros y Gatos, se omite", pagina)
                return None
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if total_paginas > 1:
        resultados = await asyncio.gather(*(
            _fetch_extra(pagina) for pagina in range(2, total_paginas + 1)
        ), return_exceptions=True)
        for pagina, resultado in zip(range(2, total_paginas + 1), resultados):
            if isinstance(resultado, BaseException):
                log.error("Excepción no anticipada en la página %s de Club de Perros y Gatos, se omite: %r", pagina, resultado)
                continue
            if resultado is None:
                continue
            paginas_ok += 1
            todos.extend(resultado)

    if not todos:
        log.error("Club de Perros y Gatos: no se detectó ningún candidato en las %s páginas", total_paginas)
        return [], 0

    resueltos = [i for i in todos if i["precio_actual"] is not None]
    pendientes = [i for i in todos if i["precio_actual"] is None]

    if pendientes:
        resultados_detalle = await asyncio.gather(*(
            _resolver_detalle(sesion, semaforo, item) for item in pendientes
        ), return_exceptions=True)
        for resultado in resultados_detalle:
            if isinstance(resultado, BaseException):
                log.error("Excepción no anticipada resolviendo una ficha de Club de Perros y Gatos: %r", resultado)
                continue
            if resultado is not None:
                resueltos.append(resultado)

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in resueltos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        descuento_pct = round((1 - item["precio_actual"] / item["precio_normal"]) * 100)
        if descuento_pct <= 0:
            continue
        item["descuento_pct"] = descuento_pct
        if not item["titulo"]:
            continue  # sin título no se puede armar el mensaje de Telegram
        items_finales.append(item)

    log.info(
        "Club de Perros y Gatos: %s ofertas crudas (sin duplicados) de %s candidatos, %s/%s páginas leídas",
        len(items_finales), len(todos), paginas_ok, total_paginas,
    )
    return items_finales, paginas_ok
