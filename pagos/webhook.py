"""Servicio siempre activo (Railway) que recibe los webhooks de MercadoPago y actualiza el
acceso al canal pago en consecuencia. Ver PLAN_canal_vip_mercadopago.md para el diseño completo.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

load_dotenv(Path(__file__).resolve().parent / ".env")

import config  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import db  # noqa: E402
import logica  # noqa: E402
import mercadopago_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pagos.webhook")

# topics de MercadoPago que representan un cambio de estado de una suscripción — se aceptan dos
# nombres a propósito ("preapproval" y "subscription_preapproval") porque la documentación no es
# consistente sobre cuál usa cada integración; ambos se resuelven igual acá.
_TOPICS_PREAPPROVAL = {"preapproval", "subscription_preapproval"}

# topic de cada cobro recurrente individual (un ciclo de facturación ya cargado/intentado) — se
# usa para extender el acceso pagado, ver pagos/logica.py::aplicar_pago_recurrente.
_TOPICS_PAGO = {"subscription_authorized_payment"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await db.conectar()
    yield
    await db.cerrar()


app = FastAPI(lifespan=_lifespan)


@app.post("/webhook/mercadopago")
async def recibir_webhook(request: Request) -> JSONResponse:
    # MercadoPago manda el id/tipo del recurso tanto en la query string como en el body — la
    # firma se valida contra el de la query string (ver mercadopago_client.verificar_firma).
    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    topic = request.query_params.get("type") or request.query_params.get("topic")

    firma_valida = mercadopago_client.verificar_firma(
        request.headers.get("x-signature"),
        request.headers.get("x-request-id"),
        data_id,
    )
    if not firma_valida:
        return JSONResponse({"error": "firma inválida"}, status_code=401)

    if topic not in _TOPICS_PREAPPROVAL and topic not in _TOPICS_PAGO:
        # incluye "payment" (el pago suelto detrás de cada invoice, no el invoice en sí) — no se
        # actúa sobre esos acá. Se responde 200 igual para que MP no reintente.
        log.info("Webhook de topic '%s' recibido, sin acción.", topic)
        return JSONResponse({"status": "ignorado"}, status_code=200)

    if not data_id:
        log.warning("Webhook de topic '%s' sin data.id — no se puede procesar.", topic)
        return JSONResponse({"status": "sin data.id"}, status_code=200)

    pool = await db.conectar()

    if topic in _TOPICS_PREAPPROVAL:
        # obtener_preapproval hace una request HTTP síncrona (SDK de MercadoPago) — se corre en
        # un thread aparte para no bloquear el event loop mientras espera la respuesta.
        preapproval = await run_in_threadpool(mercadopago_client.obtener_preapproval, data_id)
        await logica.aplicar_estado_preapproval(pool, preapproval)
    else:
        invoice = await run_in_threadpool(mercadopago_client.obtener_invoice, data_id)
        await logica.aplicar_pago_recurrente(pool, invoice)

    return JSONResponse({"status": "ok"}, status_code=200)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)


if __name__ == "__main__":
    main()
