"""Normaliza el día de la semana de un cupón de combustible (texto libre, sin campo estructurado
en ninguna de las dos fuentes reales — ver scraper/fuentes/copec|shell/listado.py) a un valor
comparable contra "qué día es hoy" — usado por scraper/cupones_writer.py para armar el digest
diario (solo entran al digest de hoy los cupones cuyo día aplica hoy, más los de "todos los días").

Mismo patrón de normalización de texto (NFKD + descartar combining chars) que ya usa
scraper/fuentes/gympro/listado.py::_normalizar, para no depender de que el texto de origen venga
con o sin tildes.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

import config

DIAS_ISO = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")

# En español, de estos 7 solo sábado/domingo pluralizan (sábados/domingos) — el resto son
# invariables singular=plural, la tabla de alias queda chica a propósito.
_ALIAS_PLURAL = {"sabados": "sabado", "domingos": "domingo"}

_DIA_ALT = "|".join(DIAS_ISO[:5]) + "|sabados?|domingos?"
_DIA_RE = re.compile(rf"\b({_DIA_ALT})\b")
_RANGO_RE = re.compile(rf"\b(?:de\s+)?({_DIA_ALT})\s+a\s+({_DIA_ALT})\b")
_TODOS_RE = re.compile(r"\btodos\s+los\s+d[ií]as\b")


def _normalizar(texto: str) -> str:
    forma = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in forma if not unicodedata.combining(c))


def _canon(palabra: str) -> str:
    return _ALIAS_PLURAL.get(palabra, palabra)


def _expandir_rango(dia_desde: str, dia_hasta: str) -> frozenset[str]:
    i = DIAS_ISO.index(dia_desde)
    j = DIAS_ISO.index(dia_hasta)
    if j >= i:
        return frozenset(DIAS_ISO[i:j + 1])
    return frozenset(DIAS_ISO[i:] + DIAS_ISO[:j + 1])  # wraparound, ej. "de sábado a martes"


def normalizar_dia_semana(texto: str | None) -> frozenset[str]:
    """Devuelve un set de días ISO en minúscula sin tilde, o {"todos"} si el texto no restringe
    a ningún día en particular (vacío, "Todos los días", o sin ninguna mención de día)."""
    if not texto:
        return frozenset({"todos"})
    plano = _normalizar(texto).lower()
    if _TODOS_RE.search(plano):
        return frozenset({"todos"})

    rango = _RANGO_RE.search(plano)
    if rango:
        return _expandir_rango(_canon(rango.group(1)), _canon(rango.group(2)))

    dias = {_canon(d) for d in _DIA_RE.findall(plano)}
    return frozenset(dias) if dias else frozenset({"todos"})


def inferir_dia_semana_texto(*fragmentos: str | None) -> str | None:
    """Para fuentes sin campo de día propio (Shell): infiere el día leyendo título+descripción.
    Devuelve None si no se detecta ningún día específico (equivalente a "todos los días" — no
    hay nada útil que guardar en el campo dia_semana), o un string legible (ej. "Jueves",
    "Viernes, Sábado, Domingo") si se detectó alguno."""
    texto = " ".join(f for f in fragmentos if f)
    dias = normalizar_dia_semana(texto)
    if dias == frozenset({"todos"}):
        return None
    return ", ".join(d.capitalize() for d in DIAS_ISO if d in dias)


def nombre_dia_chile(ahora: datetime | None = None) -> str:
    momento = ahora or datetime.now(config.TZ_CHILE)
    return DIAS_ISO[momento.weekday()]


def dia_aplica_hoy(dias: frozenset[str], hoy: str | None = None) -> bool:
    if "todos" in dias:
        return True
    return (hoy or nombre_dia_chile()) in dias
