"""Scrapea las ofertas de PC Express (tienda.pc-express.cl) — séptima tienda del rubro Tech.

OpenCart (self-hosted, server LiteSpeed) — confirmado por la estructura de paths
`catalog/view/javascript/...`/`catalog/view/theme/...` y las rutas clásicas
`index.php?route=product/category&path=...`. URLs de producto son amigables
(`/21657-mouse-gamer-asus-...`), pero categorías y la página de ofertas usan `index.php?route=`.
Sin Cloudflare ni challenge (a diferencia de Ripley/WePlay): responde igual con un fetch plano.

Existe una página de ofertas curada (`route=product/ofertas`), pero trae solo 55 productos en
todo el sitio — insuficiente por sí sola: probando una sola categoría normal ("Tarjetas de
Video") aparecieron 36 productos con descuento que no están en esa página. Mismo caso que
Easy/MyShop ("hay descuentos reales dispersos en categorías normales, limitarse a la sección
curada se queda corto") → se cosecha además el árbol de categorías. No hay API de categorías (a
diferencia de PCFactory): se cosechan del menú mega de la home (mismo criterio que Sodimac),
`route=product/category&path=<id>[_<id>...]` anidados por `_` (~296 paths, ~251 hojas — ningún
otro path las tiene como prefijo, mismo filtro que `fuentes.myshop.listado`). Se muestrean
`_MAX_CATEGORIAS` al azar (recorrerlas todas sería excesivo) + siempre la página de ofertas
completa aparte (cabe entera en 1 request).

Cada categoría/ofertas se pide con `limit=100` (tope real confirmado: es el máximo que ofrece el
selector "Mostrar" del propio sitio) y expone el total de páginas directo en el texto "Mostrando
X al Y de Z (N páginas)" — a diferencia de SPDigital/PCFactory/Sodimac no hace falta derivarlo de
un conteo de productos / tamaño de página. Mismo patrón ancla+aleatorio de siempre: página 1
(ancla) + `_PAGINAS_ALEATORIAS_POR_CATEGORIA` al azar hasta un techo. `robots.txt` bloquea (para
buscadores) los query params `?page=`/`&limit=`/`&sort=` — es el robots.txt genérico de plantilla
OpenCart, no una medida anti-bot dirigida (sin Cloudflare de por medio); mismo criterio ya
aplicado en MyShop (bloquea puntual a `ClaudeBot`, se sigue igual porque el scraper no se
identifica con ese user-agent).

Cada tarjeta de listado trae 2 precios propios (no hace falta visitar la ficha de producto, a
diferencia de por qué se dejó pendiente Winpy): `Normal` (precio de lista, solo presente si el
producto está en oferta — mismo criterio que el tachado de SPDigital) y `Transferencia` (el
precio vigente que muestra la tarjeta). Se usa `precio_actual = Transferencia` contra
`precio_normal = Normal` — mismo criterio que PCFactory ("efectivo es débito/transferencia/
efectivo real, al alcance de cualquier comprador", no una restricción tipo CMR) y confirmado
explícitamente con el usuario (da el % más alto/real; la alternativa más conservadora de
SPDigital/MyShop, contra el precio "Otros medios" que solo aparece en la ficha de producto,
hubiera dejado muy pocos productos sobre el piso de 25% del canal). Se recalcula el % siempre
desde esos 2 precios crudos.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.pcexpress")

_BASE_URL = "https://tienda.pc-express.cl"
_HOME_URL = f"{_BASE_URL}/"

_PRODUCTOS_POR_PAGINA = 100  # tope real del selector "Mostrar" del sitio, confirmado en vivo
_MAX_CATEGORIAS = 50  # techo de categorías hoja muestreadas por corrida — mismo criterio que
# fuentes.myshop._MAX_CATEGORIAS/fuentes.pcfactory._MAX_CATEGORIAS: el árbol completo
# (~251 categorías hoja) es demasiado para recorrer entero cada corrida.
_MAX_PAGINAS_POR_CATEGORIA = 10
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)

_CATEGORIA_RE = re.compile(r"route=product/category(?:&amp;|&)path=([0-9_]+)")
_TOTAL_PAGINAS_RE = re.compile(r"\((\d+)\s*p[aá]ginas?\)", re.I)
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _categorias_hoja(paths: set[str]) -> list[str]:
    return [p for p in paths if not any(o != p and o.startswith(p + "_") for o in paths)]


def _item_desde_card(card) -> dict | None:
    producto_id = card.attrib.get("data-product-id")
    if not producto_id:
        return None

    viejo = card.css(".product-list__price-old")
    if not viejo:
        return None  # sin precio "Normal" tachado, este producto no está en oferta
    precio_normal = _parse_precio(viejo[0].get_all_text(strip=True))

    actual_nodo = card.css(".product-list__price")
    precio_actual = _parse_precio(actual_nodo[0].get_all_text(strip=True)) if actual_nodo else None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    link_nodo = card.css(".product-list__name a")
    if not link_nodo:
        return None
    titulo = link_nodo[0].get_all_text(strip=True)
    url = link_nodo[0].attrib.get("href")
    if not url:
        return None
    url = url.split("?")[0]

    marca_nodo = card.css(".product-list__manufacturer span")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css(".product-list__image img")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": marca,
        "url": url,
        "comercio": "PC Express",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


def _total_paginas(html_: str) -> int:
    m = _TOTAL_PAGINAS_RE.search(html_)
    return int(m.group(1)) if m else 1


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_HOME_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_HOME_URL}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    paths = {m.group(1) for m in _CATEGORIA_RE.finditer(html_)}
    return _categorias_hoja(paths)


async def _fetch_pagina(sesion, ruta: str, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de páginas real de la fuente — sale directo del
    texto "Mostrando ... (N páginas)" del propio sitio)."""
    url = f"{_BASE_URL}/index.php?route={ruta}&limit={_PRODUCTOS_POR_PAGINA}"
    if pagina > 1:
        url = f"{url}&page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css(".product-list__item"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    html_ = respuesta.body.decode("utf-8", errors="replace")
    return items, _total_paginas(html_)


