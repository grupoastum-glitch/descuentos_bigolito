"""Scrapea la fuente de ofertas de Falabella Chile.

Dos modos, detectados automáticamente sin requests extra:

- Modo directo: `url_base` es un listado paginable (?page=N) con `pageProps.results` plano —
  el caso histórico de /collection/ofertas.
- Modo hub: `url_base` es una página curada (CMS) sin `results` propio — el caso actual de
  /page/descuentos, que en cambio linkea a ~decenas de páginas de categoría con un filtro de
  descuento aplicado en la query, cada una con el mismo `pageProps.results` de siempre. Esos
  links se cosechan del HTML de la página (sin LLM, determinístico) y se scrapean con el mismo
  parser que el modo directo.

Las páginas/categorías se piden con concurrencia acotada (config.CONCURRENCIA_LISTADO) — cada
"carril" respeta igual la pausa aleatoria DELAY_MIN/MAX_SEGUNDOS entre sus propias requests, solo
que ahora corren varios carriles en paralelo en vez de uno solo (ver spike de verificación en la
sesión que introdujo esto: FetcherSession async soporta bien requests concurrentes sobre una
misma sesión).
"""
from __future__ import annotations

import asyncio
import html as html_lib
import logging
import random
import re
from urllib.parse import parse_qs, urljoin, urlparse

import config
from fuentes.falabella.parsing import calcular_descuento_pct, extraer_next_data, precios_desde_lista

log = logging.getLogger("scraper.fuentes.listado")

_HREF_RE = re.compile(r'href="([^"]+)"')
_PARAM_DESCUENTO_FACET = "f.range.derived.variant.discount"


async def _fetch_pagina_html(sesion, url: str, path_esperado: str) -> str:
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    if not urlparse(respuesta.url).path.startswith(path_esperado):
        raise RuntimeError(f"{url} redirigió a {respuesta.url}, se omite")
    return respuesta.body.decode("utf-8", errors="replace")


async def _obtener_pagina(sesion, url_base: str, pagina: int) -> dict:
    url = f"{url_base}?page={pagina}"
    html = await _fetch_pagina_html(sesion, url, urlparse(url_base).path)
    return extraer_next_data(html)


