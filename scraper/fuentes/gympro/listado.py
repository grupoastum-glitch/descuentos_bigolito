"""Scrapea las ofertas de GymPro (gympro.cl) — tercera tienda del rubro Fitness.

PrestaShop (confirmado por los strings `prestashop`/`PrestaShop` en el HTML) — primera tienda del
proyecto sobre esta plataforma. Sin Cloudflare, responde 200 con fetch plano.

**Catálogo mucho más amplio que "deportes"**: además de Deportes/Fuerza/Ropa Deportiva/
Estructuras Deportivas/Suplementos (todo dentro de scope del canal Fitness), el sitio también
vende Ortopedia (sillas de ruedas, pañales, termómetros — equipo médico/clínico), Defensa
Personal/Airsoft (armas de aire comprimido, gas pimienta), Copas y Trofeos/Juegos y Educación
(medallas, juegos de mesa infantiles), y armas de fuego réplica/cuchillería (pistolas y
revólveres de CO2, cuchillos/cortaplumas) — decisión confirmada con el usuario (en 2 rondas, ver
`_TITULO_EXCLUIDO_RE` abajo): se excluyen todas esas categorías. **Ojo**: las URLs de producto NO
anidan bajo la categoría padre — usan directo el slug de la categoría específica asignada al
producto (confirmado en vivo: un producto de "Juegos Educativos" salió con URL
`/juegos-educativos/...`, no `/juegos-y-educacion/juegos-educativos/...`; productos de armas/
cuchillería aparecieron incluso bajo `deportes`/`outdoor`/`inicio`), así que no alcanza con
excluir por categoría — `_CATEGORIAS_EXCLUIDAS` cubre las subcategorías reales de
Ortopedia/Defensa Personal/Copas y Trofeos/Juegos y Educación (cosechadas del menú de la home,
mismo criterio que SuperZoo/Sodimac/MyShop/PC Express), y `_TITULO_EXCLUIDO_RE` cubre por
palabras clave del título lo que se cuela con categoría genérica (armas, cuchillería, gas
pimienta/lacrimógeno, gear táctico/militar) — con cuidado de no descartar de más: "táctico" y
"manopla" solos NO se usan como filtro porque también aparecen en productos legítimos (pizarras
tácticas de fútbol para entrenadores, manoplas de natación). Riesgo residual aceptado: si
existiera alguna subcategoría de las 4 ramas no enlazada desde la home, o algún producto de
armas/cuchillería con un título que no matchee ninguna palabra clave, no estaría cubierto.

Se usa `/productos-rebajados` (controller `prices-drop` de PrestaShop, resuelto a esa URL
amigable), con `?resultsPerPage=36` (probado que `resultsPerPage=9999999` para traer todo en un
solo request hace timeout — 36 es un valor real que ofrece el selector de la propia UI y responde
rápido). Confirmado en vivo: **3371 productos, 94 páginas** (`?page=N`, página 94 con 23
tarjetas). Decisión del usuario: sin muestreo, se leen las 94 páginas completas cada corrida.

Cada tarjeta (`article.product-miniature[data-id-product]`) trae precio actual numérico y limpio
en el atributo `content` de `.product-price` (mismo patrón que SuperZoo/Decathlon) y precio
normal como texto en `.regular-price` (solo aparece si el producto está realmente en oferta).
Imagen: ojo que `src` trae un placeholder de lazy-load (`blank.png`), la URL real está en
`data-src`.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import re
import unicodedata

import config

log = logging.getLogger("scraper.fuentes.gympro")

_BASE_URL = "https://www.gympro.cl"
_REBAJADOS_URL = f"{_BASE_URL}/productos-rebajados"

_PRODUCTOS_POR_PAGINA = 36  # ?resultsPerPage=36, confirmado en vivo (9999999 hace timeout)

_CATEGORIAS_EXCLUIDAS = {
    # Las URLs de producto de GymPro NO anidan bajo la categoría padre — usan directo el slug de
    # la categoría específica asignada al producto (confirmado: un producto de "Juegos
    # Educativos" salió con URL `/juegos-educativos/...`, no `/juegos-y-educacion/
    # juegos-educativos/...`). Por eso hace falta la lista completa de slugs raíz + sus
    # subcategorías reales (cosechadas del menú de la home), no solo los 4 slugs de nivel top.
    "ortopedia",
    "adulto-mayor", "asientos-ducha", "bastones-y-andadores", "colchon-antiescaras",
    "silla-de-ruedas-y-cojines-picarones", "urinarios",
    "articulos-medicos", "cinta-de-medicion", "medidor-presion-y-oximetros",
    "mobiliario-clinicos", "modelos-anatomicos", "otoscopio", "pesa-digital-caliper",
    "respiratorio", "sabanas-clinicas", "termometros", "ventosas-vacumterapia",
    "descansa-al-dormir", "anti-estres", "anti-ronquido", "descanso",
    "embarazo-y-bebe", "chupetes-mamaderas-y-extractores", "cuidado-de-la-mama",
    "cuidado-para-el-bebe-", "faja-embarazadas",
    "extremidad-inferior", "caderas", "cuidado-del-pies", "fajas-e-inguinales",
    "media-antiembolica", "muslo-y-pantorrilla", "plantillas-ortopedicas",
    "rodilla-y-meniscos", "talon-y-tobillo",
    "extremidad-superior", "brazo-y-codo", "cabeza-y-cervical", "corrector-de-postura",
    "hombros", "mano-y-muneca",
    "kinesiologia-y-masaje", "bandas-elasticas", "rehabilitacion-y-masaje",
    "tens-y-electroestimuladores", "terapias",
    "primeros-auxilios", "alcohol-gel", "algodon-y-gasas", "botiquin",
    "compresas-de-frio-y-calor", "vendas-cintas-parches-y-tape",
    "defensa-personal",
    "copas-y-trofeos", "accesorios-premiacion", "copas", "galvanos", "medallas",
    "sellos-deportivos", "trofeos",
    "juegos-y-educacion", "balones-multipropositos", "frisbie", "juegos-de-mesa",
    "juegos-educativos", "juegos-psicomotricidad", "muebles-infantiles", "pool-billar",
    "accesorios-de-pool", "bolas-de-pool", "tacos-de-pool", "tiro-al-blanco", "taca-taca",
    "airsoft-militar",
}

# Segundo filtro, por título: varios productos de armas/defensa personal salieron con
# `producto_id`/URL bajo categorías genéricas (`deportes`, `outdoor`, `bicicleta-y-ciclismo`,
# `inicio`) en vez de `airsoft-militar`/`defensa-personal` — el filtro de categoría de arriba no
# los agarra. Confirmado en vivo, en 2 rondas: balines, chaleco/guantes/rodilleras tácticas,
# bastón retráctil, manopla metálica y gas lacrimógeno (1ra ronda); pistolas/revólveres de CO2
# (réplicas de arma de fuego), gas pimienta y cuchillos/cortaplumas —decisión del usuario: se
# excluyen todos los cuchillos/cortaplumas, básicos o tácticos, no solo los "táctico/militar"
# (2da ronda). Mismo criterio ya usado en el proyecto para descartar productos por título (ver
# LuffyToys/WePlay/Bestmart/Dust2 con "Preventa"), acá con frases específicas para no descartar de
# más — ojo que "táctico"/"manopla" solos NO se usan como palabra suelta porque también aparecen
# en productos legítimos (pizarras tácticas de fútbol/básquetbol para entrenadores, manoplas de
# natación/paletas de nado) — confirmado que esos NO deben excluirse.
_TITULO_EXCLUIDO_RE = re.compile(
    r"airsoft|balines|lacrimogeno|defensa personal|manopla metal|baston retractil|"
    r"chaleco tactico|guantes? tactico|rodilleras? tactica|paintball|"
    r"\bpistola|\brevolver|gas pimienta|\bcuchillo|cortapluma",
    re.I,
)


def _normalizar(texto: str) -> str:
    forma = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in forma if not unicodedata.combining(c))


_TOTAL_RE = re.compile(r'productos-rebajados\?page=(\d+)')
_SEGMENTO_RAIZ_RE = re.compile(r"^https?://[^/]+/([^/]+)/")
_PRECIO_RE = re.compile(r"[\d.]+")


def _parse_precio(texto: str) -> int | None:
    m = _PRECIO_RE.search(texto or "")
    if not m:
        return None
    return int(m.group(0).replace(".", ""))


def _categoria_excluida(url: str) -> bool:
    m = _SEGMENTO_RAIZ_RE.match(url)
    return bool(m and m.group(1) in _CATEGORIAS_EXCLUIDAS)


def _titulo_excluido(titulo: str) -> bool:
    return bool(_TITULO_EXCLUIDO_RE.search(_normalizar(titulo)))


def _item_desde_card(card) -> dict | None:
    producto_id = card.attrib.get("data-id-product")
    if not producto_id:
        return None

    actual_nodo = card.css(".product-price")
    if not actual_nodo:
        return None
    precio_actual = _parse_precio(actual_nodo[0].attrib.get("content"))

    normal_nodo = card.css(".regular-price")
    if not normal_nodo:
        return None  # sin precio tachado, este producto no está en oferta real
    precio_normal = _parse_precio(normal_nodo[0].get_all_text(strip=True))
    if not precio_actual or not precio_normal or precio_normal <= precio_actual:
        return None

    descuento_pct = round((1 - precio_actual / precio_normal) * 100)
    if descuento_pct <= 0:
        return None

    titulo_nodo = card.css(".product-title a")
    if not titulo_nodo:
        return None
    url = titulo_nodo[0].attrib.get("href")
    if not url:
        return None
    if _categoria_excluida(url):
        return None
    titulo = titulo_nodo[0].get_all_text(strip=True)
    if _titulo_excluido(titulo):
        return None

    marca_nodo = card.css(".product-brand a")
    marca = marca_nodo[0].get_all_text(strip=True) if marca_nodo else None
    imagen_nodo = card.css("img.product-thumbnail-first")
    imagen = imagen_nodo[0].attrib.get("data-src") if imagen_nodo else None

    return {
        "producto_id": str(producto_id),
        "titulo": titulo,
        "marca": marca,
        "url": url,
        "comercio": "GymPro",
        "imagen": imagen,
        "precio_actual": precio_actual,
        "precio_normal": precio_normal,
        "descuento_pct": descuento_pct,
    }


async def _fetch_pagina(sesion, pagina: int) -> tuple[list[dict], int]:
    """Devuelve (items de esa página, total de páginas real de la fuente — sale de los links de
    paginación `productos-rebajados?page=N` del propio HTML)."""
    url = f"{_REBAJADOS_URL}?resultsPerPage={_PRODUCTOS_POR_PAGINA}"
    if pagina > 1:
        url = f"{url}&page={pagina}"
    respuesta = await sesion.get(url)
    if respuesta.status != 200:
        raise RuntimeError(f"HTTP {respuesta.status} en {url}")

    items = []
    for card in respuesta.css("article.product-miniature"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    html_ = respuesta.body.decode("utf-8", errors="replace")
    paginas_vistas = [int(p) for p in _TOTAL_RE.findall(html_)]
    total_paginas = max(paginas_vistas) if paginas_vistas else 1
    return items, total_paginas


async def obtener_ofertas_gympro(sesion) -> tuple[list[dict], int]:
    """Devuelve (items crudos sin duplicados, páginas leídas sin error — 0 significa que no se
    pudo sacar nada de esta fuente). Mismo contrato que el resto de fuentes.<tienda>.listado."""
    semaforo = asyncio.Semaphore(config.CONCURRENCIA_LISTADO)

    async with semaforo:
        try:
            items, total_paginas = await _fetch_pagina(sesion, 1)
        except Exception:
            log.exception("Falló la página 1 de las ofertas de GymPro, se aborta")
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
                log.exception("Falló la página %s de las ofertas de GymPro, se omite esa página", pagina)
                return None
            finally:
                await asyncio.sleep(random.uniform(config.DELAY_MIN_SEGUNDOS, config.DELAY_MAX_SEGUNDOS))

    if total_paginas > 1:
        resultados = await asyncio.gather(*(
            _fetch_extra(pagina) for pagina in range(2, total_paginas + 1)
        ), return_exceptions=True)
        for pagina, resultado in zip(range(2, total_paginas + 1), resultados):
            if isinstance(resultado, BaseException):
                log.error("Excepción no anticipada en la página %s de GymPro, se omite: %r", pagina, resultado)
                continue
            if resultado is None:
                continue
            paginas_ok += 1
            todos.extend(resultado)

    if not todos:
        log.error("GymPro: no se detectó ninguna oferta en las %s páginas", total_paginas)
        return [], 0

    vistos: set[str] = set()
    items_finales: list[dict] = []
    for item in todos:
        if item["producto_id"] in vistos:
            continue
        vistos.add(item["producto_id"])
        items_finales.append(item)

    log.info(
        "GymPro: %s ofertas crudas (sin duplicados) desde %s/%s páginas",
        len(items_finales), paginas_ok, total_paginas,
    )
    return items_finales, paginas_ok
