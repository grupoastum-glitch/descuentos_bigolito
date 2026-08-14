"""Configuración y constantes del servicio de pagos (webhook + reconciliación)."""
from __future__ import annotations

import os

# --- Servidor HTTP del webhook (Railway inyecta PORT dinámicamente) ---
PORT = int(os.environ.get("PORT", "8000"))

# --- Base de datos (mismo Postgres que ya usa scraper/db.py) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Telegram (mismo bot que bot/bot.py y scraper/telegram_publisher.py) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# --- MercadoPago ---
MERCADOPAGO_ACCESS_TOKEN = os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "")
# secreto de firma que genera MercadoPago al configurar la URL de notificación — valida que cada
# webhook que llega de verdad viene de MercadoPago (ver pagos/webhook.py).
MERCADOPAGO_WEBHOOK_SECRET = os.environ.get("MERCADOPAGO_WEBHOOK_SECRET", "")

# chat_id numérico por canal pago (canal privado, ver scraper/config.py::CANAL_TELEGRAM_USERNAME
# para el mecanismo equivalente del lado del scraper). Sumar un canal pago nuevo es agregar una
# línea acá, sin tocar el resto del código — mismo criterio de extensibilidad.
CANAL_CHAT_ID = {
    "vip": "-1004438197572",
    "test2": "CAMBIAR_POR_CHAT_ID_REAL",  # canal de prueba, todavía no existe en Telegram
}