def _items_desde_productos(productos: list[dict]) -> list[dict]:
    items = []
    for producto in productos:
        precio_actual, precio_normal = precios_desde_lista(producto.get("prices", []))
        if precio_actual is None:
            continue
        badge = (producto.get("discountBadge") or {}).get("label")
        descuento_pct = calcular_descuento_pct(precio_actual, precio_normal, badge)
        if descuento_pct <= 0:
            continue

        items.append({
            "producto_id": str(producto["productId"]),
            "titulo": producto.get("displayName") or "",
            "marca": producto.get("brand"),
            "url": producto["url"],
            "comercio": "Falabella",
            "imagen": (producto.get("mediaUrls") or [None])[0],
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _fetch_una_pagina_directo(sesion, semaforo, url_base: str, pagina: int, datos_pagina1: dict) -> list[dict] | None:
    async with semaforo:
        try:
            datos = datos_pagina1 if pagina == 1 else await _obtener_pagina(sesion, url_base, pagina)
            productos = datos["props"]["pageProps"]["results"]
            resultado = _items_desde_productos(productos)
        except Exception:
            log.exception("Falló la página %s del listado de ofertas, se omite", pagina)
            return None
        finally:
            if pagina != 1:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
    return resultado


async def _listado_modo_directo(sesion, url_base: str, datos_pagina1: dict) -> tuple[list[dict], int]:
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: una excepción no prevista en una sola página no debe tumbar la
    # corrida completa (ver incidencia 2026-08-11 en el módulo de Xiaomi, mismo patrón acá).
    resultados = await asyncio.gather(*(
        _fetch_una_pagina_directo(sesion, semaforo, url_base, pagina, datos_pagina1)
        for pagina in range(1, config.PAGINAS_LISTADO + 1)
    ), return_exceptions=True)

    items: list[dict] = []
    paginas_ok = 0
    for resultado in resultados:
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en una página del listado, se omite: %r", resultado)
            continue
        if resultado is None:
            continue
        paginas_ok += 1
        items.extend(resultado)

    log.info(
        "Listado directo: %s ofertas crudas en %s/%s páginas revisadas con éxito",
        len(items), paginas_ok, config.PAGINAS_LISTADO,
    )
    return items, paginas_ok


def _descuento_exigido(url: str) -> int | None:
    """% mínimo exigido por el facet de descuento en la query, o None si el link no lo trae."""
    for valor in parse_qs(urlparse(url).query).get(_PARAM_DESCUENTO_FACET, []):
        numeros = re.findall(r"\d+", valor)
        if numeros:
            return int(numeros[0])
    return None


def _prioridad(url: str) -> tuple[int, int]:
    """Menor = mejor. Un link CON facet de descuento siempre le gana a uno sin facet (evita
    terminar levantando la categoría entera sin filtrar); entre links con facet, gana el de
    MENOR % exigido (cubre más productos de esa categoría)."""
    descuento = _descuento_exigido(url)
    return (0, descuento) if descuento is not None else (1, 0)


def _dedupe_por_categoria(urls: list[str]) -> list[str]:
    mejores: dict[str, str] = {}
    for url in urls:
        clave = urlparse(url).path  # ignora query (tamaño/mkid/género/facet) a propósito
        if clave not in mejores or _prioridad(url) < _prioridad(mejores[clave]):
            mejores[clave] = url
    categorias = [mejores[clave] for clave in sorted(mejores)]
    if len(categorias) > config.MAX_CATEGORIAS_DESCUENTOS:
        log.warning(
            "Hub de descuentos: %s categorías encontradas, se truncan a %s (MAX_CATEGORIAS_DESCUENTOS)",
            len(categorias), config.MAX_CATEGORIAS_DESCUENTOS,
        )
        categorias = categorias[: config.MAX_CATEGORIAS_DESCUENTOS]
    return categorias


def _extraer_urls_categoria(html: str, url_base: str) -> list[str]:
    hrefs = _HREF_RE.findall(html)
    absolutas = {urljoin(url_base, html_lib.unescape(href)) for href in hrefs}
    candidatas = [url for url in absolutas if "/category/" in urlparse(url).path]
    return _dedupe_por_categoria(candidatas)


async def _obtener_categoria_pagina(sesion, url_categoria: str, pagina: int) -> dict:
    separador = "&" if "?" in url_categoria else "?"
    url = f"{url_categoria}{separador}page={pagina}"
    html = await _fetch_pagina_html(sesion, url, urlparse(url_categoria).path)
    return extraer_next_data(html)


async def _fetch_una_categoria(sesion, semaforo, categoria_url: str, pagina: int) -> list[dict] | None:
    async with semaforo:
        try:
            datos = await _obtener_categoria_pagina(sesion, categoria_url, pagina)
            productos = datos["props"]["pageProps"]["results"]
            resultado = _items_desde_productos(productos)
        except Exception:
            log.exception("Falló %s (página %s) del hub de descuentos, se omite", categoria_url, pagina)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
    return resultado


async def _listado_modo_hub(sesion, url_base: str, html_pagina1: str) -> tuple[list[dict], int]:
    categorias = _extraer_urls_categoria(html_pagina1, url_base)
    log.info("Hub de descuentos (%s): %s categorías únicas encontradas", url_base, len(categorias))
    if not categorias:
        log.error("Hub de descuentos (%s) no tiene ningún link /category/ utilizable", url_base)
        return [], 0

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    tareas = [
        _fetch_una_categoria(sesion, semaforo, categoria_url, pagina)
        for categoria_url in categorias
        for pagina in range(1, config.PAGINAS_POR_CATEGORIA_DESCUENTOS + 1)
    ]
    # return_exceptions=True: una excepción no prevista en una sola categoría no debe tumbar la
    # corrida completa (ver incidencia 2026-08-11 en el módulo de Xiaomi, mismo patrón acá).
    resultados = await asyncio.gather(*tareas, return_exceptions=True)

    items: list[dict] = []
    categorias_ok = 0
    for resultado in resultados:
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en una categoría del hub, se omite: %r", resultado)
            continue
        if resultado is None:
            continue
        categorias_ok += 1
        items.extend(resultado)
    # categorias_ok cuenta "categoría×página" ok, no categorías únicas — con
    # PAGINAS_POR_CATEGORIA_DESCUENTOS == 1 (el único valor usado hoy) ambos coinciden.

    if categorias and categorias_ok / len(categorias) < 0.5:
        log.warning(
            "Hub de descuentos: solo %s/%s categorías se leyeron con éxito (<50%%)",
            categorias_ok, len(categorias),
        )

    log.info(
        "Hub de descuentos: %s ofertas crudas en %s/%s categorías leídas con éxito",
        len(items), categorias_ok, len(categorias),
    )
    return items, categorias_ok


async def obtener_ofertas_listado(sesion, url_base: str = config.FALABELLA_OFERTAS_URL) -> tuple[list[dict], int]:
    """Devuelve (items crudos, "unidades" leídas sin error — páginas en modo directo,
    categorías en modo hub; 0 significa que no se pudo sacar nada de esta fuente).

    items crudos: producto_id, titulo, marca, url, comercio, imagen,
    precio_actual, precio_normal, descuento_pct."""
    url_pagina1 = f"{url_base}?page=1"
    try:
        html_pagina1 = await _fetch_pagina_html(sesion, url_pagina1, urlparse(url_base).path)
        datos_pagina1 = extraer_next_data(html_pagina1)
    except Exception:
        log.exception("Falló la página 1 del listado de ofertas (%s), se aborta", url_base)
        return [], 0

    if "results" in datos_pagina1.get("props", {}).get("pageProps", {}):
        return await _listado_modo_directo(sesion, url_base, datos_pagina1)

    log.info("%s no tiene 'results' en pageProps — se interpreta como hub de categorías", url_base)
    return await _listado_modo_hub(sesion, url_base, html_pagina1)
