"""Scrapea las ofertas de Paris Chile (paris.cl).

A diferencia de Falabella y Ripley (Next.js Pages Router, un único bloque
`<script id="__NEXT_DATA__">`, ver `fuentes.falabella.parsing`), Paris es Next.js **App Router**:
los datos viajan repartidos en varias llamadas `self.__next_f.push([id, "..."])` (protocolo RSC),
como fragmentos de texto con comillas escapadas (`\"`) — no hay un único JSON para parsear con
`json.loads`. Reensamblar el protocolo Flight completo sería mucho trabajo para lo que hace
falta acá: en vez de eso, se ancla por texto. Cada producto arranca con el patrón literal
`"id":"<id>","key":"<id>","name":"...","slug":"..."` (a nivel de producto, `id` y `key` son el
mismo valor — puede ser numérico o alfanumérico tipo "MK..." en productos marketplace/
reacondicionados) — se usa esa posición como frontera para recortar "el segmento de texto de ese
producto" (hasta el próximo match), y adentro se buscan con regex simples marca, sku, precios e
imagen. Esto funciona porque el orden de campos observado es estable: `masterVariant` (con sus
propios `prices`/`images`) aparece siempre antes que `variants`, así que tomar la *primera*
coincidencia de cada campo dentro del segmento cae naturalmente en `masterVariant`, no en una
variante de otro color/talla.

El backend es tipo commercetools: cada producto trae `prices.regular` (precio de lista) y, solo
si está rebajado, `prices.offer` (precio con descuento) — ambos como `centAmount` en pesos
enteros (CLP no tiene decimales). Paris también manda `discountOnRegular` ya calculado, pero acá
el % se recalcula desde los dos precios en vez de confiar en ese campo, mismo criterio que
`fuentes.ripley.listado` (ahí se detectaron casos donde el % publicado no correspondía a los
precios reales).

La URL del producto no hace falta scrapearla de ningún `href`: el campo `slug` ya incluye el
código final del producto, y la URL real es simplemente `https://www.paris.cl/<slug>.html`
(confirmado contra el sitio real).

`/outlet/` (la raíz) siempre devuelve el mismo primer lote y no pagina server-side (`?page=N`
no cambia nada, confirmado con requests frescas que bypasean cualquier caché) — la paginación
real es client-side. En vez de pelear con eso, igual que `fuentes.xiaomi.listado` y
`fuentes.ripley.listado`, se recorre el árbol de categorías del outlet: el menú de navegación
del home linkea ~100 hojas de subcategoría bajo `/outlet/<departamento>/<subcategoria>/` (ej.
`/outlet/outlet-tecno/outlet-celulares/`), cada una con su propio listado (también limitado a su
primera página, pero eso ya multiplica la cobertura por ~100 en vez de 1).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.paris")

_HOME_URL = "https://www.paris.cl/"

# los .{1,N}? acotados (en vez de .*? sin límite) son a propósito: en algunos productos
# marketplace se vio el escapado de comillas romperse a mitad de bloque (probablemente un
# corte de chunk del stream RSC) y un .*? sin límite se comía de largo el resto del documento,
# corrompiendo el título y perdiendo los productos siguientes. Con el límite, esos casos raros
# simplemente no matchean (se pierde ese producto puntual) en vez de corromper datos.
_PRODUCTO_INICIO_RE = re.compile(
    r'\\"id\\":\\"([A-Za-z0-9]+)\\",\\"key\\":\\"\1\\",\\"name\\":\\"(.{1,200}?)\\",\\"slug\\":\\"([a-z0-9-]+)\\"'
)
_BRAND_RE = re.compile(r'\\"brand\\":\\"(.{1,100}?)\\"')
_REGULAR_RE = re.compile(r'\\"regular\\":\{.{1,300}?\\"centAmount\\":(\d+)', re.S)
_OFFER_RE = re.compile(r'\\"offer\\":\{.{1,300}?\\"centAmount\\":(\d+)', re.S)
_IMAGEN_RE = re.compile(r'\\"images\\":\[\{\\"url\\":\\"(.{1,300}?)\\"')

# hojas de subcategoría del outlet, ej. https://www.paris.cl/outlet/outlet-tecno/outlet-celulares
# — exactamente dos segmentos después de /outlet/, para no quedarse con la raíz ni con las 14
# páginas de departamento (que son superset de sus propias hojas, sumarlas duplicaría productos).
_HOJA_HREF_RE = re.compile(
    r'\\"href\\":\\"(https://www\.paris\.cl/outlet/[a-z0-9-]+/[a-z0-9-]+)/?\\"'
)


def _desescapar(fragmento: str) -> str:
    """El fragmento viene tal cual aparece en el stream RSC (escapado como si fuera el cuerpo
    de un string JSON: \\" para comillas, \\uXXXX para unicode, etc.) — json.loads con comillas
    reales alrededor lo decodifica limpio."""
    try:
        return json.loads(f'"{fragmento}"')
    except json.JSONDecodeError:
        return fragmento


def _items_desde_html(html: str) -> list[dict]:
    inicios = list(_PRODUCTO_INICIO_RE.finditer(html))
    items = []
    for i, inicio in enumerate(inicios):
        fin = inicios[i + 1].start() if i + 1 < len(inicios) else len(html)
        segmento = html[inicio.start():fin]

        oferta_match = _OFFER_RE.search(segmento)
        regular_match = _REGULAR_RE.search(segmento)
        if not oferta_match or not regular_match:
            continue  # sin "offer" no es una oferta real, solo precio de lista

        precio_actual = int(oferta_match.group(1))
        precio_normal = int(regular_match.group(1))
        if precio_normal <= 0:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        brand_match = _BRAND_RE.search(segmento)
        imagen_match = _IMAGEN_RE.search(segmento)

        items.append({
            "producto_id": inicio.group(1),
            "titulo": _desescapar(inicio.group(2)),
            "marca": _desescapar(brand_match.group(1)) if brand_match else None,
            "url": f"https://www.paris.cl/{inicio.group(3)}.html",
            "comercio": "Paris",
            "imagen": _desescapar(imagen_match.group(1)) if imagen_match else None,
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _hojas_outlet(sesion) -> list[str]:
    respuesta = await sesion.get(_HOME_URL)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {_HOME_URL}")
    html = respuesta.body.decode("utf-8", errors="replace")
    return sorted({f"{m.group(1)}/" for m in _HOJA_HREF_RE.finditer(html)})


async def _fetch_hoja(sesion, semaforo: asyncio.Semaphore, url: str) -> list[dict] | None:
    async with semaforo:
        try:
            respuesta = await sesion.get(url)
            if respuesta.status != 200:
                raise RuntimeError(f"HTTP {respuesta.status} en {url}")
            html = respuesta.body.decode("utf-8", errors="replace")
            resultado = _items_desde_html(html)
        except Exception:
            log.exception("Falló %s del outlet de Paris, se omite", url)
            return None
        finally:
            await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))
    return resultado


async def obtener_ofertas_paris(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, hojas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que fuentes.xiaomi.listado.obtener_ofertas_xiaomi y
    fuentes.ripley.listado.obtener_ofertas_ripley."""
    try:
        hojas = await _hojas_outlet(sesion)
    except Exception:
        log.exception("Falló la carga del menú de outlet de Paris (%s), se aborta", _HOME_URL)
        return [], 0

    log.info("Paris: %s hojas de outlet encontradas", len(hojas))
    if not hojas:
        log.error("Paris: el menú de navegación no tiene ninguna hoja de outlet utilizable")
        return [], 0

    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)
    # return_exceptions=True: mismo motivo que en Falabella/Xiaomi/Ripley — una excepción no
    # anticipada en una sola hoja no debe tumbar la corrida completa (incidencia 2026-08-11).
    resultados = await asyncio.gather(*(
        _fetch_hoja(sesion, semaforo, url) for url in hojas
    ), return_exceptions=True)

    vistos: set[str] = set()
    items: list[dict] = []
    hojas_ok = 0
    for url, resultado in zip(hojas, resultados):
        if isinstance(resultado, BaseException):
            log.error("Excepción no anticipada en %s del outlet de Paris, se omite: %r", url, resultado)
            continue
        if resultado is None:
            continue
        hojas_ok += 1
        for item in resultado:
            if item["producto_id"] in vistos:
                continue  # un mismo producto puede aparecer en más de una hoja
            vistos.add(item["producto_id"])
            items.append(item)

    if not items:
        log.error("Paris: no se detectó ninguna oferta en las %s hojas leídas", hojas_ok)
        return [], 0

    log.info(
        "Paris: %s ofertas crudas (sin duplicados) en %s/%s hojas leídas con éxito",
        len(items), hojas_ok, len(hojas),
    )
    return items, hojas_ok
