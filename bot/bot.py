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
from telegram.error import BadRequest, Conflict, TelegramError
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

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
    """Menú de botones del /start: canales de ofertas, VIP y contacto."""
    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    vip = config.get("vip")
    contacto = config.get("contacto")

    filas = [[InlineKeyboardButton(c["nombre"], url=c["url"])] for c in ofertas if c.get("url")]
    if vip:
        # el canal VIP es privado y pago (ver PLAN_canal_vip_mercadopago.md) — el botón dispara
        # el flujo de suscripción (pide email, genera el link de pago de MercadoPago) en vez de
        # linkear directo al canal. La web usa vip["url"] para su propio botón VIP, pero apunta
        # al bot (mismo flujo), no al canal directo — el canal exige creates_join_request y
        # rechazaría a cualquiera sin suscripción activa (ver cb_solicitud_union).
        filas.append([InlineKeyboardButton(f"{vip['nombre']} 👑", callback_data="suscribirme_vip")])
    if contacto and contacto.get("url"):
        filas.append([InlineKeyboardButton("Háblame 💬", url=contacto["url"])])
    return InlineKeyboardMarkup(filas) if filas else None


def texto_bienvenida(config: dict) -> str:
    """Saludo del /start — incluye directo la explicación de los dos niveles (gratis/VIP) y el
    pago, para no depender de un botón "Información" aparte que solo agregaba un tap extra."""
    marca = config["marca"]
    nombre = " ".join(filter(None, [marca.get("nombre"), marca.get("emoji")]))
    texto = f"{marca.get('saludo', '')}\n\nSoy el bot de *{nombre}*. {marca.get('descripcion', '')}"

    # Sin repetir el nombre del canal/VIP acá: el botón de abajo ya lo dice, así que cada línea
    # va directo al beneficio en vez de duplicar "Descuentos 25%+"/"Descuentos VIP" en el texto.
    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    for canal in ofertas:
        if canal.get("descripcion"):
            texto += f"\n\n🏷️ *{canal['descripcion']}*"

    vip = config.get("vip")
    if vip and vip.get("descripcion"):
        texto += f"\n\n*{vip['descripcion']}*"
        if vip.get("nota"):
            texto += f"\n_{vip['nota']}_"

    return texto


# ------------------------------ comandos ------------------------------


async def _mostrar(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, teclado: InlineKeyboardMarkup | None
) -> None:
    """Transforma el mensaje del flujo de menú/VIP en vez de acumular mensajes nuevos.

    Si el update viene de un botón (callback_query), edita ese mismo mensaje — es el que el
    usuario acaba de tocar. Si viene de texto libre (ej. el email), edita el último mensaje del
    flujo rastreado en user_data["menu_msg_id"], porque no hay un mensaje "tocado" al que
    referirse. Si no hay nada que editar o falla (mensaje borrado, con más de 48hs, o ya tiene
    exactamente ese contenido — típico de un doble clic), manda un mensaje nuevo y lo deja
    rastreado para el próximo paso, para no dejar al usuario sin respuesta."""
    chat_id = update.effective_chat.id
    query = update.callback_query
    msg_id = query.message.message_id if query else context.user_data.get("menu_msg_id")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=texto, parse_mode="Markdown", reply_markup=teclado
            )
            context.user_data["menu_msg_id"] = msg_id
            return
        except BadRequest:
            pass
    nuevo = await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode="Markdown", reply_markup=teclado)
    context.user_data["menu_msg_id"] = nuevo.message_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = cargar_config()
    await _mostrar(update, context, texto_bienvenida(config), teclado_inicio(config))


