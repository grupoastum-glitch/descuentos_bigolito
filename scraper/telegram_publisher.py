"""Publica en el canal de Telegram correspondiente las ofertas que cumplen alguna de las Reglas
1/2/3 (ver ofertas_writer.py) — récord de precio, récord de descuento, o republicación por
antigüedad.

Reusa el mismo bot que ya responde comandos privados (bot/bot.py) — necesita el mismo token,
y el bot necesita ser administrador (permiso "Publicar mensajes") en cada canal de
config.CANAL_TELEGRAM_USERNAME.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError, TimedOut

import config

log = logging.getLogger("scraper.telegram_publisher")

MAX_EVENTOS_HISTORIAL_EN_CAPTION = 5  # Telegram limita el caption de una foto a 1024 caracteres


def _formatear_clp(monto: int) -> str:
    return "$" + format(monto, ",").replace(",", ".")


def _formatear_fecha(fecha_iso: str) -> str:
    return datetime.strptime(fecha_iso, "%Y-%m-%dT%H:%M:%SZ").strftime("%d/%m/%Y")


def _formatear_caption(oferta: dict) -> str:
    # parse_mode=HTML (ver _enviar_con_reintento) — escapar todo texto libre para que un
    # título con "&"/"<"/">" no rompa el formato ni tire BadRequest al mandar el mensaje.
    titulo = html.escape(oferta["titulo"])
    url = html.escape(oferta["url"])
    precio_actual_negrita = f"<b>{_formatear_clp(oferta['precio_actual'])}</b>"

    hashtag_comercio = oferta["comercio"].replace(" ", "")
    lineas = [
        f"🤖 🎯 #{hashtag_comercio} ✨💰 Dscto. {oferta['descuento_pct']}%",
        "",
        titulo,
        "",
    ]

    if oferta.get("precio_normal"):
        lineas.append(
            f"{_formatear_clp(oferta['precio_normal'])} → "
            f"{precio_actual_negrita} ({oferta['descuento_pct']}%)"
        )
    else:
        lineas.append(f"{precio_actual_negrita} ({oferta['descuento_pct']}%)")

    historial = oferta["historial"]
    eventos = historial[-MAX_EVENTOS_HISTORIAL_EN_CAPTION:]
    lineas.append("")
    lineas.append("📊 Historial de precios oferta")
    if len(eventos) < len(historial):
        lineas.append(f"(+{len(historial) - len(eventos)} eventos anteriores)")
    for evento in eventos:
        lineas.append(
            f"{_formatear_clp(evento['precio'])} | {evento['descuento_pct']}% off | "
            f"{_formatear_fecha(evento['fecha'])}"
        )

    lineas.append("")
    if oferta.get("precio_minimo_anterior") is None:
        lineas.append("✅ Precio mínimo histórico")
    else:
        lineas.append("🏷️ Mayor descuento registrado")
        lineas.append(
            f"Precio mínimo anterior: {_formatear_clp(oferta['precio_minimo_anterior'])} "
            f"el {_formatear_fecha(oferta['fecha_precio_minimo_anterior'])}"
        )

    lineas.append("")
    lineas.append("🔗 VER PRODUCTO 👀")
    lineas.append(url)

    # backstop defensivo — el recorte de eventos de arriba ya debería alcanzar, pero un título
    # muy largo podría igual pasarse del límite de Telegram para el caption de una foto (1024,
    # menor al límite de 4096 de un mensaje de texto plano).
    return "\n".join(lineas)[:1024]


async def avisar_admin(mensaje: str) -> None:
    """Manda un mensaje de texto plano a TELEGRAM_ADMIN_CHAT_ID (avisos operativos, no ofertas).
    Si no está configurado, solo queda en el log — no rompe la corrida."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_ADMIN_CHAT_ID:
        log.warning("TELEGRAM_ADMIN_CHAT_ID no configurado, no se puede avisar: %s", mensaje)
        return

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        try:
            await bot.send_message(chat_id=config.TELEGRAM_ADMIN_CHAT_ID, text=f"⚠️ {mensaje}")
        except TelegramError:
            log.exception("Falló el aviso al admin: %s", mensaje)


