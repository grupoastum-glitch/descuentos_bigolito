"""Scrapea las ofertas de Geekz (geekz.cl).

Es una tienda Odoo (eCommerce/website_sale) — cookies `session_id`, rutas `/web/content/...` y
`/web/image/...`, clases HTML `oe_product`/`o_wsale_*`/`oe_currency_value` son firmas de esa
plataforma. A diferencia de LuffyToys, no hay endpoint JSON público (`/products.json` da 404) —
hay que parsear el HTML server-renderizado de `/shop?page=N` (paginación real confirmada: 0
productos repetidos entre página 1 y 2), 15 productos por página, cantidad total declarada en
`data-total-page` del propio HTML.

No hay ninguna colección "ofertas" ni campo de "% off" propio del sitio — se calcula el
descuento de cada producto comparando `price` (itemprop="price") contra `list_price`
(`<strike data-oe-expression="combination_info['list_price']">`), mismo criterio que el resto de
las fuentes: no confiar en ningún texto de descuento que muestre el sitio ("(20% DCTO)" en este
caso), recalcular siempre desde los precios crudos.

Pedir las páginas muy rápido seguidas (sin pausa) dispara un 503 Service Temporarily Unavailable
a mitad de camino (confirmado) — se espera random.uniform(DELAY_MIN_SEGUNDOS, DELAY_MAX_SEGUNDOS)
entre cada página, igual que el resto de las fuentes que recorren varias URLs.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import re

import config

log = logging.getLogger("scraper.fuentes.geekz")

_BASE_URL = "https://www.geekz.cl"

_TOTAL_PAGINAS_RE = re.compile(r'data-total-page="(\d+)"')

# el label de marca (ej. "/POKEMON COMPANY") es opcional a propósito: no todos los productos
# traen ese atributo — cuando falta, el grupo "marca" simplemente no matchea y queda None.
_PRODUCTO_RE = re.compile(
    r'(?:href="/shop\?attrib=[^"]*">\s*<small class="dr_category_lable[^"]*">/(?P<marca>[^<]+)</small>\s*</a>\s*)?'
    r'<a class="text-body[^"]*"[^>]*href="(?P<url>/shop/product/[^"]+?)\?(?:page=\d+&amp;)?ppg=\d+">.*?'
    r'itemprop="name" content="(?P<titulo>.*?)".*?'
    r'itemprop="price" style="display:none;">(?P<price>\d+)</span>.*?'
    r'list_price.*?oe_currency_value">(?P<list_price>[\d.]+)</span></strike>.*?'
    r'product_template_id" type="hidden" value="(?P<tid>\d+)"',
    re.S,
)


def _items_desde_pagina(html_pagina: str) -> list[dict]:
    items = []
    for m in _PRODUCTO_RE.finditer(html_pagina):
        d = m.groupdict()
        precio_actual = int(d["price"])
        precio_normal = int(d["list_price"].replace(".", ""))
        if precio_normal <= precio_actual:
            continue
        descuento_pct = round((1 - precio_actual / precio_normal) * 100)
        if descuento_pct <= 0:
            continue

        items.append({
            "producto_id": d["tid"],
            "titulo": html.unescape(d["titulo"]),
            "marca": html.unescape(d["marca"]) if d["marca"] else None,
            "url": f"{_BASE_URL}{d['url']}",
            "comercio": "Geekz",
            "imagen": f"{_BASE_URL}/web/image/product.template/{d['tid']}/image/1024x1024",
            "precio_actual": precio_actual,
            "precio_normal": precio_normal,
            "descuento_pct": descuento_pct,
        })
    return items


async def _fetch_pagina(sesion, pagina: int) -> str:
    url = f"{_BASE_URL}/shop?page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")
    body = respuesta.body
    return body if isinstance(body, str) else body.decode("utf-8", errors="replace")


async def obtener_ofertas_geekz(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos, páginas leídas sin error — 0 significa que no se pudo sacar nada
    de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    items: list[dict] = []
    paginas_ok = 0
    total_paginas = None
    pagina = 1

    while total_paginas is None or pagina <= total_paginas:
        try:
            html_pagina = await _fetch_pagina(sesion, pagina)
        except Exception:
            log.exception("Falló la página %s de Geekz, se corta ahí", pagina)
            break

        paginas_ok += 1
        if total_paginas is None:
            match_total = _TOTAL_PAGINAS_RE.search(html_pagina)
            # sin data-total-page en la página 1 no hay forma confiable de saber cuándo parar —
            # se sigue igual, pero con un tope de seguridad para no quedar en loop infinito si el
            # sitio cambia de estructura (el corte real por página vacía, más abajo, ya cubre el
            # caso normal).
            total_paginas = int(match_total.group(1)) if match_total else 200

        nuevos = _items_desde_pagina(html_pagina)
        items.extend(nuevos)
        if not nuevos and pagina > 1:
            break  # página vacía antes de lo esperado, fin del catálogo real

        pagina += 1
        await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if paginas_ok == 0:
        log.error("Geekz: no se pudo leer ninguna página del catálogo")
        return [], 0

    log.info("Geekz: %s ofertas crudas en %s páginas leídas", len(items), paginas_ok)
    return items, paginas_ok
