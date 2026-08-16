"""Lee web/data/config.json::canales_pagos — misma fuente de verdad que usa bot/bot.py
(cargar_config/_canales_pagos) para no duplicar monto/frecuencia a mano en este servicio (a
diferencia de CANAL_CHAT_ID en config.py, que sí está duplicado a propósito porque bot/ y pagos/
no comparten imports — duplicar el *precio* es mucho más riesgoso tratándose de plata real).

A diferencia de bot/bot.py::cargar_config, que tolera que el archivo falte y degrada a un menú
vacío, acá no hay ningún fallback: si no se puede leer el config o el canal_id pedido no existe,
el llamador debe rechazar la request (ver pagos/pagos_tarjeta.py) en vez de asumir un monto.
"""
from __future__ import annotations

import json
from pathlib import Path

RUTA_CONFIG = Path(__file__).resolve().parent.parent / "web" / "data" / "config.json"


def canales_pagos() -> dict[str, dict]:
    """Canales pagos definidos en config.json, indexados por canal_id. Lanza OSError/
    JSONDecodeError/KeyError si el archivo falta o está mal formado — a propósito, ver docstring
    del módulo."""
    with open(RUTA_CONFIG, encoding="utf-8") as archivo:
        datos = json.load(archivo)
    return {c["canal_id"]: c for c in datos.get("canales_pagos") or [] if c.get("canal_id")}
