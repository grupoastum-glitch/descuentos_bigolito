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
se descarta la card si ni el título ni la descripción mencionan "litro"/"combustible" (cubre las
18 cards reales vistas al implementar esto, incluida "Mach", cuyo título no lo menciona pero su
descripción sí).

Ese filtro de inclusión no alcanza solo: "Pide tu Tarjeta de Crédito Bci con nosotros" (promo de
adquisición de tarjeta, no un descuento recurrente) coló la primera vez por mencionar "estanque
gratis" — se sacó "estanque" del patrón de inclusión, ninguna promo genuina lo necesita. "Beneficios
MITTA Rent a Car" (arriendo de auto) sí dice literalmente "descuento por litro de combustible" en
su texto — un filtro de puro texto no puede distinguirlo de un cupón genuino, así que se suma un
patrón de EXCLUSIÓN explícito (`_EXCLUSION_RE`, mismo criterio pragmático que
`scraper/fuentes/gympro/listado.py::_TITULO_EXCLUIDO_RE`) para estos casos conocidos — lista a
ampliar si aparecen más falsos positivos, no hay una señal estructurada limpia para esto.

No se detectó paginación — la página trae todas las promos vigentes en una sola carga.

Segunda pasada — página de detalle: la card de listado NO trae "cómo se activa" (código vs.
tarjeta) ni la vigencia (fecha de inicio/término) — esos campos solo existen en la página de
detalle de cada promo (`a.btn-tertiary[href]`, ya capturado como `url_fuente` pero nunca
visitado). Confirmado con `curl` contra `/personas/promociones/caja-los-andes`: el bloque
`.products-detail__product` trae, en HTML simple sin JS:
- Un `<p>Uso exclusivo con</p>` seguido de un `span.tag-dia` con el método ("Código", "Tarjeta").
- Un `<p>Vigencia</p>` seguido de un `<span>` con el texto "Válido desde DD/MM/YY - hasta el
  DD/MM/YY" — se parsea con `_VIGENCIA_RE` a fechas ISO (asumiendo siglo 20XX, único formato visto).

Confirmado además (probando la URL de listado con `?fields.Fecha+de+inicio[lte]=<hoy>&fields.Fecha
+de+término[gt]=<hoy>`, el mismo filtro que usa el propio sitio) que `/personas/promociones` SIN
filtrar ya excluye promos vencidas — por eso la vigencia acá es solo para MOSTRAR en el digest
("vence DD/MM"), no hace falta filtrar por ella.

Esto agrega un request extra por promo que pasó el filtro de combustible (~16-18 por corrida) —
aceptable para un cron que corre una vez por hora.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("scraper.fuentes.copec")

_URL_PROMOCIONES = "https://ww2.copec.cl/personas/promociones"
_PALABRAS_COMBUSTIBLE_RE = re.compile(r"litro|combustible", re.I)
_EXCLUSION_RE = re.compile(r"tarjeta de cr[eé]dito.*con nosotros|rent a car|arrendar", re.I)
_VIGENCIA_RE = re.compile(
    r"desde\s+(\d{1,2})/(\d{1,2})/(\d{2})\s*-\s*hasta\s+el\s+(\d{1,2})/(\d{1,2})/(\d{2})", re.I
)


def _item_desde_card(card) -> dict | None:
    titulo_nodo = card.css(".card-body p.text-normal.text-gray.font-weight-regular")
    if not titulo_nodo:
        return None
    titulo = titulo_nodo[0].get_all_text(strip=True)
    if not titulo:
        return None

    descripcion_nodo = card.css(".card-body p.text-normal.text-gray-50")
    descripcion = descripcion_nodo[0].get_all_text(strip=True) if descripcion_nodo else ""

    texto_completo = f"{titulo} {descripcion}"
    if not _PALABRAS_COMBUSTIBLE_RE.search(texto_completo) or _EXCLUSION_RE.search(texto_completo):
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


def _siglo_completo(anio_corto: str) -> int:
    return 2000 + int(anio_corto)


async def _enriquecer_con_detalle(sesion, item: dict) -> None:
    """Visita la página de detalle de la promo (`item["url_fuente"]`) y completa `como_activar`
    y `vigencia_desde`/`vigencia_hasta` in-place. Cualquier falla acá se ignora en silencio — el
    item ya es válido con los datos de la card, esto es solo un enriquecimiento best-effort."""
    try:
        respuesta = await sesion.get(item["url_fuente"])
    except Exception:
        log.warning("Copec: falló el fetch del detalle de %r", item["titulo"])
        return
    if respuesta.status != 200:
        log.warning("Copec: HTTP %s en el detalle de %r", respuesta.status, item["titulo"])
        return

    detalle = respuesta.css(".products-detail__product")
    if not detalle:
        return
    nodo = detalle[0]

    # Estructura real (inspeccionada con curl): el primer span.tag-dia es el día (ya lo tenemos de
    # la card); el segundo, si existe, es el método bajo "Uso exclusivo con" (Código/Tarjeta).
    metodos = nodo.css("span.tag-dia")
    if len(metodos) >= 2:
        metodo = metodos[1].get_all_text(strip=True)
        if metodo:
            item["como_activar"] = metodo

    for span in nodo.css("span"):
        texto = span.get_all_text(strip=True)
        coincidencia = _VIGENCIA_RE.search(texto)
        if coincidencia:
            d1, m1, a1, d2, m2, a2 = coincidencia.groups()
            item["vigencia_desde"] = f"{_siglo_completo(a1)}-{int(m1):02d}-{int(d1):02d}"
            item["vigencia_hasta"] = f"{_siglo_completo(a2)}-{int(m2):02d}-{int(d2):02d}"
            break


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

    for item in items:
        await _enriquecer_con_detalle(sesion, item)

    log.info("Copec: %s cupones de combustible detectados", len(items))
    return items, 1