async def _fetch_fuente_paginada(sesion, semaforo: asyncio.Semaphore, ruta: str, etiqueta: str) -> list[dict] | None:
    """Pide página 1 (ancla) + páginas al azar hasta un techo, para una ruta ya armada (categoría
    con su path, o la página de ofertas)."""
    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina(sesion, ruta, 1)
        except Exception:
            log.exception("Falló la página 1 de %s en PC Express, se omite", etiqueta)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    techo = min(total_paginas or 1, _MAX_PAGINAS_POR_CATEGORIA)
    candidatas = list(range(2, techo + 1))
    extra_paginas = random.sample(candidatas, min(_PAGINAS_ALEATORIAS_POR_CATEGORIA, len(candidatas)))

    for pagina in extra_paginas:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina(sesion, ruta, pagina)
                items.extend(extra_items)
            except Exception:
                log.exception("Falló la página %s de %s en PC Express, se omite esa página", pagina, etiqueta)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def obtener_ofertas_pcexpress(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, fuentes leídas sin error — categorías + ofertas,
    0 significa que no se pudo sacar nada de esta fuente). Mismo contrato que el resto de
    fuentes.<tienda>.listado."""
    try:
        categorias_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga de la home de PC Express para cosechar categorías, se aborta")
        return [], 0

    if not categorias_disponibles:
        log.error("PC Express: no se encontró ninguna categoría hoja en la home")
        return [], 0

    categorias = random.sample(categorias_disponibles, min(_MAX_CATEGORIAS, len(categorias_disponibles)))
    log.info("PC Express: %s categorías hoja disponibles, muestreando %s", len(categorias_disponibles), len(categorias))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    etiquetas = ["ofertas", *categorias]
    tareas = [_fetch_fuente_paginada(sesion, semaforo, "product/ofertas", "ofertas")]
    tareas += [
        _fetch_fuente_paginada(sesion, semaforo, f"product/category&path={path}", path)
        for path in categorias
    ]
    resultados = await asyncio.gather(*tareas, return_exceptions=True)

    fuentes_ok = 0
    todos: list[dict] = []
    for etiqueta, resultado in zip(etiquetas, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en %s de PC Express, se omite: %r", etiqueta, resultado)
            continue
        if resultado is None:
            continue
        fuentes_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("PC Express: no se detectó ninguna oferta en ofertas + %s categorías muestreadas", len(categorias))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "PC Express: %s ofertas crudas (sin duplicados) en %s/%s fuentes leídas con éxito",
        len(items), fuentes_ok, len(etiquetas),
    )
    return items, fuentes_ok
