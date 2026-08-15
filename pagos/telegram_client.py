"""Acciones puntuales sobre la API de Telegram para canales pagos: invitación de un solo uso y
expulsión. Mismo patrón que scraper/telegram_publisher.py — un Bot(token=...) "de una sola vez"
por llamada, sin correr run_polling() (no hace falta el proceso del bot activo para esto)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

import config

log = logging.getLogger("pagos.telegram_client")


async def invitar(telegram_user_id: int, canal_id: str) -> None:
    """Genera un invite link por cada chat que el canal pago desbloquee (ver
    config.CANAL_CHAT_ID — un canal_id puede dar acceso a más de un chat) y manda un solo DM con
    un botón por chat. No usa member_limit=1 — la documentación de Telegram aclara que ese límite
    es de miembros *simultáneos*, no de usos acumulados, y en cualquier caso no verifica que quien
    entra sea quien pagó (si el suscriptor reenvía el link antes de usarlo, cualquiera podría
    entrar primero). En cambio, cada link pide aprobación (creates_join_request) y
    bot/bot.py::cb_solicitud_union decide caso por caso contra la base de datos, sin importar
    cuánto circule el link. expire_date es solo higiene (evita solicitudes eternas si el bot
    estuviera caído mucho tiempo), no la defensa real.

    Si crear el link falla para alguno de los chats (ej. el bot todavía no es admin ahí), se
    loguea puntual y se sigue con los demás — no tiene sentido perder el acceso a los chats que sí
    funcionan por uno que esté mal configurado.

    Si el usuario nunca le escribió al bot, el DM falla (Telegram no deja iniciar conversaciones
    en frío) — queda logueado; el usuario puede escribirle /start al bot y volver a intentar el
    pago para destrabarlo (fuera de alcance resolverlo automático en esta primera versión)."""
    botones = []
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        for chat_id, etiqueta in config.CANAL_CHAT_ID[canal_id]:
            try:
                invite = await bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
                )
            except TelegramError:
                log.exception(
                    "No se pudo crear el invite link para %s (canal %s, chat %s)",
                    telegram_user_id, canal_id, chat_id,
                )
                continue
            botones.append([InlineKeyboardButton(f"🔓 Entrar al canal {etiqueta}", url=invite.invite_link)])

        if not botones:
            log.error(
                "No se pudo generar ningún invite link para %s (canal %s) — no se manda DM",
                telegram_user_id, canal_id,
            )
            return

        try:
            await bot.send_message(
                chat_id=telegram_user_id,
                text="✅ ¡Pago confirmado! Tocá el botón para entrar (invitación válida solo para vos):",
                reply_markup=InlineKeyboardMarkup(botones),
            )
        except TelegramError:
            log.exception(
                "No se pudo mandar la invitación por DM a %s (¿nunca le escribió al bot?)",
                telegram_user_id,
            )


async def expulsar(telegram_user_id: int, canal_id: str) -> None:
    """Saca al usuario de cada chat que el canal pago desbloquea (ver config.CANAL_CHAT_ID) — ban
    seguido de unban inmediato por chat (equivalente a un "kick": lo saca ahora, pero no le
    bloquea volver a entrar si paga de nuevo más adelante) — y le avisa por DM con un botón para
    renovar. El botón reusa callback_data=f"suscribirme_{canal_id}": lo captura
    bot/bot.py::cb_suscribirme sin importar que este mensaje lo haya mandado el servicio de pagos,
    porque los clics le llegan a quien esté haciendo run_polling() (el servicio bot).

    Cada chat se intenta por separado — que falle en uno (ej. el bot ya no es admin ahí) no debe
    impedir sacarlo de los demás. Solo se omite el DM de renovación si no se pudo expulsar de
    ningún chat (ahí sí no tiene sentido avisar)."""
    algun_exito = False
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        for chat_id, etiqueta in config.CANAL_CHAT_ID[canal_id]:
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=telegram_user_id)
                await bot.unban_chat_member(chat_id=chat_id, user_id=telegram_user_id, only_if_banned=True)
                algun_exito = True
            except TelegramError:
                log.exception(
                    "No se pudo expulsar a %s del canal %s (chat %s / %s)",
                    telegram_user_id, canal_id, chat_id, etiqueta,
                )

        if not algun_exito:
            return  # no se pudo expulsar de ningún chat, no tiene sentido avisar que se expulsó

        try:
            teclado = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Renovar suscripción", callback_data=f"suscribirme_{canal_id}")]]
            )
            await bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "Tu acceso a este canal terminó. Si querés seguir recibiendo las ofertas "
                    "exclusivas, renová tu suscripción:"
                ),
                reply_markup=teclado,
            )
        except TelegramError:
            log.exception(
                "No se pudo avisar por DM a %s que fue expulsado del canal %s",
                telegram_user_id, canal_id,
            )
