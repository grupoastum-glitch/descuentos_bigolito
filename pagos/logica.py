"""Lógica compartida entre pagos/webhook.py y pagos/reconciliacion.py: qué hacer con el estado
real de una preapproval, una vez consultado a la API de MercadoPago (nunca desde el body de un
webhook sin verificar — ver mercadopago_client.obtener_preapproval)."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import asyncpg

import config
import config_canales
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


def _parse_external_reference(external_reference: str | None) -> tuple[int, str, str | None] | None:
    """external_reference = "{telegram_user_id}:{canal_id}:{email}" (armado en
    bot/bot.py::_crear_preapproval). El email viaja acá — no en el campo payer_email de la
    preapproval, que la API real de MercadoPago nunca devuelve poblado (ver
    aplicar_estado_preapproval) — porque es la única forma de que este proceso, separado del bot,
    recupere el email que el usuario escribió en el chat. maxsplit=2 conserva cualquier ":" que el
    email tuviera (raro, pero válido) en vez de cortarlo. None si no tiene el formato esperado, o
    si es una preapproval vieja sin el tercer campo (external_reference solo tenía 2 partes)."""
    if not external_reference or ":" not in external_reference:
        return None
    partes = external_reference.split(":", 2)
    if len(partes) < 2 or not partes[0].isdigit():
        return None
    email = partes[2] if len(partes) == 3 else None
    return int(partes[0]), partes[1], email


async def _registrar_y_avisar_venta(
    pool: asyncpg.Pool, telegram_user_id: int, canal_id: str, tipo: str, email: str | None,
) -> None:
    """Registra un pago confirmado (alta o renovación) en pagos_historial y avisa al canal admin
    de control interno. Se llama SIEMPRE después de que el usuario ya tiene su acceso (ver
    llamadores) y envuelta en try/except acá mismo a propósito: pagos/webhook.py deja que
    cualquier excepción no atrapada llegue a un 500 para que MercadoPago reintente el webhook
    entero (comportamiento correcto ante errores transitorios reales) — pero un fallo en esto
    (guardar el historial o avisar al admin) no debe disparar ese reintento, porque el acceso ya
    quedó dado y el reintento no lo repetiría igual (es_nueva_activacion/ultimo_invoice_id ya
    quedaron marcados), solo generaría ruido. En el peor caso se pierde un aviso puntual, nunca el
    acceso del que pagó."""
    try:
        monto = config_canales.canales_pagos().get(canal_id, {}).get("monto")
        username = await db.obtener_username(pool, telegram_user_id)
        await db.registrar_pago_historial(pool, telegram_user_id, canal_id, tipo, monto, email)
        await telegram_client.avisar_pago(telegram_user_id, username, canal_id, tipo, monto, email)
    except Exception:
        log.exception(
            "Falló registrar/avisar la venta (%s) de %s en %s — el acceso ya fue otorgado, no se reintenta.",
            tipo, telegram_user_id, canal_id,
        )


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
    telegram_user_id, canal_id, email = referencia

    if canal_id not in config.CANAL_CHAT_ID:
        log.warning(
            "Preapproval %s referencia un canal_id desconocido: %r — se ignora.",
            preapproval.get("id"), canal_id,
        )
        return

    # preapproval.get("payer_email"): confirmado contra la documentación oficial de MercadoPago
    # que esto siempre viene None — el recurso de preapproval solo trae "payer_id" (un ID interno
    # numérico), nunca el email. El email real viaja en el external_reference (ver
    # _parse_external_reference); se sigue pasando payer_email como fallback por si algún día la
    # API empieza a devolverlo, protegido por el COALESCE de db.upsert_suscripcion.
    es_nueva_activacion = await db.upsert_suscripcion(
        pool, telegram_user_id, canal_id, preapproval["id"], estado,
        email or preapproval.get("payer_email"),
    )

    if estado == "activa":
        if es_nueva_activacion:
            # arranca acceso_hasta en "ahora", sin sumar período: el período real lo otorga
            # aplicar_pago_recurrente, incluido el primer cobro — MercadoPago dispara también su
            # propio webhook de invoice para esa primera carga (confirmado en la prueba real), así
            # que sumarlo acá también contaría el mismo pago dos veces.
            await db.extender_acceso(pool, telegram_user_id, canal_id, timedelta(0))
            await telegram_client.invitar(telegram_user_id, canal_id)
            await _registrar_y_avisar_venta(pool, telegram_user_id, canal_id, "alta", email)
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

    if fila["estado"] != "activa":
        # un cobro exitoso mientras la fila no estaba 'activa' es una recuperación: o bien un
        # reintento de MercadoPago que finalmente funcionó (la fila quedó 'vencida' por
        # reconciliacion.py sin que la preapproval cambiara de status nunca), o un invoice que
        # llegó justo después de una expulsión por una carrera angosta. En ambos casos hay que
        # reinvitar — no llega un cambio de estado de la preapproval que dispare esto solo.
        await db.marcar_estado(pool, fila["telegram_user_id"], fila["canal_id"], "activa")
        await telegram_client.invitar(fila["telegram_user_id"], fila["canal_id"])

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
    log.info(
        "Acceso extendido para %s en canal %s (invoice %s, preapproval %s).",
        fila["telegram_user_id"], fila["canal_id"], invoice_id, preapproval_id,
    )
    await _registrar_y_avisar_venta(
        pool, fila["telegram_user_id"], fila["canal_id"], "renovacion", fila.get("payer_email"),
    )
