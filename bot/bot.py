"""
Bot de Telegram — agregador de descuentos.

Toda la marca, los canales y los textos salen de web/data/config.json,
la misma fuente que usa la web. Editar ese archivo actualiza el bot en
la próxima interacción, sin reiniciar el proceso.
"""

import json
import logging
import os
from pathlib import Path

import mercadopago
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

RUTA_CONFIG = Path(__file__).resolve().parent.parent / "web" / "data" / "config.json"

CANAL_ID_VIP = "vip"  # ver pagos/config.py::CANAL_CHAT_ID — mismo id en ambos lados
# mismo back_url usado al crear el Preapproval Plan (ver PLAN_canal_vip_mercadopago.md) — a dónde
# vuelve el usuario después de autorizar el pago en MercadoPago.
BACK_URL_MERCADOPAGO = "https://t.me/descuentos_bigolito_cl_bot"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# Si config.json falta o está roto, el bot sigue respondiendo con esto
# en vez de caerse. Mismo criterio de robustez que web/js/data.js.
CONFIG_MINIMA = {
    "marca": {"nombre": "Descuentos Bigolito", "emoji": "🐶", "saludo": "Hola!", "descripcion": ""},
    "canales": [],
    "vip": None,
    "redes": [],
    "contacto": None,
    "bot": {"descripcion_larga": "", "descripcion_corta": "", "comandos": []},
}


