"""Acciones puntuales sobre la API de Telegram para canales pagos: invitación de un solo uso y
expulsión. Mismo patrón que scraper/telegram_publisher.py — un Bot(token=...) "de una sola vez"
por llamada, sin correr run_polling() (no hace falta el proceso del bot activo para esto)."""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import TelegramError

import config

log = logging.getLogger("pagos.telegram_client")


async def invitar(telegram_user_id: int, canal_id: str) -> None:
    """Genera un invite link de un solo uso al canal pago y lo manda por DM. Si el usuario nunca
    le escribió al bot, el DM falla (Telegram no deja iniciar conversaciones en frío) — queda
    logueado; el usuario puede escribirle /start al bot y volver a intentar el pago para
    destrabarlo (fuera de alcance resolverlo automático en esta primera versión)."""
    chat_id = config.CANAL_CHAT_ID[canal_id]
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        invite = await bot.create_chat_invite_link(chat_id=chat_id, member_limit=1)
        try:
            await bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "✅ ¡Pago confirmado! Acá está tu invitación al canal VIP "
                    "(válida solo para vos, un solo uso):\n" + invite.invite_link
                ),
            )
        except TelegramError:
            log.exception(
                "No se pudo mandar la invitación por DM a %s (¿nunca le escribió al bot?)",
                telegram_user_id,
            )


async def expulsar(telegram_user_id: int, canal_id: str) -> None:
    """Saca al usuario del canal — ban seguido de unban inmediato (equivalente a un "kick": lo
    saca ahora, pero no le bloquea volver a entrar si paga de nuevo más adelante)."""
    chat_id = config.CANAL_CHAT_ID[canal_id]
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=telegram_user_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=telegram_user_id, only_if_banned=True)
        except TelegramError:
            log.exception("No se pudo expulsar a %s del canal %s", telegram_user_id, canal_id)
