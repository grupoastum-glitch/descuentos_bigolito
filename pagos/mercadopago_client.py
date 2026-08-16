"""Cliente delgado sobre el SDK oficial de MercadoPago (paquete `mercadopago`, ver
requirements.txt): verificación de firma de webhooks, consulta de preapprovals, y creación de
preapprovals con tarjeta tokenizada (pago con MercadoPago Bricks, ver pagos/pagos_tarjeta.py).

La verificación de firma usa el validador que trae el propio SDK
(mercadopago.webhook.WebhookSignatureValidator) en vez de reimplementar el HMAC a mano — algoritmo
confirmado contra la fuente oficial: https://github.com/mercadopago/sdk-python/blob/master/mercadopago/webhook/validator.py
"""
from __future__ import annotations

import logging

import mercadopago
from mercadopago.webhook import InvalidWebhookSignatureError, WebhookSignatureValidator

import config

log = logging.getLogger("pagos.mercadopago_client")

_sdk = mercadopago.SDK(config.MERCADOPAGO_ACCESS_TOKEN)


def verificar_firma(x_signature: str | None, x_request_id: str | None, data_id: str | None) -> bool:
    """True si la notificación viene de verdad de MercadoPago. Nunca lanza — cualquier fallo de
    validación (header ausente/malformado, firma que no matchea) se trata como firma inválida."""
    try:
        WebhookSignatureValidator.validate(
            x_signature, x_request_id, data_id, config.MERCADOPAGO_WEBHOOK_SECRET,
        )
        return True
    except InvalidWebhookSignatureError as error:
        log.warning("Firma de webhook inválida (%s)", error.reason.value)
        return False


def obtener_preapproval(preapproval_id: str) -> dict:
    """GET /preapproval/{id} — la fuente de verdad del estado real de la suscripción. El webhook
    nunca actúa en base al body de la notificación en sí, solo en base a esta consulta.

    El SDK envuelve toda respuesta en {"status": <http_code>, "response": <body>} — acá se
    desenvuelve para devolver directo el objeto preapproval (id, status, external_reference...),
    que es lo que espera pagos/logica.py."""
    resultado = _sdk.preapproval().get(preapproval_id)
    resultado.raise_for_status()
    return resultado["response"]


def obtener_invoice(invoice_id: str) -> dict:
    """GET /authorized_payments/{id} — un invoice representa un cobro individual de un ciclo de
    la suscripción (lo que dispara el webhook de topic 'subscription_authorized_payment'). Fuente:
    mercadopago.resources.invoice.Invoice en el SDK oficial (sdk.invoice().get(...))."""
    resultado = _sdk.invoice().get(invoice_id)
    resultado.raise_for_status()
    return resultado["response"]


def crear_preapproval_con_tarjeta(
    telegram_user_id: int, email: str, card_token_id: str, canal_cfg: dict,
) -> dict:
    """POST /preapproval con pago autorizado inmediato vía tarjeta tokenizada (MercadoPago
    Bricks) — alternativa a bot/bot.py::_crear_preapproval para pagadores sin cuenta de
    MercadoPago, llamada desde pagos/pagos_tarjeta.py. Mismo external_reference/auto_recurring/
    payer_email/reason que el flujo redirigido (para que pagos/logica.py::_parse_external_reference
    lo entienda sin cambios), cambiando back_url+status:pending por card_token_id+status:authorized.

    Confirmado contra la documentación oficial y contra soporte de MercadoPago (ver el plan de
    Bricks): no hace falta payment_method_id, y el payer_email solo necesita coincidir con el que
    se le pasó al Card Payment Brick al inicializarlo (initialization.payer.email en el frontend),
    no con una cuenta MercadoPago real — confirmar en sandbox antes de habilitar en producción.

    status: "authorized" en la respuesta NO significa "cuota ya cobrada" (el cobro real de la
    primera cuota es asíncrono, ~1h, y puede fallar) — pagos/logica.py::aplicar_estado_preapproval
    ya maneja esto sin cambios (invita al canal pero no extiende acceso_hasta hasta que
    aplicar_pago_recurrente confirme el cobro efectivo)."""
    resultado = _sdk.preapproval().create({
        "reason": f"Suscripción {canal_cfg.get('nombre_corto', canal_cfg['nombre'])} — Descuentos Bigolito",
        "external_reference": f"{telegram_user_id}:{canal_cfg['canal_id']}:{email}",
        "payer_email": email,
        "auto_recurring": {
            "frequency": canal_cfg["frecuencia"],
            "frequency_type": canal_cfg["frecuencia_tipo"],
            "transaction_amount": canal_cfg["monto"],
            "currency_id": "CLP",
        },
        "card_token_id": card_token_id,
        "status": "authorized",
    })
    resultado.raise_for_status()
    return resultado["response"]


def cancelar_preapprovals_anteriores(telegram_user_id: int, canal_id: str, email: str) -> None:
    """Cancela cualquier preapproval anterior de este usuario en este canal que MercadoPago
    todavía no dejó de cobrar — mismo criterio que bot/bot.py::_cancelar_preapprovals_anteriores
    (portado acá para que lo puedan llamar tanto el bot como pagos/pagos_tarjeta.py, evitando
    preapprovals huérfanas si alguien reintenta el pago con tarjeta tras un rechazo):
    - 'pending': un intento anterior sin completar.
    - 'authorized': una suscripción vieja que dejó de renovarse bien pero que MercadoPago nunca
      canceló del todo — si no se cierra explícitamente, sigue reintentando cobrarla en paralelo
      con la nueva.

    Best-effort: un fallo acá no debe impedir que el usuario cree su preapproval nueva (el
    llamador debe atrapar cualquier excepción y seguir adelante)."""
    prefijo_esperado = f"{telegram_user_id}:{canal_id}"
    resultado = _sdk.preapproval().search({"payer_email": email})
    resultado.raise_for_status()

    for item in resultado["response"].get("results", []):
        if item.get("status") not in ("pending", "authorized"):
            continue
        referencia = item.get("external_reference") or ""
        if referencia != prefijo_esperado and not referencia.startswith(prefijo_esperado + ":"):
            continue
        try:
            _sdk.preapproval().update(item["id"], {"status": "cancelled"})
        except Exception:
            log.exception("Falló la cancelación de la preapproval anterior %s", item["id"])
