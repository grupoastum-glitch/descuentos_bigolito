"""Lock entre corridas del scraper, vía advisory lock de Postgres — evita que dos corridas (ej.
un "Run Now" manual pisando al cron de la hora, o dos manuales seguidos) scrapeen y publiquen al
mismo tiempo.

pg_try_advisory_lock es no bloqueante (si ya hay lock, se aborta esta corrida, no se espera) y es
por sesión: se libera solo en cuanto la conexión que lo sostiene se cierra, sin importar si fue un
liberar_lock() prolijo o el contenedor muriendo de golpe — a diferencia del lock viejo (un archivo
commiteado a git), no hace falta ningún timeout fijo para recuperarse de una corrida colgada.
"""
from __future__ import annotations

import logging

import asyncpg

log = logging.getLogger("scraper.run_lock")

_LOCK_KEY = 727271  # arbitrario, namespaced a esta app — un solo scraper, una sola clave


async def adquirir_lock(pool: asyncpg.Pool) -> asyncpg.pool.PoolConnectionProxy | None:
    """Devuelve la conexión que sostiene el lock (hay que mantenerla viva hasta liberar_lock),
    o None si ya hay otra corrida activa — en ese caso el caller debe abortar sin scrapear ni
    publicar nada."""
    con = await pool.acquire()
    obtenido = await con.fetchval("SELECT pg_try_advisory_lock($1)", _LOCK_KEY)
    if not obtenido:
        await pool.release(con)
        log.error("Ya hay una corrida activa — se aborta esta corrida.")
        return None
    return con


async def liberar_lock(con: asyncpg.pool.PoolConnectionProxy, pool: asyncpg.Pool) -> None:
    await con.fetchval("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    await pool.release(con)
