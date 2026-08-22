"""Configuración y constantes del servicio de pagos (webhook + reconciliación)."""
from __future__ import annotations

import os

# --- Servidor HTTP del webhook (Railway inyecta PORT dinámicamente) ---
PORT = int(os.environ.get("PORT", "8000"))

# --- Base de datos (mismo Postgres que ya usa scraper/db.py) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Telegram (mismo bot que bot/bot.py y scraper/telegram_publisher.py) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# canal privado admin-only ("Subs Bigolito💰") donde se avisa cada alta/renovación confirmada —
# distinto del TELEGRAM_ADMIN_CHAT_ID de scraper/ (ese es para alertas operativas de scraping, no
# de ventas). Si no está seteado, avisar_pago() solo loguea y sigue.
TELEGRAM_ADMIN_CHAT_ID_PAGOS = os.environ.get("TELEGRAM_ADMIN_CHAT_ID_PAGOS", "")

# --- MercadoPago ---
MERCADOPAGO_ACCESS_TOKEN = os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "")
# secreto de firma que genera MercadoPago al configurar la URL de notificación — valida que cada
# webhook que llega de verdad viene de MercadoPago (ver pagos/webhook.py).
MERCADOPAGO_WEBHOOK_SECRET = os.environ.get("MERCADOPAGO_WEBHOOK_SECRET", "")

# secreto compartido con bot/ (mismo valor en los dos .env) para firmar el link de pago con
# tarjeta que el bot genera — sin esto, cualquiera podría cambiar telegram_user_id/canal_id/email
# en la URL y generar un cobro/acceso a nombre de otro usuario, ya que ese endpoint lo llama el
# navegador directo, sin sesión autenticada de Telegram (ver pagos/pagos_tarjeta.py).
LINK_PAGO_SECRET = os.environ.get("LINK_PAGO_SECRET", "")

# chat_id(s) de Telegram por canal pago (canal privado, ver
# scraper/config.py::CANAL_TELEGRAM_USERNAME para el mecanismo equivalente del lado del scraper).
# Cada canal_id mapea a una LISTA de (chat_id, etiqueta) — una misma suscripción puede dar acceso
# a más de un chat (ej. "vip" da acceso al VIP general y al Geek VIP, sin cobro aparte). Sumar un
# canal pago nuevo con su propio precio es agregar una key acá (y en web/data/config.json
# ::canales_pagos); sumar un chat más a un canal_id existente es agregar un elemento a su lista.
CANAL_CHAT_ID = {
    "vip": [
        ("-1004438197572", "+50% OFF RETAIL VIP 💎"),
        ("-1003952570153", "+20% OFF GEEK VIP 🕹️"),
        ("-1003961858440", "+20% OFF DECO/HOGAR VIP 🏠"),
        ("-1004332754687", "+20% OFF TECH VIP 🖥"),
        ("-1004304511002", "+25% OFF MASCOTAS VIP 🐶🐱"),
        ("-1004357028199", "+25% OFF FITNESS VIP 🏋️"),
    ],
    "test2": [("CAMBIAR_POR_CHAT_ID_REAL", "Canal Test 2")],  # canal de prueba, todavía no existe en Telegram
}
