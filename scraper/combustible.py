"""Servicio dedicado del canal "Cupones Combustible" (Copec/Shell) — separado a propósito del
scraper general (main.py) y su publicador (publicar.py):

1. Scrapea Copec y Shell (~28 cupones totales) y actualiza el snapshot en Postgres (tabla
   `cupones_combustible`, ver db.py/cupones_writer.py).
2. Arma el digest diario (los cupones vigentes HOY, ver cupones_writer.construir_grupos_digest) y
   lo manda directo a Telegram — sin pasar por `cola_publicacion`: esa cola existe para desacoplar
   el scrapeo lento de miles de ofertas del envío con throttle; acá no hay volumen que la
   justifique (1 mensaje al día).

Corre como un cron de Railway independiente (one-shot, sin loop 24/7 ni advisory lock propio — a
diferencia del scraper general, una superposición ocasional acá no duplica horas de trabajo).
Respeta la pausa MANUAL del admin (`/pausar`), pero NO la pausa automática nocturna
(`config.en_pausa_madrugada()`) — es la única fuente del proyecto exenta a propósito: el digest
tiene que estar disponible desde la madrugada (quien sale a trabajar a las 5am lo necesita antes),
no recién cuando el resto del scraper despierta a las 07:00.

Idempotente sin estados intermedios: si el envío a Telegram falla, no se marca
`cupones_digest_enviado` y la próxima corrida del cron reintenta desde cero (re-scrapea, reconstruye
el digest, reintenta mandarlo) — no hace falta persistir un estado "a medias".

Override manual: `config.FORZAR_DIGEST_COMBUSTIBLE=1` salta el chequeo "¿ya se mandó hoy?" — para
poder apretar "Run Now" en Railway y ver el resultado al toque en vez de esperar al día siguiente.
Acordarse de apagarlo después: mientras esté en 1, TODAS las corridas reenvían, no solo la manual.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from scrapling.fetchers import FetcherSession

load_dotenv(Path(__file__).resolve().parent / ".env")

import config  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import cupones_writer  # noqa: E402
import db  # noqa: E402
import dias_semana  # noqa: E402
import telegram_publisher  # noqa: E402
from fuentes.copec.listado import obtener_cupones_copec  # noqa: E402
from fuentes.shell.listado import obtener_cupones_shell  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scraper.combustible")

_FUENTES = (
    ("shell", "Shell", obtener_cupones_shell),
    ("copec", "Copec", obtener_cupones_copec),
)


async def _scrapear_y_actualizar_snapshot(pool) -> None:
    async with FetcherSession(impersonate=config.USER_AGENT_IMPERSONATE, timeout=config.HTTP_TIMEOUT_SEGUNDOS) as sesion:
        for tienda_id, comercio, obtener in _FUENTES:
            try:
                detectados, ok = await obtener(sesion)
            except Exception:
                log.exception("%s: excepción no anticipada, se omite esta fuente", comercio)
                continue
            if ok:
                await cupones_writer.procesar_cupones(pool, detectados, tienda_id, comercio)
            else:
                log.error("%s: no se pudo leer ninguna promo, se deja el snapshot como estaba", comercio)


async def _correr() -> None:
    if not config.DATABASE_URL:
        raise SystemExit("Falta DATABASE_URL en el entorno")
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en el entorno")
    if not config.CANAL_TELEGRAM_USERNAME.get("ofertas_combustible"):
        raise SystemExit('Falta CANAL_TELEGRAM_USERNAME["ofertas_combustible"] en config.py')

    pool = await db.conectar()
    try:
        if await db.pausa_manual_activa(pool):
            log.info("Pausa manual activa, se omite esta corrida.")
            return

        await _scrapear_y_actualizar_snapshot(pool)

        hoy = datetime.now(config.TZ_CHILE).date()
        if config.FORZAR_DIGEST_COMBUSTIBLE:
            log.warning(
                "FORZAR_DIGEST_COMBUSTIBLE activo: se ignora el chequeo de \"ya se mandó hoy\" — "
                "acordarse de apagarlo después de probar, si no el cron lo va a reenviar cada vez."
            )
        elif await db.digest_enviado_hoy(pool, hoy):
            log.info("El digest de hoy (%s) ya se mandó, nada más que hacer.", hoy)
            return

        activos = await db.cargar_cupones_activos(pool)
        grupos = cupones_writer.construir_grupos_digest(activos, dias_semana.nombre_dia_chile())
        texto = telegram_publisher.formatear_digest_cupones(grupos)
        if texto is None:
            log.info("No hay cupones vigentes hoy, no se manda digest.")
            return

        oferta_digest = {
            "id": f"digest-combustible-{hoy.isoformat()}",
            "tipo": "digest_cupones",
            "canal": "ofertas_combustible",
            "titulo": f"Digest cupones combustible {hoy.isoformat()}",
            "texto": texto,
        }
        confirmados = await telegram_publisher.publicar_ofertas_nuevas([oferta_digest])
        if oferta_digest["id"] in confirmados:
            await db.marcar_digest_enviado(pool, hoy)
            log.info("Digest de cupones combustible enviado para %s.", hoy)
        else:
            log.error(
                "No se pudo confirmar el envío del digest de %s — se reintenta en la próxima corrida.",
                hoy,
            )
    finally:
        await db.cerrar()


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
