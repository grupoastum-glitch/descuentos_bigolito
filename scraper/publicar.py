"""Publicador de ofertas a Telegram — proceso separado del scraper, corre 24/7 (no cron, ver
scraper/Dockerfile.publicar), igual que bot/bot.py.

scraper/main.py ya no publica directo: al final de cada corrida solo encola las ofertas nuevas
en Postgres (tabla `cola_publicacion`, ver db.encolar_publicaciones). Este proceso es el único
que lee esa cola y la manda a Telegram — así, aunque el volumen de una corrida sea grande y
tarde horas en drenarse (limitado por config.TELEGRAM_DELAY_SEGUNDOS por canal, no por
nosotros), eso nunca bloquea el advisory lock que necesita la próxima corrida horaria del
scraper para arrancar (ver run_lock.py).

Está pensado como instancia única, pero a diferencia del scraper eso no está garantizado por
naturaleza (no hay cron de por medio) — un deploy de Railway que se cuelga puede dejar al
contenedor viejo corriendo mientras el nuevo arranca, y sin coordinación ambos drenarían la
misma cola a la vez (mismo _cola_id publicado dos veces, el bug reportado 2026-08-28). Por eso
usa run_lock.adquirir_lock_publicador (bloqueante, clave propia _LOCK_KEY_PUBLICAR) en vez de
arrancar sin lock.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import config  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import db  # noqa: E402
import ofertas_writer  # noqa: E402
import run_lock  # noqa: E402
import telegram_publisher  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scraper.publicar")

POLL_SEGUNDOS = 15  # espera entre chequeos de la cola cuando está vacía
LIMPIEZA_INTERVALO_HORAS = 1  # cada cuánto se corre db.limpiar_cola_publicada — antes 5h/6h/24h.
# Bajado a 1h junto con la retención (ver LIMPIEZA_RETENCION_DIAS): nada en el código lee filas ya
# publicadas (el único SELECT de cola_publicacion filtra publicado_en IS NULL) — el evento real
# vive en historial_precios — así que no hay ninguna razón funcional para retenerlas más que un
# rato corto para poder mirarlas a mano en la consola de Railway si hace falta debuggear algo
# (sesión 2026-08-25). El intervalo tiene que ser <= la retención, si no las filas se siguen
# acumulando entre limpiezas sin que la retención más chica se note.
LIMPIEZA_RETENCION_DIAS = 2 / 24  # 2 horas — filas ya publicadas más viejas que esto se borran


async def _drenar_una_vez(pool) -> bool:
    """Publica todo lo pendiente de la cola. Devuelve True si había algo para publicar (para que
    el loop no espere POLL_SEGUNDOS de más después de un lote grande)."""
    pendientes = await db.cola_pendiente(pool)
    if not pendientes:
        return False

    async def _on_publicada(oferta: dict) -> None:
        # mismo callback que usaba main.py antes de este cambio — persiste el evento de
        # historial al toque, no al final del lote.
        await ofertas_writer.registrar_evento_publicado(pool, oferta)
        await db.marcar_publicada(pool, oferta["_cola_id"])

    await telegram_publisher.publicar_ofertas_nuevas(pendientes, on_publicada=_on_publicada)
    return True


async def _correr() -> None:
    if not config.DATABASE_URL:
        raise SystemExit("Falta DATABASE_URL en el entorno")
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en el entorno")

    pool = await db.conectar()
    con_lock = await run_lock.adquirir_lock_publicador(pool)
    log.info("Publicador arrancado, drenando cola_publicacion cada %ss cuando está vacía.", POLL_SEGUNDOS)

    ultima_limpieza = datetime.now(timezone.utc)
    try:
        while True:
            pausado = config.en_pausa_madrugada() or await db.pausa_manual_activa(pool)
            if pausado:
                # no se corta un envío ya en curso (este chequeo solo corre entre vueltas del
                # loop, nunca a mitad de _drenar_una_vez) — pero no tiene sentido guardar lo
                # pendiente para soltarlo horas después: se descarta, así lo primero que se
                # publica al reanudar es recién scrapeado, no un backlog viejo.
                descartadas = await db.descartar_cola_pendiente(pool)
                if descartadas:
                    log.info("Pausa activa: se descartaron %s ofertas que estaban en cola.", descartadas)
                hubo_trabajo = False
            else:
                try:
                    hubo_trabajo = await _drenar_una_vez(pool)
                except Exception:
                    # un error no anticipado (ej. problema transitorio de red/DB) no debe tumbar el
                    # proceso — es 24/7, tiene que seguir intentando en la próxima vuelta.
                    log.exception("Falló un ciclo de publicación, se reintenta en %ss", POLL_SEGUNDOS)
                    hubo_trabajo = False

            ahora = datetime.now(timezone.utc)
            if (ahora - ultima_limpieza).total_seconds() >= LIMPIEZA_INTERVALO_HORAS * 3600:
                try:
                    borradas = await db.limpiar_cola_publicada(pool, dias=LIMPIEZA_RETENCION_DIAS)
                    log.info("Limpieza de cola_publicacion: %s filas borradas (>%sd publicadas).", borradas, LIMPIEZA_RETENCION_DIAS)
                except Exception:
                    log.exception("Falló la limpieza periódica de cola_publicacion")
                ultima_limpieza = ahora

            if not hubo_trabajo:
                await asyncio.sleep(POLL_SEGUNDOS)
    finally:
        await run_lock.liberar_lock_publicador(con_lock, pool)
        await db.cerrar()


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
