"""Cron diario (Railway) — red de seguridad para cuando un webhook de MercadoPago se perdió (ej.
el servicio de pagos estaba caído en ese momento). Recorre las suscripciones marcadas como
activas localmente y vuelve a confirmar su estado real contra la API de MercadoPago, corrigiendo
cualquier diferencia. No es el mecanismo principal — eso es pagos/webhook.py, que reacciona en
tiempo real; esto solo corrige drift.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import db  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import logica  # noqa: E402
import mercadopago_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pagos.reconciliacion")


async def _correr() -> None:
    pool = await db.conectar()
    try:
        activas = await db.listar_activas(pool)
        log.info("%s suscripciones activas a reconfirmar.", len(activas))

        for fila in activas:
            preapproval_id = fila["mercadopago_preapproval_id"]
            try:
                preapproval = await asyncio.to_thread(
                    mercadopago_client.obtener_preapproval, preapproval_id,
                )
                await logica.aplicar_estado_preapproval(pool, preapproval)
            except Exception:
                # una suscripción con problemas (ej. la preapproval ya no existe en MP) no debe
                # cortar la reconciliación del resto — se reintenta mañana.
                log.exception(
                    "Falló la reconciliación de la preapproval %s (usuario %s, canal %s)",
                    preapproval_id, fila["telegram_user_id"], fila["canal_id"],
                )

        log.info("Reconciliación terminada.")
    finally:
        await db.cerrar()


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