async def cb_volver_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vuelve al menú principal y limpia cualquier estado de flujo en curso (email VIP pendiente
    de escribir o de confirmar) — mismo callback_data se reusa como "Cancelar" en esos flujos."""
    query = update.callback_query
    await query.answer()
    context.user_data["esperando_email_vip"] = False
    context.user_data.pop("email_vip_pendiente", None)
    config = cargar_config()
    await _mostrar(update, context, texto_bienvenida(config), teclado_inicio(config))


async def cb_suscribirme_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Primer paso del flujo de suscripción: MercadoPago exige el email del pagador para crear
    la preapproval (no alcanza con el user_id de Telegram). Si ya hay un email guardado de una
    suscripción anterior, se salta directo a confirmarlo; si no, se pide acá y se procesa en el
    próximo mensaje de texto (ver mensaje_no_reconocido, que revisa ese estado primero)."""
    query = update.callback_query
    await query.answer()
    config = cargar_config()
    vip = config.get("vip") or {}
    intro = f"👑 *{vip['descripcion']}*\n\n" if vip.get("descripcion") else ""

    # Si ya se suscribió antes con éxito, se saltea pedir el email de nuevo — se reusa la misma
    # pantalla de confirmación que el flujo normal (cb_confirmar_email_vip/cb_reescribir_email_vip).
    telegram_user_id = update.effective_user.id
    email_conocido = await db.obtener_email(context.bot_data["db_pool"], telegram_user_id, CANAL_ID_VIP)
    if email_conocido:
        context.user_data["esperando_email_vip"] = False
        context.user_data["email_vip_pendiente"] = email_conocido
        email_seguro = escape_markdown(email_conocido, version=1)
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí, continuar", callback_data="confirmar_email_vip")],
            [InlineKeyboardButton("✏️ Usar otro correo", callback_data="reescribir_email_vip")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")],
        ])
        await _mostrar(
            update, context, f"{intro}¿Seguís usando tu correo de MercadoPago: *{email_seguro}*?", teclado
        )
        return

    context.user_data["esperando_email_vip"] = True
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
    await _mostrar(
        update,
        context,
        f"{intro}El pago es 100% seguro con MercadoPago y podés cancelar cuando quieras.\n\n"
        "Indicá el correo de tu cuenta de MercadoPago (el mismo con el que vas a pagar).\n"
        "¿No te acordás? Abrí la app de MercadoPago → tocá tu perfil (el ícono de arriba) → "
        "ahí aparece tu email.\n\nEscribilo en tu próximo mensaje 👇",
        teclado,
    )


async def _crear_preapproval(telegram_user_id: int, email: str, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Crea la preapproval en MercadoPago y devuelve el init_point, o None si falló (ya logueado)."""
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
        return None
    return resultado["response"]["init_point"]


def teclado_error_preapproval() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Reintentar", callback_data="suscribirme_vip")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")],
    ])


async def _procesar_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Valida el formato del email y pide confirmación antes de generar el cobro — es el único
    punto del bot donde el usuario escribe texto libre, así que confirmar antes de llamar a
    MercadoPago evita que un typo mande el link de pago a un email equivocado."""
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
        await _mostrar(update, context, "Ese email no es válido 🤔 Probá escribirlo de nuevo:", teclado)
        return

    context.user_data["esperando_email_vip"] = False
    context.user_data["email_vip_pendiente"] = email
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sí, confirmar", callback_data="confirmar_email_vip")],
        [InlineKeyboardButton("✏️ Escribir de nuevo", callback_data="reescribir_email_vip")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")],
    ])
    # escape_markdown: el email es texto libre del usuario, y un "_" o "*" (común en emails)
    # rompería el parseo de Markdown si se interpola sin escapar.
    email_seguro = escape_markdown(email, version=1)
    await _mostrar(
        update, context, f"¿Este es el email de tu cuenta de MercadoPago: *{email_seguro}*?", teclado
    )


async def cb_confirmar_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # .pop() en vez de .get(): un doble clic no encuentra email la segunda vez, evitando
    # generar dos preapprovals para el mismo pago.
    email = context.user_data.pop("email_vip_pendiente", None)
    if not email:
        teclado = InlineKeyboardMarkup(
            [[InlineKeyboardButton("👑 Suscribirme al VIP", callback_data="suscribirme_vip")]]
        )
        await _mostrar(update, context, "Esta confirmación ya venció. Empecemos de nuevo:", teclado)
        return

    telegram_user_id = update.effective_user.id
    init_point = await _crear_preapproval(telegram_user_id, email, context)
    if not init_point:
        await _mostrar(
            update, context, "No pudimos generar el link de pago. Probá de nuevo:", teclado_error_preapproval()
        )
        return

    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pagar suscripción", url=init_point)]])
    await _mostrar(update, context, "Listo 🙌 Tocá el botón para completar el pago y activar tu VIP:", teclado)


