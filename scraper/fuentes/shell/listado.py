"""Scrapea los cupones/descuentos de combustible de Shell — primera fuente del canal
"Cupones Combustible" (ver Tiendas/paginas.md, sección COMBUSTIBLES).

A diferencia de todas las demás fuentes del proyecto, esto NO son productos con precio (ver
scraper/cupones_writer.py para el modelo paralelo) — son beneficios/promos de descuento por
litro, asociados a un banco/medio de pago, sin "producto" ni precio propio.

Fuente: la API REST pública de WordPress de `https://goplus.shell.cl`, NO la grilla HTML de
`/beneficios/` (usada en una versión anterior de este archivo). La grilla solo trae título +
descripción — el día de la semana quedaba adivinado con `dias_semana.inferir_dia_semana_texto()`
sobre el texto libre, y el banco/socio quedaba siempre en `None`, porque esos datos NO están en el
HTML de la card. Confirmado con `curl` contra `goplus.shell.cl/wp-json/wp/v2/types` que el sitio
expone un post type custom "beneficio" con taxonomías reales:

- `GET /wp-json/wp/v2/beneficio?per_page=100&servicio=12&_embed=1` — lista completa, ya filtrada
  por la taxonomía "servicio" = 12 ("Combustible", confirmado contra `/wp-json/wp/v2/servicio`;
  mismo filtro que antes hacía el tag de la card, ahora por ID en vez de por texto). `_embed=1`
  trae la imagen destacada inline (`_embedded.wp:featuredmedia[0].source_url`) sin requests extra.
- Taxonomía `dia-de-la-semana` (IDs confirmados con `curl`: 11=jueves, 14=lunes, 15=martes,
  16=miércoles, 17=viernes, 18=sábado, 19=domingo) — cada beneficio trae el array de IDs en el
  campo `dia-de-la-semana`. Esto es un dato ESTRUCTURADO real, a diferencia de la inferencia de
  texto anterior — se traduce a los mismos nombres que espera `dias_semana.normalizar_dia_semana`
  (ver `_convertir_dias` más abajo), y ya no debería quedar nunca sin detectar.
- Taxonomía `alianza` (banco/app asociado, ej. Scotiabank, Tenpo, Mercado Pago) — array de IDs en
  el campo `alianza`, a veces vacío (ej. "Happy Shell", un beneficio genérico sin banco asociado).
  Antes `socio` quedaba siempre en `None` para Shell; ahora se puebla igual que Copec.
- `content.rendered` — mismo texto que la card (verificado contra varios items reales), con tags
  HTML simples (`<p>`, a veces `<figure><table>`) que se limpian con `_LIMPIAR_HTML_RE`.
- `link` — URL individual real por beneficio (ej. `.../beneficio/celebra-con-nosotros/`); antes se
  usaba la URL compartida de la grilla porque no se encontró un `<a>` individual en la card — sí
  existe, solo que vive en la API, no en el HTML de listado.

Solo un beneficio real visto trae un código explícito en el texto ("Los jueves es Diésel en
Shell" → "código DIESEL50") — se extrae con `_CODIGO_RE`. El resto de las promos de Shell no
tienen un código estático que copiar: son "paga con la tarjeta/cuenta X en la app Shell", así que
`codigo` queda en `None` para esos y la instrucción de activación se arma en
scraper/cupones_sintesis.py a partir de `socio`.

No se detectó vigencia (fecha de término) estructurada en ningún beneficio (revisado el campo
`acf`, viene vacío) — `vigencia_hasta` queda en `None`, a diferencia de Copec.
"""
from __future__ import annotations

import html
import json
import logging
import re

log = logging.getLogger("scraper.fuentes.shell")

_BASE_URL = "https://goplus.shell.cl"
_API_BASE = f"{_BASE_URL}/wp-json/wp/v2"
_URL_BENEFICIOS = f"{_API_BASE}/beneficio?per_page=100&servicio=12&_embed=1"
_SERVICIO_COMBUSTIBLE_ID = 12

_DIAS_ISO_POR_TERMINO_ID = {
    14: "lunes", 15: "martes", 16: "miercoles", 11: "jueves",
    17: "viernes", 18: "sabado", 19: "domingo",
}
_ORDEN_DIAS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")

_TAG_RE = re.compile(r"<[^>]+>")
_ESPACIOS_RE = re.compile(r"\s+")
_CODIGO_RE = re.compile(r"c[oó]digo\s+([A-Z][A-Z0-9]{2,14})\b")


