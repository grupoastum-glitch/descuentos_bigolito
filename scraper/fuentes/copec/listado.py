"""Scrapea los cupones/descuentos de combustible de Copec — segunda fuente del canal "Cupones
Combustible" (ver Tiendas/paginas.md, sección COMBUSTIBLES, y scraper/cupones_writer.py para el
modelo de datos paralelo a "oferta").

Fuente: `https://ww2.copec.cl/personas/promociones` — NO `fullcopec.cl/promociones`, que es una
SPA en Vue sin contenido en el HTML servido (confirmado con `curl`: el bundle de datos vive en un
endpoint JSON separado, sin URL documentada, requeriría navegador headless). `ww2.copec.cl` en
cambio viene **renderizada en el servidor**: confirmado con `curl` plano (sin JS) que el HTML crudo
ya trae las 18 cards completas — mismo patrón simple de fetch+parsing CSS que el resto del
proyecto, sin necesidad de Chromium.

Cada card (`article.card-promotion`) trae:
- `img.card-logo[alt]` — logo del banco/socio asociado (ej. "Logo Imagen: Rutpay"), AUSENTE en
  las promos genéricas de Copec (ej. "Domingos", "Copec Pay") — ahí el socio es "Copec".
- `span.tag-dia` — día o etiqueta de la promo, como texto libre (ej. "Domingos", "Martes",
  "Todos los días").
- Primer `p.text-normal.text-gray.font-weight-regular.mb-2` dentro de `.card-body` — título corto.
- `p.text-normal.text-gray-50.font-weight-regular` — condición completa en prosa (monto, día,
  medio de pago/código) — igual que Shell, no hay campos separados de monto/tope/código en el
  HTML, así que quedan sin extraer (None) y el caption muestra el texto tal cual.
- `a.btn-tertiary[href]` — link "Conoce más" a la promo individual (URL absoluta).
- `img.card-img[src]` — imagen ya en URL absoluta del CDN de Copec (a diferencia de Shell, no es
  un placeholder lazy-load).

La página mezcla cupones de combustible con otros beneficios de Full Copec no relacionados
(ej. "Pide tu Tarjeta de Crédito Bci con nosotros", "Beneficios MITTA Rent a Car") — no hay un tag
de categoría "Combustible" como en Shell (el filtro de categoría real del sitio es
"Medios de Pago"/"Aliados Copec"/"Socios conductores", no por rubro), así que se filtra por texto:
se descarta la card si ni el título ni la descripción mencionan "litro"/"combustible"/"estanque"
(cubre las 18 cards reales vistas al implementar esto, incluida "Mach", cuyo título no lo
menciona pero su descripción sí). No se detectó paginación — la página trae todas las promos
vigentes en una sola carga.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("scraper.fuentes.copec")

_URL_PROMOCIONES = "https://ww2.copec.cl/personas/promociones"
_PALABRAS_COMBUSTIBLE_RE = re.compile(r"litro|combustible|estanque", re.I)


def _item_desde_card(card) -> dict | None:
    titulo_nodo = card.css(".card-body p.text-normal.text-gray.font-weight-regular")
    if not titulo_nodo:
        return None
    titulo = titulo_nodo[0].get_all_text(strip=True)
    if not titulo:
        return None

    descripcion_nodo = card.css(".card-body p.text-normal.text-gray-50")
    descripcion = descripcion_nodo[0].get_all_text(strip=True) if descripcion_nodo else ""

    if not _PALABRAS_COMBUSTIBLE_RE.search(f"{titulo} {descripcion}"):
        return None

    logo_nodo = card.css("img.card-logo")
    if logo_nodo:
        alt = logo_nodo[0].attrib.get("alt") or ""
        socio = alt.split(":", 1)[-1].strip() or "Copec"
    else:
        socio = "Copec"

    dia_nodo = card.css(".tag-dia")
    dia_semana = dia_nodo[0].get_all_text(strip=True) if dia_nodo else None

    link_nodo = card.css("a.btn-tertiary")
    url_fuente = link_nodo[0].attrib.get("href") if link_nodo else _URL_PROMOCIONES

    imagen_nodo = card.css("img.card-img")
    imagen = imagen_nodo[0].attrib.get("src") if imagen_nodo else None

    return {
        "socio": socio,
        "titulo": titulo,
        "descripcion": descripcion or None,
        "tipo_descuento": None,
        "valor_descuento": None,
        "tope_clp": None,
        "dia_semana": dia_semana,
        "vigencia_desde": None,
        "vigencia_hasta": None,
        "codigo": None,
        "como_activar": None,
        "url_fuente": url_fuente,
        "imagen": imagen,
    }


async def obtener_cupones_copec(sesion) -> tuple[list[dict], int]:
    """Devuelve (cupones detectados, 1 si se pudo leer la página / 0 si falló) — mismo contrato
    (items, contador_ok) que el resto de fuentes.<tienda>.listado."""
    try:
        respuesta = await sesion.get(_URL_PROMOCIONES)
    except Exception:
        log.exception("Copec: falló el fetch de %s", _URL_PROMOCIONES)
        return [], 0
    if respuesta.status != 200:
        log.error("Copec: HTTP %s en %s", respuesta.status, _URL_PROMOCIONES)
        return [], 0

    items = []
    for card in respuesta.css("article.card-promotion"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    if not items:
        log.error("Copec: no se detectó ningún cupón de combustible en %s", _URL_PROMOCIONES)
        return [], 0

    log.info("Copec: %s cupones de combustible detectados", len(items))
    return items, 1