async def cb_reescribir_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("email_vip_pendiente", None)
    context.user_data["esperando_email_vip"] = True
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
    await _mostrar(
        update, context,
        "Dale, escribí tu email de nuevo. Lo encontrás en la app de MercadoPago, en tu perfil "
        "(el ícono de arriba).",
        teclado,
    )


async def mensaje_no_reconocido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("esperando_email_vip"):
        await _procesar_email_vip(update, context)
        return
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]])
    await _mostrar(update, context, "No entendí ese mensaje 🤔", teclado)


async def cb_solicitud_union(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """El link de invitación al canal VIP (pagos/telegram_client.py::invitar) ya no deja entrar
    directo — pide aprobación a propósito, porque un link "de un solo uso" no verifica quién lo
    usa (si el suscriptor lo reenvía antes de entrar, cualquiera podría ocupar ese cupo). Acá se
    aprueba o rechaza comparando la identidad real de quien pide entrar contra su suscripción."""
    solicitud = update.chat_join_request
    activo = await db.esta_activo(context.bot_data["db_pool"], solicitud.from_user.id, CANAL_ID_VIP)
    if activo:
        await solicitud.approve()
        log.info("Solicitud de unión aprobada: %s", solicitud.from_user.id)
        try:
            # Telegram no siempre lleva al usuario al canal solo tras aprobar una solicitud de
            # unión — sin este aviso, un usuario común se queda esperando sin saber que ya puede
            # entrar. Mismo criterio que invitar(): si el DM falla (nunca le escribió al bot), se
            # loguea sin cortar el flujo — la aprobación en sí ya se hizo.
            await context.bot.send_message(
                chat_id=solicitud.from_user.id,
                text=(
                    "✅ ¡Listo! Tu solicitud fue aprobada. Si Telegram no te abrió el canal solo, "
                    "tocá de nuevo el link que te mandamos."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]]
                ),
            )
        except TelegramError:
            log.exception(
                "No se pudo avisar por DM a %s que su solicitud fue aprobada", solicitud.from_user.id,
            )
    else:
        await solicitud.decline()
        log.info("Solicitud de unión rechazada (sin suscripción activa): %s", solicitud.from_user.id)
        try:
            await context.bot.send_message(
                chat_id=solicitud.from_user.id,
                text=(
                    "No pudimos darte acceso: no encontramos una suscripción activa a tu nombre. "
                    "Si ya pagaste y creés que es un error, escribinos. Si no, sumate al VIP acá:"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("👑 Suscribirme al VIP", callback_data="suscribirme_vip")]]
                ),
            )
        except TelegramError:
            log.exception(
                "No se pudo avisar por DM a %s que su solicitud fue rechazada", solicitud.from_user.id,
            )


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


async def _post_init(app: Application) -> None:
    await sincronizar_perfil(app)
    app.bot_data["db_pool"] = await db.conectar(app.bot_data["database_url"])


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

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Falta DATABASE_URL en bot/.env")

    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["database_url"] = database_url
    app.bot_data["mp_sdk"] = mercadopago.SDK(mp_access_token)
    app.bot_data["mp_monto"] = int(mp_monto)
    app.bot_data["mp_frecuencia"] = int(mp_frecuencia)
    app.bot_data["mp_frecuencia_tipo"] = mp_frecuencia_tipo

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_suscribirme_vip, pattern="^suscribirme_vip$"))
    app.add_handler(CallbackQueryHandler(cb_confirmar_email_vip, pattern="^confirmar_email_vip$"))
    app.add_handler(CallbackQueryHandler(cb_reescribir_email_vip, pattern="^reescribir_email_vip$"))
    app.add_handler(CallbackQueryHandler(cb_volver_menu, pattern="^volver_menu$"))
    app.add_handler(ChatJoinRequestHandler(cb_solicitud_union))
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