def _limpiar_texto(crudo: str | None) -> str | None:
    if not crudo:
        return None
    sin_tags = _TAG_RE.sub(" ", crudo)
    texto = html.unescape(_ESPACIOS_RE.sub(" ", sin_tags)).strip()
    return texto or None


def _convertir_dias(ids_taxonomia: list[int]) -> str | None:
    """Traduce los IDs de la taxonomía `dia-de-la-semana` a un texto que
    dias_semana.normalizar_dia_semana ya sabe interpretar. `None` solo si el beneficio no trae la
    taxonomía (no debería pasar con datos reales, pero se deja como fallback defensivo)."""
    nombres = {_DIAS_ISO_POR_TERMINO_ID[i] for i in ids_taxonomia if i in _DIAS_ISO_POR_TERMINO_ID}
    if not nombres:
        return None
    if len(nombres) == len(_DIAS_ISO_POR_TERMINO_ID):
        return "Todos los días"
    return ", ".join(dia.capitalize() for dia in _ORDEN_DIAS if dia in nombres)


async def _obtener_mapa_taxonomia(sesion, taxonomia: str) -> dict[int, str]:
    try:
        respuesta = await sesion.get(f"{_API_BASE}/{taxonomia}?per_page=100")
    except Exception:
        log.exception("Shell: falló el fetch de la taxonomía %s", taxonomia)
        return {}
    if respuesta.status != 200:
        log.error("Shell: HTTP %s al leer la taxonomía %s", respuesta.status, taxonomia)
        return {}
    try:
        terminos = json.loads(respuesta.body)
    except Exception:
        log.exception("Shell: no se pudo parsear la taxonomía %s como JSON", taxonomia)
        return {}
    return {t["id"]: t["name"] for t in terminos if "id" in t and "name" in t}


def _imagen_desde_embed(beneficio: dict) -> str | None:
    embebido = beneficio.get("_embedded", {}).get("wp:featuredmedia")
    if not embebido:
        return None
    url = embebido[0].get("source_url")
    if not url:
        return None
    if url.startswith("/"):
        return f"{_BASE_URL}{url}"
    return url


def _item_desde_beneficio(beneficio: dict, mapa_alianza: dict[int, str]) -> dict | None:
    titulo = _limpiar_texto(beneficio.get("title", {}).get("rendered"))
    if not titulo:
        return None

    descripcion = _limpiar_texto(beneficio.get("content", {}).get("rendered"))

    ids_alianza = beneficio.get("alianza") or []
    nombres_alianza = [mapa_alianza[i] for i in ids_alianza if i in mapa_alianza]
    socio = ", ".join(nombres_alianza) if nombres_alianza else None

    dia_semana = _convertir_dias(beneficio.get("dia-de-la-semana") or [])

    codigo = None
    if descripcion:
        coincidencia = _CODIGO_RE.search(descripcion)
        if coincidencia:
            codigo = coincidencia.group(1)

    url_fuente = beneficio.get("link") or _BASE_URL

    return {
        "socio": socio,
        "titulo": titulo,
        "descripcion": descripcion,
        "tipo_descuento": None,
        "valor_descuento": None,
        "tope_clp": None,
        "dia_semana": dia_semana,
        "vigencia_desde": None,
        "vigencia_hasta": None,
        "codigo": codigo,
        "como_activar": None,
        "url_fuente": url_fuente,
        "imagen": _imagen_desde_embed(beneficio),
    }


async def obtener_cupones_shell(sesion) -> tuple[list[dict], int]:
    """Devuelve (cupones detectados, 1 si se pudo leer la API / 0 si falló) — mismo contrato
    (items, contador_ok) que el resto de fuentes.<tienda>.listado."""
    mapa_alianza = await _obtener_mapa_taxonomia(sesion, "alianza")

    try:
        respuesta = await sesion.get(_URL_BENEFICIOS)
    except Exception:
        log.exception("Shell: falló el fetch de %s", _URL_BENEFICIOS)
        return [], 0
    if respuesta.status != 200:
        log.error("Shell: HTTP %s en %s", respuesta.status, _URL_BENEFICIOS)
        return [], 0
    try:
        beneficios = json.loads(respuesta.body)
    except Exception:
        log.exception("Shell: no se pudo parsear %s como JSON", _URL_BENEFICIOS)
        return [], 0

    items = []
    for beneficio in beneficios:
        item = _item_desde_beneficio(beneficio, mapa_alianza)
        if item:
            items.append(item)

    if not items:
        log.error("Shell: no se detectó ningún cupón de combustible en %s", _URL_BENEFICIOS)
        return [], 0

    log.info("Shell: %s cupones de combustible detectados", len(items))
    return items, 1
