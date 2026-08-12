"""Lógica compartida entre pagos/webhook.py y pagos/reconciliacion.py: qué hacer con el estado
real de una preapproval, una vez consultado a la API de MercadoPago (nunca desde el body de un
webhook sin verificar — ver mercadopago_client.obtener_preapproval)."""
from __future__ import annotations

import logging

import asyncpg

import config
import db
import telegram_client

log = logging.getLogger("pagos.logica")

_ESTADO_POR_STATUS_MP = {
    "authorized": "activa",
    "paused": "pausada",
    "cancelled": "cancelada",
}


def _parse_external_reference(external_reference: str | None) -> tuple[int, str] | None:
    """external_reference = "{telegram_user_id}:{canal_id}" (armado en bot/bot.py al crear la
    preapproval). None si no tiene el formato esperado."""
    if not external_reference or ":" not in external_reference:
        return None
    id_parte, canal_id = external_reference.split(":", 1)
    if not id_parte.isdigit():
        return None
    return int(id_parte), canal_id


async def aplicar_estado_preapproval(pool: asyncpg.Pool, preapproval: dict) -> None:
    """Traduce el estado real de una preapproval a un cambio de acceso: upsert en Postgres +
    invitar/expulsar según corresponda. No hace nada si el status de MP no es uno de los que nos
    importan (ej. "pending", una suscripción todavía no autorizada)."""
    estado = _ESTADO_POR_STATUS_MP.get(preapproval.get("status"))
    if estado is None:
        log.info(
            "Preapproval %s con status '%s' — sin acción.",
            preapproval.get("id"), preapproval.get("status"),
        )
        return

    referencia = _parse_external_reference(preapproval.get("external_reference"))
    if referencia is None:
        log.warning(
            "Preapproval %s con external_reference inesperado: %r — se ignora.",
            preapproval.get("id"), preapproval.get("external_reference"),
        )
        return
    telegram_user_id, canal_id = referencia

    if canal_id not in config.CANAL_CHAT_ID:
        log.warning(
            "Preapproval %s referencia un canal_id desconocido: %r — se ignora.",
            preapproval.get("id"), canal_id,
        )
        return

    es_nueva_activacion = await db.upsert_suscripcion(
        pool, telegram_user_id, canal_id, preapproval["id"], estado,
    )

    if estado == "activa":
        if es_nueva_activacion:
            await telegram_client.invitar(telegram_user_id, canal_id)
    else:
        await telegram_client.expulsar(telegram_user_id, canal_id)
