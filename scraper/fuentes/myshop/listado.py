"""Scrapea las ofertas de MyShop (myshop.cl) — quinta tienda del rubro Tech.

Backend PHP a medida (cookies `PaginaWEB`/`PHPSESSID`, sin match de WooCommerce/Shopify/Magento/
etc.), detrás de Cloudflare. Ninguna página de categoría trae productos embebidos en su HTML: todo
se pobla client-side vía la API JSON interna que el propio sitio usa para su filtro de catálogo
(`#filtroShop`, JS "sphinxProductos.js" — confirmado en vivo): `POST /servicio/producto` con
`{"tipo": "3", "idFamilia": <id>, "page": N}` para una categoría normal, o `{"tipo": "6", "page": N}`
para la sección de Liquidación/Outlet (`/liquidacion`, sin `idFamilia`).

El sitio no expone un árbol de categorías en JSON ni un filtro "solo ofertas" para el catálogo
general (confirmado: probando una categoría cualquiera aparecen ofertas reales dispersas fuera de
Liquidación, así que limitarse a esa sección se queda corto) — hay que cosechar los slugs de
categoría desde los links de la home (`href="/slug"`) y quedarse con las hojas reales (un slug cuyo
nombre no es prefijo de ningún otro — evita repetir el mismo catálogo bajo su categoría padre, ej.
"gamer" vs "gamer-monitores-gamer"), descartando además un puñado de páginas no-catálogo conocidas
(carro, boleta, contacto, etc.). A diferencia de Sodimac/SPDigital (donde la URL alcanza para
paginar), acá la API exige el `idFamilia` numérico de cada categoría, que no viene en la home — hay
que visitar la página de cada categoría hoja una vez para sacarlo del `<input name="idFamilia">` de
su propio `#filtroShop`.

Con ~100-110 categorías hoja reales, se muestrean `_MAX_CATEGORIAS` al azar (mismo criterio que
Sodimac/PCFactory: recorrerlas todas sería excesivo) y, de cada una, página 1 (ancla, da
`resultado.productos.count` — el total real de la categoría) + `_PAGINAS_ALEATORIAS_POR_CATEGORIA`
al azar hasta un techo. Liquidación (`tipo=6`) se suma siempre aparte, completa (catálogo chico,
~18 productos en la prueba en vivo).

Cada producto trae 3 precios: `precio` (Transferencia o Efectivo, el más bajo), `precio_tarjeta`
(pagando con tarjeta — siempre `precio * 1.055` exacto, un recargo fijo del 5.5%, confirmado en
12/12 productos de muestra) y `precio_normal` (precio de lista antes del descuento). Se usa el
precio más bajo, `precio_actual = precio` (Transferencia/Efectivo) — no es una restricción tipo
CMR de Falabella/Xiaomi, pagar así está al alcance de cualquier comprador; decisión 2026-08-22,
mismo criterio ya aplicado en PCFactory/PC Express/SPDigital. Se recalcula el % siempre desde
`precio`/`precio_normal`, nunca desde el `descuento`/`oferta` que ya trae el JSON.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re

import config

log = logging.getLogger("scraper.fuentes.myshop")

_BASE_URL = "https://www.myshop.cl"
_API_URL = f"{_BASE_URL}/servicio/producto"

_PRODUCTOS_POR_PAGINA = 12  # observado, confirmado contra el sitio real
_MAX_CATEGORIAS = 53  # ~50% de las ~100-110 categorías hoja — subido de 45 el 2026-08-22 para
# reducir la latencia de detección en categorías poco visitadas (antes ~43% de cobertura por
# corrida); acá cada categoría ya cuesta un request extra (harvest del idFamilia) antes de poder
# pedir productos, así que el aumento de tráfico real es proporcionalmente mayor que en PC
# Express/PCFactory — sin Cloudflare de por medio, pero nunca probado a este volumen, revisar
# fallos_tiendas.json los primeros días por si el sitio empieza a devolver errores.
_MAX_PAGINAS_POR_CATEGORIA = 10
_PAGINAS_ALEATORIAS_POR_CATEGORIA = 2  # además de la página 1 (siempre se pide)

# páginas del sitio que no son categorías de catálogo (nav/cuenta/legal) — quedarían como "hoja"
# igual que una categoría real porque ningún otro slug las tiene como prefijo.
_NO_CATALOGO = {
    "carro", "boleta", "contacto", "despachogratis", "login", "nosotros",
    "politica-privacidad", "preguntas-frecuentes", "registro", "seguimiento",
    "seguimiento-contacto", "terminos-y-condiciones", "liquidacion", "servicios",
}

_SLUG_RE = re.compile(r'href="(/[a-z0-9-]+)"')
# el orden de atributos del input varía entre páginas (`value="139" name="idFamilia"` en algunas,
# `name="idFamilia" value="139"` en otras) — se matchea el tag completo sin asumir un orden.
_ID_FAMILIA_RE = re.compile(r'<input[^>]*name="idFamilia"[^>]*value="(\d+)"|<input[^>]*value="(\d+)"[^>]*name="idFamilia"')


def _slugs_hoja(html_: str) -> list[str]:
    slugs = {m.group(1).strip("/") for m in _SLUG_RE.finditer(html_)}
    slugs -= _NO_CATALOGO
    slugs = {s for s in slugs if not s.startswith("centro-de-ayuda")}
    return [s for s in slugs if not any(otro != s and otro.startswith(s + "-") for otro in slugs)]


def _item_desde_producto(producto: dict) -> dict | None:
    try:
        precio_actual = float(producto["precio"])
        precio_normal = float(producto["precio_normal"])
    except (KeyError, TypeError, ValueError):
        return None
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None
    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    producto_id = producto.get("id_producto")
    url = producto.get("url")
    if producto_id is None or not url:
        return None

    return {
        "producto_id": str(producto_id),
        "titulo": producto.get("nombre"),
        "marca": producto.get("marca"),
        "url": f"{_BASE_URL}{url}",
        "comercio": "MyShop",
        "imagen": producto.get("foto"),
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


async def _categorias_disponibles(sesion) -> list[str]:
    respuesta = await sesion.get(_BASE_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_BASE_URL}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    return _slugs_hoja(html_)


async def _fetch_idfamilia(sesion, slug: str) -> int | None:
    url = f"{_BASE_URL}/{slug}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    html_ = respuesta.body.decode("utf-8", errors="replace")
    m = _ID_FAMILIA_RE.search(html_)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


async def _fetch_pagina(sesion, body: dict) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, resultado.productos.count — total real de la fuente,
    usado para saber cuántas páginas hay)."""
    respuesta = await sesion.post(_API_URL, json=body)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_API_URL} ({body})")
    resultado = (respuesta.json() or {}).get("resultado") or {}
    total = (resultado.get("productos") or {}).get("count") or 0
    return _items_desde_lista(resultado.get("items") or []), total


