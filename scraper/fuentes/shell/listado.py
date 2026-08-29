"""Scrapea los cupones/descuentos de combustible de Shell — primera fuente del canal
"Cupones Combustible" (ver Tiendas/paginas.md, sección COMBUSTIBLES).

A diferencia de todas las demás fuentes del proyecto, esto NO son productos con precio (ver
scraper/cupones_writer.py para el modelo paralelo) — son beneficios/promos de descuento por
litro, asociados a un banco/medio de pago, sin "producto" ni precio propio.

Fuente: `https://goplus.shell.cl/beneficios/` — página de WordPress renderizada en el servidor
(confirmado con `curl` plano, sin JS: el HTML crudo ya trae las cards completas), sin necesidad de
navegador headless. La página lista beneficios de Shell en general (hay un filtro "Servicio" con
un <select>), así que se filtra por `span.beneficio-card__tag == "Combustible"` en cada card — a
la fecha de esta implementación las 10 cards visibles son 100% combustible, pero el filtro es
defensivo por si Shell agrega beneficios de otro rubro más adelante.

Cada card (`div.beneficio-card`) trae:
- `h3.beneficio-card__title` — título corto (ej. "Los jueves es Diésel en Shell").
- `div.beneficio-card__excerpt p` — condición completa en prosa (banco/app, monto, día, cómo
  activarlo) — a diferencia de Copec, acá no hay campos separados de banco/monto/código, todo
  vive en este texto libre, así que el caption de Telegram se arma mostrando título+descripción
  tal cual, sin intentar parsear un monto o banco por separado.
- `img[data-lazy-src]` — imagen real (el `src` visible es un placeholder SVG de lazy-load).
- Primer `div.beneficio-card__meta span` — fecha de vigencia como texto libre (ej.
  "30 septiembre 2026"); el segundo `.beneficio-card__meta` de la card no es vigencia (a veces
  vacío, a veces un tag suelto tipo "Upa!") y se ignora.

Shell no expone ningún campo de día de la semana (a diferencia de Copec, que sí trae `tag-dia`) —
el día casi siempre está mencionado en prosa dentro del título/descripción (ej. "Los jueves es
Diésel en Shell"), así que se infiere con `dias_semana.inferir_dia_semana_texto()` sobre
título+descripción — usado por el digest diario (ver scraper/cupones_writer.py) para decidir si
este cupón aplica hoy. Si no se detecta ningún día, `dia_semana` queda en `None` (el digest lo
trata como "todos los días").

No hay link individual por card (confirmado: ningún `<a>` dentro de `.beneficio-card`) — todas las
promos comparten `url_fuente` = la página de beneficios.
"""
from __future__ import annotations

import logging

import dias_semana

log = logging.getLogger("scraper.fuentes.shell")

_BASE_URL = "https://goplus.shell.cl"
_URL_BENEFICIOS = f"{_BASE_URL}/beneficios/"


def _texto_meta_vigencia(card) -> str | None:
    metas = card.css(".beneficio-card__meta")
    if not metas:
        return None
    spans = metas[0].css("span")
    if not spans:
        return None
    texto = spans[0].get_all_text(strip=True)
    return texto or None


def _item_desde_card(card) -> dict | None:
    tag_nodo = card.css(".beneficio-card__tag")
    tag = tag_nodo[0].get_all_text(strip=True) if tag_nodo else ""
    if tag.strip().lower() != "combustible":
        return None

    titulo_nodo = card.css(".beneficio-card__title")
    if not titulo_nodo:
        return None
    titulo = titulo_nodo[0].get_all_text(strip=True)
    if not titulo:
        return None

    descripcion_nodo = card.css(".beneficio-card__excerpt p")
    descripcion = descripcion_nodo[0].get_all_text(strip=True) if descripcion_nodo else None

    imagen_nodo = card.css("img")
    imagen = imagen_nodo[0].attrib.get("data-lazy-src") if imagen_nodo else None
    if imagen and imagen.startswith("/"):
        imagen = f"{_BASE_URL}{imagen}"

    return {
        "socio": None,  # sin campo separado en esta fuente — el banco/app queda en el texto
        "titulo": titulo,
        "descripcion": descripcion,
        "tipo_descuento": None,
        "valor_descuento": None,
        "tope_clp": None,
        "dia_semana": dias_semana.inferir_dia_semana_texto(titulo, descripcion),
        "vigencia_desde": None,
        "vigencia_hasta": _texto_meta_vigencia(card),
        "codigo": None,
        "como_activar": None,
        "url_fuente": _URL_BENEFICIOS,
        "imagen": imagen,
    }


async def obtener_cupones_shell(sesion) -> tuple[list[dict], int]:
    """Devuelve (cupones detectados, 1 si se pudo leer la página / 0 si falló) — mismo contrato
    (items, contador_ok) que el resto de fuentes.<tienda>.listado, con "páginas/categorías" que
    acá es simplemente "se leyó la página o no" (una sola página, sin paginación)."""
    try:
        respuesta = await sesion.get(_URL_BENEFICIOS)
    except Exception:
        log.exception("Shell: falló el fetch de %s", _URL_BENEFICIOS)
        return [], 0
    if respuesta.status != 200:
        log.error("Shell: HTTP %s en %s", respuesta.status, _URL_BENEFICIOS)
        return [], 0

    items = []
    for card in respuesta.css(".beneficio-card"):
        item = _item_desde_card(card)
        if item:
            items.append(item)

    if not items:
        log.error("Shell: no se detectó ningún cupón de combustible en %s", _URL_BENEFICIOS)
        return [], 0

    log.info("Shell: %s cupones de combustible detectados", len(items))
    return items, 1