async def _enviar_con_reintento(bot: Bot, chat_id: str, oferta: dict) -> bool:
    """Manda la oferta con foto (bot.send_photo) si hay `imagen` — el preview automático de
    Telegram a partir del link no es confiable para todas las tiendas (Paris, por ejemplo, no
    trae imagen en su preview), así que se manda explícita. Si `send_photo` falla por un
    problema de la imagen en sí (URL rota, formato no soportado, etc. — típicamente
    BadRequest), se cae a texto plano en el mismo intento en vez de perder la oferta; los
    errores transitorios de Telegram (RetryAfter/TimedOut) se dejan propagar para que el loop
    de abajo espere y reintente el envío completo. Devuelve True si se logró publicar (con o
    sin foto), False si se agotaron los reintentos (pérdida definitiva)."""
    texto = _formatear_caption(oferta)
    for intento in range(config.TELEGRAM_REINTENTOS_MAX + 1):
        try:
            if oferta.get("imagen"):
                try:
                    await bot.send_photo(
                        chat_id=chat_id, photo=oferta["imagen"], caption=texto, parse_mode=ParseMode.HTML,
                    )
                except (RetryAfter, TimedOut):
                    raise
                except TelegramError:
                    log.warning(
                        "send_photo falló para %s (imagen inválida?), fallback a texto: %s",
                        chat_id, oferta.get("imagen"),
                    )
                    await bot.send_message(chat_id=chat_id, text=texto, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=chat_id, text=texto, parse_mode=ParseMode.HTML)
            return True
        except RetryAfter as error:
            if intento == config.TELEGRAM_REINTENTOS_MAX:
                break
            log.warning(
                "Flood control en %s, se espera %ss y se reintenta (intento %s/%s)",
                chat_id, error.retry_after, intento + 1, config.TELEGRAM_REINTENTOS_MAX,
            )
            await asyncio.sleep(error.retry_after)
        except TimedOut:
            if intento == config.TELEGRAM_REINTENTOS_MAX:
                break
            log.warning(
                "Timeout en %s, se reintenta (intento %s/%s)",
                chat_id, intento + 1, config.TELEGRAM_REINTENTOS_MAX,
            )
            await asyncio.sleep(config.TELEGRAM_DELAY_SEGUNDOS)
    return False


async def publicar_ofertas_nuevas(ofertas: list[dict]) -> set[str]:
    """Intenta publicar cada oferta en su canal. Devuelve los `id` de las que realmente se
    mandaron — pasarlo a ofertas_writer.confirmar_publicaciones() para que el historial solo
    se actualice en las que de verdad llegaron a Telegram (no en las que no tenían canal activo
    o cuyo envío falló del todo)."""
    ids_confirmados: set[str] = set()
    if not ofertas:
        return ids_confirmados
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN no configurado, se omite el posteo a canales")
        return ids_confirmados

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    ultimo_posteo_por_canal: dict[str, float] = {}
    async with bot:
        for oferta in ofertas:
            username = config.CANAL_TELEGRAM_USERNAME.get(oferta.get("canal"))
            if not username:
                # tier sin canal activo todavía (ver config.CANAL_TELEGRAM_USERNAME) — se omite,
                # sigue disponible como candidata en la próxima corrida (no se confirma acá)
                continue
            chat_id = f"@{username}"

            # el límite de flood control de Telegram es por chat, no global del bot — dos
            # posteos seguidos a canales distintos no compiten por el mismo límite. Además,
            # espaciar solo por canal (no global) hace que cada canal se sienta fluido por su
            # cuenta, sin que el ritmo de uno le "robe" tiempo de espera al otro.
            ultimo_posteo = ultimo_posteo_por_canal.get(chat_id)
            if ultimo_posteo is not None:
                espera = config.TELEGRAM_DELAY_SEGUNDOS - (time.monotonic() - ultimo_posteo)
                if espera > 0:
                    await asyncio.sleep(espera)

            try:
                publicado = await _enviar_con_reintento(bot, chat_id, oferta)
                if publicado:
                    log.info("Publicado en %s: %s", chat_id, oferta["titulo"])
                    ids_confirmados.add(oferta["id"])
                else:
                    log.error(
                        "Se agotaron los reintentos por flood control en %s para la oferta %s — "
                        "sigue disponible como candidata, se reintenta en la próxima corrida",
                        chat_id, oferta["id"],
                    )
            except TelegramError:
                log.exception("Falló el posteo a %s para la oferta %s", chat_id, oferta["id"])
            ultimo_posteo_por_canal[chat_id] = time.monotonic()
    return ids_confirmados