def cargar_config() -> dict:
    try:
        with open(RUTA_CONFIG, encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return {**CONFIG_MINIMA, **datos}
    except (OSError, json.JSONDecodeError) as error:
        log.warning("No se pudo leer config.json: %s", error)
        return CONFIG_MINIMA


def teclado_inicio(config: dict) -> InlineKeyboardMarkup | None:
    """Menú de botones del /start: canales de ofertas, VIP, ayuda e información."""
    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    vip = config.get("vip")
    contacto = config.get("contacto")

    filas = [[InlineKeyboardButton(c["nombre"], url=c["url"])] for c in ofertas if c.get("url")]
    if vip:
        # el canal VIP es privado y pago (ver PLAN_canal_vip_mercadopago.md) — el botón dispara
        # el flujo de suscripción (pide email, genera el link de pago de MercadoPago) en vez de
        # linkear directo al canal como antes. La web sigue usando vip["url"] para su propio
        # botón — ese lado queda pendiente aparte, no lo toca este cambio.
        filas.append([InlineKeyboardButton(f"{vip['nombre']} 👑", callback_data="suscribirme_vip")])
    if contacto and contacto.get("url"):
        filas.append([InlineKeyboardButton("Háblame 💬", url=contacto["url"])])
    filas.append([InlineKeyboardButton("ℹ️ Información", callback_data="informacion")])
    return InlineKeyboardMarkup(filas) if filas else None


def texto_informacion(config: dict) -> str:
    marca = config["marca"]
    nombre = " ".join(filter(None, [marca.get("nombre"), marca.get("emoji")]))
    return f"*{nombre}*\n\n{marca.get('descripcion', '')}\n\nToca 🆘 Ayuda en el menú si necesitas hablar con nosotros."


# ------------------------------ comandos ------------------------------


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    config = cargar_config()
    marca = config["marca"]
    nombre = " ".join(filter(None, [marca.get("nombre"), marca.get("emoji")]))

    texto = f"{marca.get('saludo', '')}\n\nSoy el bot de *{nombre}*. {marca.get('descripcion', '')}"
    await update.message.reply_text(texto, parse_mode="Markdown", reply_markup=teclado_inicio(config))


async def cb_informacion(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config = cargar_config()
    await query.message.reply_text(texto_informacion(config), parse_mode="Markdown")


async def cb_suscribirme_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Primer paso del flujo de suscripción: MercadoPago exige el email del pagador para crear
    la preapproval (no alcanza con el user_id de Telegram) — se lo pedimos acá y se procesa en
    el próximo mensaje de texto (ver mensaje_no_reconocido, que revisa este estado primero)."""
    query = update.callback_query
    await query.answer()
    context.user_data["esperando_email_vip"] = True
    await query.message.reply_text(
        "Para suscribirte al canal VIP necesito tu email (MercadoPago lo pide para el cobro).\n"
        "Escribilo en tu próximo mensaje:"
    )


async def _procesar_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        await update.message.reply_text("Ese no parece un email válido. Escribilo de nuevo:")
        return
    context.user_data["esperando_email_vip"] = False

    telegram_user_id = update.effective_user.id
    try:
        # Sin preapproval_plan_id a propósito: esa variante exige card_token_id (tarjeta
        # tokenizada de antemano, pensado para un checkout propio) y no genera un init_point
        # para redirigir — la que sí lo genera es esta, "sin plan asociado" con status=pending,
        # confirmado contra la documentación oficial tras un 400 real (card_token_id is required).
        resultado = context.bot_data["mp_sdk"].preapproval().create({
            "reason": "Suscripción VIP — Descuentos Bigolito",
            "external_reference": f"{telegram_user_id}:{CANAL_ID_VIP}",
            "payer_email": email,
            "auto_recurring": {
                "frequency": context.bot_data["mp_frecuencia"],
                "frequency_type": context.bot_data["mp_frecuencia_tipo"],
                "transaction_amount": context.bot_data["mp_monto"],
                "currency_id": "CLP",
            },
            "back_url": BACK_URL_MERCADOPAGO,
            "status": "pending",
        })
        resultado.raise_for_status()
    except Exception:
        log.exception("Falló la creación de preapproval para %s", telegram_user_id)
        await update.message.reply_text(
            "Hubo un problema generando el link de pago. Probá de nuevo más tarde."
        )
        return

    init_point = resultado["response"]["init_point"]
    await update.message.reply_text(
        "Listo, completá el pago acá para activar tu suscripción VIP:\n" + init_point
    )


async def mensaje_no_reconocido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("esperando_email_vip"):
        await _procesar_email_vip(update, context)
        return
    await update.message.reply_text("No entendí ese mensaje 🤔 Usa /start para ver el menú.")


async def manejar_error(_, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB ya reintenta solo ante errores de red/Telegram, no hace falta relanzar nada acá.

    Solo evita que un Conflict (dos instancias compitiendo por el mismo token
    durante un redeploy) llene los logs de Railway con un traceback completo
    en cada reintento — para cualquier otro error, sí queremos el traceback.
    """
    if isinstance(context.error, Conflict):
        log.warning("Otra instancia del bot sigue activa — probablemente un redeploy en curso.")
    else:
        log.error("Error no manejado", exc_info=context.error)


# ------------------------------ arranque ------------------------------


async def sincronizar_perfil(app: Application) -> None:
    """Aplica descripción, bio corta y comandos vía Bot API.

    Equivale a ejecutar /setdescription, /setabouttext y /setcommands en
    BotFather, pero sin intervención manual. Idempotente: correrlo de
    nuevo simplemente vuelve a aplicar el mismo contenido de config.json.
    """
    config = cargar_config()
    bot_cfg = config.get("bot", {})

    if bot_cfg.get("descripcion_larga"):
        await app.bot.set_my_description(bot_cfg["descripcion_larga"])
    if bot_cfg.get("descripcion_corta"):
        await app.bot.set_my_short_description(bot_cfg["descripcion_corta"])
    comandos = bot_cfg.get("comandos") or []
    if comandos:
        await app.bot.set_my_commands(
            [BotCommand(c["comando"], c["descripcion"]) for c in comandos]
        )
    else:
        await app.bot.delete_my_commands()
    log.info("Perfil del bot sincronizado con config.json")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN en bot/.env")

    mp_access_token = os.environ.get("MERCADOPAGO_ACCESS_TOKEN")
    mp_monto = os.environ.get("MERCADOPAGO_SUSCRIPCION_MONTO")
    mp_frecuencia = os.environ.get("MERCADOPAGO_SUSCRIPCION_FRECUENCIA")
    mp_frecuencia_tipo = os.environ.get("MERCADOPAGO_SUSCRIPCION_FRECUENCIA_TIPO")
    if not all([mp_access_token, mp_monto, mp_frecuencia, mp_frecuencia_tipo]):
        raise SystemExit(
            "Falta MERCADOPAGO_ACCESS_TOKEN, MERCADOPAGO_SUSCRIPCION_MONTO, "
            "MERCADOPAGO_SUSCRIPCION_FRECUENCIA o MERCADOPAGO_SUSCRIPCION_FRECUENCIA_TIPO en bot/.env"
        )

    app = Application.builder().token(token).post_init(sincronizar_perfil).build()
    app.bot_data["mp_sdk"] = mercadopago.SDK(mp_access_token)
    app.bot_data["mp_monto"] = int(mp_monto)
    app.bot_data["mp_frecuencia"] = int(mp_frecuencia)
    app.bot_data["mp_frecuencia_tipo"] = mp_frecuencia_tipo

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_informacion, pattern="^informacion$"))
    app.add_handler(CallbackQueryHandler(cb_suscribirme_vip, pattern="^suscribirme_vip$"))
    app.add_handler(MessageHandler(filters.COMMAND | filters.TEXT, mensaje_no_reconocido))
    app.add_error_handler(manejar_error)

    log.info("Bot arrancando (polling)...")
    # run_polling() cierra en orden ante SIGTERM (stop_signals trae SIGINT/SIGTERM/SIGABRT
    # por defecto) — pero eso solo funciona porque bot/Dockerfile usa CMD en forma exec
    # (["python", "bot.py"]), con Python como PID 1. Si algún día se envuelve el arranque
    # en un shell, la señal deja de llegarle y el cierre ordenado se rompe en silencio.
    #
    # Deliberadamente SIN drop_pending_updates: Telegram ya evita que dos instancias
    # procesen el mismo mensaje (por eso existe el 409 Conflict, no una entrega duplicada),
    # así que ese flag no protegía contra nada real — y sí descartaba mensajes que hubieran
    # llegado justo durante la ventana de un redeploy, en vez de solo demorarlos.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
