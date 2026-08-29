"""Lock entre corridas del scraper, vía advisory lock de Postgres — evita que dos corridas (ej.
un "Run Now" manual pisando al cron de la hora, o dos manuales seguidos) scrapeen y publiquen al
mismo tiempo.

pg_try_advisory_lock es no bloqueante (si ya hay lock, se aborta esta corrida, no se espera) y es
por sesión: se libera solo en cuanto la conexión que lo sostiene se cierra, sin importar si fue un
liberar_lock() prolijo o el contenedor muriendo de golpe — a diferencia del lock viejo (un archivo
commiteado a git), no hace falta ningún timeout fijo para recuperarse de una corrida colgada.

La clave está namespaced por config.SCRAPER_NOMBRE, no fija: dos instancias de scraper corriendo
en paralelo (ej. una para tiendas normales, otra para geek) necesitan cada una su propio lock —
si compartieran la misma clave, la segunda instancia se abortaría pensando que es una corrida
duplicada de la primera, aunque sean dos scrapers legítimos distintos.
"""
from __future__ import annotations

import logging
import zlib

import asyncpg

import config

log = logging.getLogger("scraper.run_lock")

# Sin SCRAPER_NOMBRE (instancia única de hoy): la clave arbitraria de siempre, sin cambio de
# comportamiento. Con SCRAPER_NOMBRE seteado: hash estable del nombre, así cada instancia nueva
# obtiene su propia clave con solo setear la env var, sin mantener un mapa a mano en el código.
_LOCK_KEY = (
    727271 if not config.SCRAPER_NOMBRE
    else zlib.crc32(config.SCRAPER_NOMBRE.encode()) & 0x7FFFFFFF
)

# Clave distinta a _LOCK_KEY: publicar.py es un proceso separado del scraper (otro ciclo de
# vida, no coordinado por SCRAPER_NOMBRE) — si compartiera la clave, competiría por el mismo
# lock que las corridas del scraper en vez de coordinar contra otras instancias de sí mismo.
_LOCK_KEY_PUBLICAR = 727280


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


async def adquirir_lock_publicador(pool: asyncpg.Pool) -> asyncpg.pool.PoolConnectionProxy:
    """Bloqueante, a diferencia de adquirir_lock: publicar.py es un proceso 24/7 de instancia
    única, no una corrida puntual que deba abortar si ya hay otra activa. Si el lock está
    tomado (ej. un deploy de Railway colgado dejó al contenedor viejo corriendo mientras el
    nuevo arranca), simplemente espera a que se libere — el contenedor viejo lo libera solo al
    morir, sin importar si fue un apagado prolijo o un kill de golpe (ver docstring arriba)."""
    con = await pool.acquire()
    await con.fetchval("SELECT pg_advisory_lock($1)", _LOCK_KEY_PUBLICAR)
    return con


async def liberar_lock(con: asyncpg.pool.PoolConnectionProxy, pool: asyncpg.Pool) -> None:
    await con.fetchval("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    await pool.release(con)


async def liberar_lock_publicador(con: asyncpg.pool.PoolConnectionProxy, pool: asyncpg.Pool) -> None:
    await con.fetchval("SELECT pg_advisory_unlock($1)", _LOCK_KEY_PUBLICAR)
    await pool.release(con)
