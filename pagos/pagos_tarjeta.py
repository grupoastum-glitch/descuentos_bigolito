"""Endpoint público (llamado directo desde el navegador, no server-to-server como el webhook) que
crea una suscripción con pago autorizado inmediato vía tarjeta tokenizada — la contraparte
backend de web/pagar-tarjeta.html (MercadoPago Bricks). Alternativa a bot/bot.py::_crear_preapproval
para pagadores sin cuenta de MercadoPago; ver el plan de Bricks para el diseño completo.

No hace ningún upsert/invitación acá: una vez creada la preapproval, MercadoPago dispara el mismo
webhook (topic subscription_preapproval) que ya procesa pagos/webhook.py para el flujo redirigido
— confirmado con soporte de MercadoPago que el webhook se dispara igual sin importar si el alta
fue por redirección o por API, así que esta ruta no duplica esa lógica.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from mercadopago.errors.exceptions import MercadoPagoError

import config
import config_canales
import db
import mercadopago_client
import precios

log = logging.getLogger("pagos.pagos_tarjeta")

router = APIRouter()


def _firma_valida(telegram_user_id: str, canal_id: str, email: str, exp: str, sig: str) -> bool:
    """True si (telegram_user_id, canal_id, email, exp) fueron firmados por bot/bot.py con
    LINK_PAGO_SECRET y todavía no vencieron. Sin esto, cualquiera podría cambiar esos parámetros
    en la URL y generar un cobro/acceso a nombre de otro usuario — el navegador no tiene ninguna
    sesión autenticada de Telegram."""
    try:
        exp_unix = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_unix < time.time():
        return False

    payload = f"{telegram_user_id}:{canal_id}:{email}:{exp_unix}"
    firma_esperada = hmac.new(
        config.LINK_PAGO_SECRET.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(firma_esperada, sig or "")


@router.post("/pagos/tarjeta")
async def crear_pago_tarjeta(request: Request) -> JSONResponse:
    body = await request.json()
    telegram_user_id = body.get("telegram_user_id")
    canal_id = body.get("canal_id")
    email = body.get("email")
    exp = body.get("exp")
    sig = body.get("sig")
    card_token_id = body.get("card_token_id")

    if not all([telegram_user_id, canal_id, email, exp, sig, card_token_id]):
        return JSONResponse({"error": "faltan_campos"}, status_code=400)

    if not _firma_valida(telegram_user_id, canal_id, email, exp, sig):
        return JSONResponse({"error": "link_invalido_o_vencido"}, status_code=401)

    try:
        telegram_user_id_int = int(telegram_user_id)
    except (TypeError, ValueError):
        return JSONResponse({"error": "faltan_campos"}, status_code=400)

    # sin este chequeo, alguien podría pagar dos veces: el mensaje del bot muestra los dos
    # botones de pago juntos, y si ya pagó con uno (MercadoPago o tarjeta), este endpoint
    # cobraría una tarjeta nueva igual si vuelve a tocar "Pagar con tarjeta" en el mismo mensaje
    # — cancelar_preapprovals_anteriores (más abajo) cancela la suscripción vieja, pero no
    # devuelve lo que ya se cobró.
    pool = await db.conectar()
    if await db.esta_activo(pool, telegram_user_id_int, canal_id):
        return JSONResponse({"error": "ya_activo"}, status_code=200)

    try:
        canal_cfg = config_canales.canales_pagos().get(canal_id)
    except (OSError, ValueError):
        log.exception("No se pudo leer config_canales al procesar pago con tarjeta")
        return JSONResponse({"error": "error_interno"}, status_code=500)
    if not canal_cfg or not canal_cfg.get("visible", True):
        return JSONResponse({"error": "canal_invalido"}, status_code=400)

    # precio real de esta persona (tramo si es primera vez, o su precio_congelado si ya pagó
    # antes acá — ver pagos/precios.py) en vez del "monto" fijo de config.json, que con el
    # escalamiento de precios ya no es un número único.
    precio = await precios.resolver_precio(pool, telegram_user_id_int, canal_id, canal_cfg)
    canal_cfg = {**canal_cfg, "monto": precio}

    try:
        await run_in_threadpool(
            mercadopago_client.cancelar_preapprovals_anteriores, telegram_user_id, canal_id, email,
        )
    except Exception:
        # best-effort, mismo criterio que bot/bot.py::_cancelar_preapprovals_anteriores — no debe
        # impedir que el usuario cree su preapproval nueva.
        log.exception("Falló la limpieza de preapprovals anteriores para %s", telegram_user_id)

    try:
        preapproval = await run_in_threadpool(
            mercadopago_client.crear_preapproval_con_tarjeta,
            telegram_user_id, email, card_token_id, canal_cfg,
        )
    except MercadoPagoError as error:
        # rechazo síncrono de la API (ej. tarjeta/token inválido) — no filtrar el mensaje interno
        # de MercadoPago al frontend, solo el motivo si lo trae. A confirmar el shape exacto en
        # sandbox (ver plan de Bricks, plan de pruebas punto 1).
        log.warning("Preapproval con tarjeta rechazada para %s: %s", telegram_user_id, error)
        return JSONResponse({"error": "pago_rechazado", "detalle": str(error)}, status_code=200)
    except Exception:
        log.exception("Falló la creación de preapproval con tarjeta para %s", telegram_user_id)
        return JSONResponse({"error": "error_interno"}, status_code=500)

    if preapproval.get("status") == "rejected":
        return JSONResponse(
            {"error": "pago_rechazado", "detalle": preapproval.get("status_detail")},
            status_code=200,
        )

    return JSONResponse({"status": "ok"}, status_code=200)