async def _fetch_fuente_paginada(sesion, semaforo: asyncio.Semaphore, body_base: dict, etiqueta: str) -> list[dict] | None:
    """Pide página 1 (ancla) + páginas al azar hasta un techo, para un body de request ya armado
    (categoría con idFamilia, o liquidación con tipo=6)."""
    async with semaforo:
        try:
            items, total = await _fetch_pagina(sesion, {**body_base, "page": 1})
        except Exception:
            log.exception("Falló la página 1 de %s en MyShop, se omite", etiqueta)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    total_paginas = math.ceil(total / _PRODUCTOS_POR_PAGINA) if total else 1
    techo = min(total_paginas, _MAX_PAGINAS_POR_CATEGORIA)
    candidatas = list(range(2, techo + 1))
    extra_paginas = random.sample(candidatas, min(_PAGINAS_ALEATORIAS_POR_CATEGORIA, len(candidatas)))

    for pagina in extra_paginas:
        async with semaforo:
            try:
                extra_items, _ = await _fetch_pagina(sesion, {**body_base, "page": pagina})
                items.extend(extra_items)
            except Exception:
                log.exception("Falló la página %s de %s en MyShop, se omite esa página", pagina, etiqueta)
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    return items


async def _fetch_categoria(sesion, semaforo: asyncio.Semaphore, slug: str) -> list[dict] | None:
    async with semaforo:
        try:
            id_familia = await _fetch_idfamilia(sesion, slug)
        except Exception:
            log.exception("Falló la página de categoría %s de MyShop, se omite", slug)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if id_familia is None:
        log.warning("MyShop: la categoría %s no tiene idFamilia (página no-catálogo), se omite", slug)
        return None

    return await _fetch_fuente_paginada(sesion, semaforo, {"tipo": "3", "idFamilia": id_familia}, slug)


async def obtener_ofertas_myshop(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, fuentes leídas sin error — categorías + liquidación,
    0 significa que no se pudo sacar nada de esta fuente). Mismo contrato que el resto de
    fuentes.<tienda>.listado."""
    try:
        slugs_disponibles = await _categorias_disponibles(sesion)
    except Exception:
        log.exception("Falló la carga de la home de MyShop para cosechar categorías, se aborta")
        return [], 0

    if not slugs_disponibles:
        log.error("MyShop: no se encontró ninguna categoría hoja en la home")
        return [], 0

    slugs = random.sample(slugs_disponibles, min(_MAX_CATEGORIAS, len(slugs_disponibles)))
    log.info("MyShop: %s categorías hoja disponibles, muestreando %s", len(slugs_disponibles), len(slugs))

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    etiquetas = [*slugs, "liquidacion"]
    tareas = [_fetch_categoria(sesion, semaforo, slug) for slug in slugs]
    tareas.append(_fetch_fuente_paginada(sesion, semaforo, {"tipo": "6"}, "liquidacion"))
    resultados = await asyncio.gather(*tareas, return_exceptions=True)

    fuentes_ok = 0
    todos: list[dict] = []
    for etiqueta, resultado in zip(etiquetas, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en %s de MyShop, se omite: %r", etiqueta, resultado)
            continue
        if resultado is None:
            continue
        fuentes_ok += 1
        todos.extend(resultado)

    if not todos:
        log.error("MyShop: no se detectó ninguna oferta en las %s categorías + liquidación", len(slugs))
        return [], 0

    vistos: set[str] = set()
    items: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items.append(item)

    log.info(
        "MyShop: %s ofertas crudas (sin duplicados) en %s/%s fuentes leídas con éxito",
        len(items), fuentes_ok, len(etiquetas),
    )
    return items, fuentes_ok
