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
    if vip and vip.get("url"):
        filas.append([InlineKeyboardButton(f"👑 {vip['nombre']}", url=vip["url"])])
    if contacto and contacto.get("url"):
        filas.append([InlineKeyboardButton("🆘 Ayuda", url=contacto["url"])])
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


async def mensaje_no_reconocido(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
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

    app = Application.builder().token(token).post_init(sincronizar_perfil).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_informacion, pattern="^informacion$"))
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
