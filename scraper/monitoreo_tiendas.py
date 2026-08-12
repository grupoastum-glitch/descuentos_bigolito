"""Lleva un contador de corridas fallidas consecutivas por tienda, para avisar al admin cuando
una tienda lleva varias corridas seguidas sin traer nada — sin esto, una tienda podía fallar en
silencio indefinidamente y nadie se enteraba salvo revisando el log a mano (ver incidencia
2026-08-12: Ripley falló una corrida entera sin ningún aviso).

Falabella no usa esto — ya tiene su propio mecanismo de alerta dentro del flujo de
descubrimiento de URL (ver descubrimiento.py). Este módulo es para las demás tiendas, que no
tienen descubrimiento de URL y por lo tanto no necesitan esa maquinaria — solo contar fallos.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import config

log = logging.getLogger("scraper.monitoreo_tiendas")


def _cargar(repo_dir: Path) -> dict:
    ruta = repo_dir / config.RUTA_FALLOS_TIENDAS
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError):
        return {}


def _guardar(repo_dir: Path, datos: dict) -> None:
    ruta = repo_dir / config.RUTA_FALLOS_TIENDAS
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2)


def registrar_resultado(repo_dir: Path, tienda_id: str, ok: bool) -> int:
    """Actualiza el contador de fallos consecutivos de una tienda. Devuelve el contador
    resultante (0 si ok=True, que además resetea/borra la entrada)."""
    datos = _cargar(repo_dir)

    if ok:
        if tienda_id in datos:
            del datos[tienda_id]
            _guardar(repo_dir, datos)
        return 0

    fallos = datos.get(tienda_id, 0) + 1
    datos[tienda_id] = fallos
    _guardar(repo_dir, datos)
    log.info("%s: fallos consecutivos = %s", tienda_id, fallos)
    return fallos
