"""Utilidades compartidas para leer el bloque __NEXT_DATA__ que Falabella embebe en sus páginas.

Falabella es una app Next.js: tanto el listado de ofertas como la página de producto incluyen
un <script id="__NEXT_DATA__"> con los props ya armados en JSON. Leer ese bloque es mucho más
confiable que parsear clases CSS (que son hashes jsx-XXXXX que cambian con cada build).
"""
from __future__ import annotations

import json
import re

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def extraer_next_data(html: str) -> dict:
    coincidencia = _NEXT_DATA_RE.search(html)
    if not coincidencia:
        raise ValueError("No se encontró el bloque __NEXT_DATA__ en la página")
    return json.loads(coincidencia.group(1))


def parsear_precio(texto: str) -> int:
    """'79.900' -> 79900 (formato chileno, punto como separador de miles)."""
    return int(texto.replace(".", "").replace(",", "").strip())


def precios_desde_lista(prices: list[dict]) -> tuple[int | None, int | None]:
    """Devuelve (precio_actual, precio_normal) a partir del array 'prices' de Falabella."""
    actual = normal = None
    for entrada in prices:
        valores = entrada.get("price") or []
        if not valores:
            continue
        valor = parsear_precio(valores[0])
        tipo = entrada.get("type")
        if tipo == "internetPrice":
            actual = valor
        elif tipo == "normalPrice":
            normal = valor
        elif entrada.get("crossed") and normal is None:
            normal = valor
        elif not entrada.get("crossed") and actual is None:
            actual = valor
    return actual, normal


def calcular_descuento_pct(precio_actual: int | None, precio_normal: int | None, badge_label: str | None) -> int:
    """Prioriza el % que Falabella ya muestra (discountBadge); si no está, lo calcula."""
    if badge_label:
        numeros = re.findall(r"\d+", badge_label)
        if numeros:
            return int(numeros[0])
    if precio_actual is not None and precio_normal:
        return round((1 - precio_actual / precio_normal) * 100)
    return 0
