"""Fallback de descubrimiento: cuando una URL de listado conocida deja de funcionar (el
guardrail de main.py detecta paginas_ok == 0), este módulo arma una lista corta de URLs
candidatas (sin LLM) y le pide a Claude Haiku 4.5 que elija cuál es la página vigente de
ofertas. No extrae datos — solo encuentra la URL correcta; la extracción sigue haciéndola
fuentes/listado.py con el parser __NEXT_DATA__ existente.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import config

log = logging.getLogger("scraper.descubrimiento")

_PALABRAS_CLAVE = ("oferta", "ofertas", "descuento", "descuentos", "collection", "page")
_HREF_RE = re.compile(r'href="([^"]+)"')
_MAX_CANDIDATAS = 30


@dataclass
class ResultadoDescubrimiento:
    url: str
    confianza: str  # "alta" | "media" | "baja"
    razonamiento: str


def _es_candidata_relevante(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(palabra in path for palabra in _PALABRAS_CLAVE)


async def _candidatas_desde_sitemap(sesion, home_url: str) -> list[str]:
    sitemap_url = urljoin(home_url, "/sitemap.xml")
    try:
        respuesta = await sesion.get(sitemap_url)
        if respuesta.status != 200:
            return []
        raiz = ElementTree.fromstring(respuesta.body)
    except Exception:
        log.info("No se pudo leer/parsear %s, se sigue con links de la home", sitemap_url)
        return []

    urls = [elemento.text.strip() for elemento in raiz.iter() if elemento.tag.endswith("loc") and elemento.text]
    return [url for url in urls if _es_candidata_relevante(url)]


def _candidatas_desde_home(home_url: str, home_html: str) -> list[str]:
    hrefs = _HREF_RE.findall(home_html)
    absolutas = {urljoin(home_url, html.unescape(href)) for href in hrefs}
    return [url for url in absolutas if _es_candidata_relevante(url)]


async def mapear_candidatas(sesion, home_url: str, home_html: str) -> list[str]:
    """Lista corta de URLs candidatas a ser la página vigente de ofertas. Sin LLM."""
    candidatas = await _candidatas_desde_sitemap(sesion, home_url)
    if not candidatas:
        candidatas = _candidatas_desde_home(home_url, home_html)

    candidatas = sorted(set(candidatas))[:_MAX_CANDIDATAS]
    log.info("Descubrimiento: %s URLs candidatas encontradas", len(candidatas))
    return candidatas


def elegir_url_ofertas(candidatas: list[str]) -> ResultadoDescubrimiento | None:
    """Le pide a Claude Haiku 4.5 que elija, entre las candidatas, cuál es la página vigente
    de ofertas/descuentos. Devuelve None si no hay API key, no hay candidatas, o la respuesta
    no se puede interpretar."""
    if not candidatas:
        log.warning("Descubrimiento: no hay URLs candidatas, no se llama al LLM")
        return None
    if not config.ANTHROPIC_API_KEY:
        log.error("Descubrimiento: falta ANTHROPIC_API_KEY, no se puede elegir URL automáticamente")
        return None

    import anthropic

    cliente = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        "Estas son URLs candidatas encontradas en un sitio de e-commerce. Buscamos la página "
        "GENERAL de ofertas del sitio: un listado multi-categoría (electro, ropa, hogar, "
        "perfumería, etc. mezclados) filtrado a productos con descuento, NO una categoría "
        "específica (ej. 'bicicletas-infantiles', 'ropa-hombre') aunque tenga un filtro de "
        "descuento aplicado en la URL — eso sigue siendo una sola categoría, no sirve.\n\n"
        "Si ninguna candidata es claramente esa página general, elegí igual la que más se "
        "le acerque pero marcá confianza 'baja' — no inventes confianza 'alta' ni 'media' "
        "para una categoría específica solo porque tiene un filtro de descuento en la URL.\n\n"
        "URLs candidatas:\n" + "\n".join(candidatas)
    )
    herramienta = {
        "name": "elegir_url_ofertas",
        "description": "Registra cuál de las URLs candidatas es la página vigente de ofertas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "La URL elegida, tal cual aparece en la lista de candidatas."},
                "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
                "razonamiento": {"type": "string"},
            },
            "required": ["url", "confianza", "razonamiento"],
        },
    }

    try:
        respuesta = cliente.messages.create(
            model=config.ANTHROPIC_MODEL_DESCUBRIMIENTO,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
            tools=[herramienta],
            tool_choice={"type": "tool", "name": "elegir_url_ofertas"},
        )
        bloque = next(b for b in respuesta.content if b.type == "tool_use")
        datos = bloque.input
        return ResultadoDescubrimiento(
            url=datos["url"],
            confianza=datos["confianza"],
            razonamiento=datos.get("razonamiento", ""),
        )
    except Exception:
        log.exception("Descubrimiento: falló la llamada a Claude o el parseo de su respuesta")
        return None


def cargar_cache(repo_dir: Path) -> dict:
    ruta = repo_dir / config.RUTA_RUTAS_DESCUBIERTAS
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return {}


def guardar_cache(repo_dir: Path, clave: str, resultado: ResultadoDescubrimiento) -> None:
    ruta = repo_dir / config.RUTA_RUTAS_DESCUBIERTAS
    cache = cargar_cache(repo_dir)
    cache[clave] = {
        "url": resultado.url,
        "confianza": resultado.confianza,
        "razonamiento": resultado.razonamiento,
    }
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(cache, archivo, ensure_ascii=False, indent=2)
    log.info("Descubrimiento: URL cacheada para %s -> %s (confianza %s)", clave, resultado.url, resultado.confianza)


# --- Memoria de intentos fallidos: evita que el LLM vuelva a proponer una URL que ya se
# probó y no sirvió, y permite avisar después de N corridas fallidas seguidas. ---

def cargar_historial_fallidos(repo_dir: Path) -> dict:
    ruta = repo_dir / config.RUTA_DESCUBRIMIENTO_FALLIDOS
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return {}


def urls_descartadas(repo_dir: Path, clave: str) -> list[str]:
    historial = cargar_historial_fallidos(repo_dir)
    return historial.get(clave, {}).get("urls_descartadas", [])


def registrar_intento_fallido(repo_dir: Path, clave: str, urls_a_descartar: list[str]) -> int:
    """Suma urls_a_descartar a la lista de URLs ya probadas (no se le van a volver a ofrecer
    al LLM) e incrementa el contador de fallos consecutivos. Devuelve el contador actualizado."""
    ruta = repo_dir / config.RUTA_DESCUBRIMIENTO_FALLIDOS
    historial = cargar_historial_fallidos(repo_dir)
    registro = historial.setdefault(clave, {"urls_descartadas": [], "fallos_consecutivos": 0})

    descartadas = set(registro["urls_descartadas"]) | set(urls_a_descartar)
    registro["urls_descartadas"] = sorted(descartadas)
    registro["fallos_consecutivos"] = registro.get("fallos_consecutivos", 0) + 1
    registro["ultimo_intento"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(historial, archivo, ensure_ascii=False, indent=2)

    log.info(
        "Descubrimiento: intento fallido registrado para %s (fallos consecutivos: %s)",
        clave, registro["fallos_consecutivos"],
    )
    return registro["fallos_consecutivos"]


def limpiar_historial_fallidos(repo_dir: Path, clave: str) -> None:
    """Se llama cuando el descubrimiento vuelve a funcionar: resetea el contador y la lista
    de URLs descartadas para ese sitio."""
    ruta = repo_dir / config.RUTA_DESCUBRIMIENTO_FALLIDOS
    historial = cargar_historial_fallidos(repo_dir)
    if clave not in historial:
        return
    del historial[clave]
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(historial, archivo, ensure_ascii=False, indent=2)
