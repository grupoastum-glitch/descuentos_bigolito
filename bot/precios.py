"""Escalamiento de precios por tramos + precio fijo de por vida. Espejo intencional de
pagos/precios.py — son dos procesos separados (bot/ y pagos/) sobre la misma Postgres, mismo
criterio que la duplicación de CANAL_CHAT_ID (ver pagos/config.py): acá se duplica lógica/ladder,
no un número de plata suelto, que sigue centralizado en web/data/config.json."""
from __future__ import annotations

import asyncpg

import db


def calcular_precio_tramo(canal_cfg: dict, cantidad_usuarios_existentes: int) -> int:
    """Precio que le toca a una persona nueva dado cuánta gente ya fijó un precio antes que ella
    en este canal. Si canal_cfg no trae "tramos" (ej. el canal interno "test2"), se mantiene el
    comportamiento de siempre: precio fijo único en "monto". Los tramos están ordenados por
    "hasta" en config.json; se toma el primero que cubra la cantidad, y más allá del último se usa
    el techo "tramo_monto_maximo"."""
    tramos = canal_cfg.get("tramos")
    if not tramos:
        return canal_cfg["monto"]
    for tramo in tramos:
        if cantidad_usuarios_existentes <= tramo["hasta"]:
            return tramo["monto"]
    return canal_cfg["tramo_monto_maximo"]


async def resolver_precio(
    pool: asyncpg.Pool, telegram_user_id: int, canal_id: str, canal_cfg: dict,
) -> int:
    """El precio a mostrar/cobrar a esta persona para este canal: si ya pagó antes acá, el mismo
    de siempre (precio_congelado, fijo de por vida); si es su primera vez, el tramo que le toca
    según el total histórico de gente que ya fijó precio. No persiste nada acá — solo calcula; el
    guardado real ocurre en pagos/logica.py cuando el pago se confirma."""
    ya_fijado = await db.obtener_precio_congelado(pool, telegram_user_id, canal_id)
    if ya_fijado is not None:
        return ya_fijado
    cantidad_existente = await db.contar_precios_congelados(pool, canal_id)
    return calcular_precio_tramo(canal_cfg, cantidad_existente)
