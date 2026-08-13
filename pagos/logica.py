"""Lógica compartida entre pagos/webhook.py y pagos/reconciliacion.py: qué hacer con el estado
real de una preapproval, una vez consultado a la API de MercadoPago (nunca desde el body de un
webhook sin verificar — ver mercadopago_client.obtener_preapproval)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import asyncpg

import config
import db
import mercadopago_client
import telegram_client

log = logging.getLogger("pagos.logica")

_ESTADO_POR_STATUS_MP = {
    "authorized": "activa",
    "paused": "pausada",
    "cancelled": "cancelada",
}

# Confirmado contra un pago real de prueba (ver PLAN_periodo_gracia_cancelacion.md): el "status"
# del invoice en sí (ej. "processed") describe el intento de cobro, no si salió bien — el
# resultado real está en el objeto "payment" anidado, con status "approved" (status_detail
# "accredited" en el caso exitoso).
_STATUS_PAGO_APROBADO = "approved"


def _periodo_de(auto_recurring: dict) -> timedelta:
    """Traduce frequency/frequency_type de MercadoPago a un timedelta. "months" se aproxima a 30
    días por mes — MercadoPago no admite otro frequency_type que "days"/"months", y una duración
    calendario exacta es más precisión de la que este negocio necesita."""
    frecuencia = auto_recurring["frequency"]
    if auto_recurring["frequency_type"] == "months":
        return timedelta(days=30 * frecuencia)
    return timedelta(days=frecuencia)


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
            # arranca acceso_hasta en "ahora", sin sumar período: el período real lo otorga
            # aplicar_pago_recurrente, incluido el primer cobro — MercadoPago dispara también su
            # propio webhook de invoice para esa primera carga (confirmado en la prueba real), así
            # que sumarlo acá también contaría el mismo pago dos veces.
            await db.extender_acceso(pool, telegram_user_id, canal_id, timedelta(0))
            await telegram_client.invitar(telegram_user_id, canal_id)
    # Ya no se expulsa acá al pausar/cancelar — el usuario conserva el acceso hasta que vence
    # acceso_hasta (el período que ya pagó). La expulsión real la hace pagos/reconciliacion.py
    # cuando ese plazo pasa. Ver PLAN_periodo_gracia_cancelacion.md.


async def aplicar_pago_recurrente(pool: asyncpg.Pool, invoice: dict) -> None:
    """Traduce un cobro recurrente confirmado (invoice/authorized_payment) en una extensión del
    acceso pagado. Se dispara desde el webhook de topic 'subscription_authorized_payment', que
    antes se ignoraba por completo."""
    # normalizado a str: el SDK puede devolver "id" como int, y la columna ultimo_invoice_id es
    # TEXT — sin esto, la comparación de reenvío de abajo nunca matchea.
    invoice_id = str(invoice["id"]) if invoice.get("id") is not None else None

    if invoice.get("payment", {}).get("status") != _STATUS_PAGO_APROBADO:
        log.info(
            "Invoice %s sin pago aprobado todavía (payment.status=%r) — sin acción.",
            invoice_id, invoice.get("payment", {}).get("status"),
        )
        return

    preapproval_id = invoice.get("preapproval_id")
    if not preapproval_id:
        log.warning("Invoice %s sin preapproval_id — se ignora.", invoice_id)
        return

    fila = await db.buscar_por_preapproval_id(pool, preapproval_id)
    if fila is None:
        log.warning(
            "Invoice %s referencia una preapproval desconocida: %s — se ignora.",
            invoice_id, preapproval_id,
        )
        return

    if invoice_id is not None and fila["ultimo_invoice_id"] == invoice_id:
        log.info("Invoice %s ya procesado — se ignora (reenvío de MercadoPago).", invoice_id)
        return

    # el período (frequency/frequency_type) vive en la preapproval, no en el invoice — se
    # consulta acá en vez de guardarlo aparte, ya recibir un cobro confirma que sigue vigente.
    preapproval = await asyncio.to_thread(mercadopago_client.obtener_preapproval, preapproval_id)
    await db.extender_acceso(
        pool,
        fila["telegram_user_id"],
        fila["canal_id"],
        _periodo_de(preapproval["auto_recurring"]),
        invoice_id=invoice_id,
    )
