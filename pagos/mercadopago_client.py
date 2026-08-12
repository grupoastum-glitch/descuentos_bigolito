"""Cliente delgado sobre el SDK oficial de MercadoPago (paquete `mercadopago`, ver
requirements.txt): verificación de firma de webhooks + consulta de preapprovals.

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
