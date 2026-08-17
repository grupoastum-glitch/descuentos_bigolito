"""
Bot de Telegram — agregador de descuentos.

Toda la marca, los canales y los textos salen de web/data/config.json,
la misma fuente que usa la web. Editar ese archivo actualiza el bot en
la próxima interacción, sin reiniciar el proceso.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

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

# chat_id(s) por canal pago — duplicado a propósito de pagos/config.py::CANAL_CHAT_ID:
# bot/ y pagos/ son servicios independientes (containers separados, sin imports cruzados), mismo
# criterio de "cada uno autocontenido" que ya usa el resto del proyecto. Cada canal_id mapea a una
# LISTA de (chat_id, etiqueta) — una misma suscripción puede dar acceso a más de un chat (ej. "vip"
# da acceso al VIP general y al Geek VIP, sin cobro aparte). Sumar un canal pago nuevo con su
# propio precio es agregar una key acá Y en pagos/config.py (además de web/data/config.json
# ::canales_pagos); sumar un chat más a un canal_id existente es agregar un elemento a su lista.
CANAL_CHAT_ID = {
    "vip": [
        ("-1004438197572", "+50% OFF RETAIL VIP 💎"),
        ("-1003952570153", "+20% OFF GEEK VIP 🕹️"),
        ("-1003961858440", "+20% OFF DECO/HOGAR VIP 🏠"),
        ("-1004332754687", "+20% OFF TECH VIP 🖥"),
    ],
    "test2": [("CAMBIAR_POR_CHAT_ID_REAL", "Canal Test 2")],  # canal de prueba, todavía no existe en Telegram
}
TZ_CHILE = ZoneInfo("America/Santiago")  # acceso_hasta se guarda en UTC — convertir antes de mostrar
# ventana para no repetir el aviso de "solicitud aprobada" en cb_solicitud_union: un canal_id con
# varios chats (ej. "vip" = 4) genera una solicitud de unión por chat, y el usuario suele tocar
# los links casi al mismo tiempo — sin esto le llegarían N DMs idénticos seguidos.
VENTANA_DEBOUNCE_APROBACION_SEGUNDOS = 10
# mismo back_url usado al crear el Preapproval Plan (ver PLAN_canal_vip_mercadopago.md) — a dónde
# vuelve el usuario después de autorizar el pago en MercadoPago.
BACK_URL_MERCADOPAGO = "https://t.me/descuentos_bigolito_cl_bot"

# página propia (Cloudflare Pages, mismo sitio que web/index.html) con el Card Payment Brick de
# MercadoPago — alternativa de pago con tarjeta para quien no tiene cuenta de MercadoPago, ver
# _firmar_link_tarjeta y pagos/pagos_tarjeta.py (el endpoint que la procesa del otro lado).
URL_PAGO_TARJETA = "https://www.descuentosbigolito.cl/pagar-tarjeta.html"
# ventana de validez del link firmado — corta a propósito, se genera y se usa al toque en la
# misma conversación con el bot.
_VENTANA_LINK_TARJETA_SEGUNDOS = 15 * 60

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
    "canales_pagos": [],
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


def _canales_pagos(config: dict) -> dict[str, dict]:
    """Canales pagos definidos en config.json, indexados por canal_id."""
    return {c["canal_id"]: c for c in config.get("canales_pagos") or [] if c.get("canal_id")}


def teclado_inicio(config: dict) -> InlineKeyboardMarkup | None:
    """Menú de botones del /start: canales de ofertas, canales pagos visibles y contacto."""
    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    contacto = config.get("contacto")

    filas = [[InlineKeyboardButton(c["nombre"], url=c["url"])] for c in ofertas if c.get("url")]
    for canal in config.get("canales_pagos") or []:
        if not canal.get("visible", True):
            continue
        # canal pago = privado (ver PLAN_canal_vip_mercadopago.md) — el botón dispara el flujo de
        # suscripción (pide email, genera el link de pago de MercadoPago) en vez de linkear
        # directo al canal, que exige creates_join_request y rechazaría a cualquiera sin
        # suscripción activa (ver cb_solicitud_union). La web tiene su propio botón fijo para el
        # VIP (clave "vip" de config.json) que apunta al bot, mismo flujo.
        filas.append(
            [InlineKeyboardButton(
                f"Suscribirme {canal.get('nombre_corto', canal['nombre'])} 👑",
                callback_data=f"suscribirme_{canal['canal_id']}",
            )]
        )
    filas.append([InlineKeyboardButton("📡 Ver mis canales", callback_data="ver_mis_canales")])
    if contacto and contacto.get("url"):
        filas.append([InlineKeyboardButton("Háblame 💬", url=contacto["url"])])
    return InlineKeyboardMarkup(filas) if filas else None


def texto_bienvenida(config: dict) -> str:
    """Saludo del /start — incluye directo la explicación de los niveles (gratis + pagos) y el
    pago, para no depender de un botón "Información" aparte que solo agregaba un tap extra."""
    marca = config["marca"]
    nombre = " ".join(filter(None, [marca.get("nombre"), marca.get("emoji")]))
    texto = f"{marca.get('saludo', '')}\n\nSoy el bot de *{nombre}*. {marca.get('descripcion', '')}"

    # Sin repetir el nombre del canal acá: el botón de abajo ya lo dice, así que cada línea va
    # directo al beneficio en vez de duplicar el nombre en el texto.
    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    for canal in ofertas:
        if canal.get("descripcion"):
            texto += f"\n\n🏷️ *{canal['descripcion']}*"

    for canal in config.get("canales_pagos") or []:
        if not canal.get("visible", True):
            continue
        if canal.get("descripcion"):
            texto += f"\n\n*{canal['descripcion']}*"
            for nombre_canal in canal.get("canales_incluidos") or []:
                texto += f"\n• {nombre_canal}"
            if canal.get("nota"):
                texto += f"\n_{canal['nota']}_"

    return texto


# ------------------------------ comandos ------------------------------


async def _mostrar(
    update: Update, context: ContextTypes.DEFAULT_TYPE, texto: str, teclado: InlineKeyboardMarkup | None
) -> None:
    """Transforma el mensaje del flujo de menú/suscripción en vez de acumular mensajes nuevos.

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
    # deep link `/start sus_<canal_id>` (ej. https://t.me/bot?start=sus_test2): dispara el flujo
    # de suscripción de un canal directo, sin pasar por el menú — así se puede probar un canal con
    # "visible": false sin exponerlo como botón a usuarios reales.
    if context.args and context.args[0].startswith("sus_"):
        canal_id = context.args[0].removeprefix("sus_")
        await _iniciar_suscripcion(update, context, canal_id, config)
        return
    await _mostrar(update, context, texto_bienvenida(config), teclado_inicio(config))


async def cb_volver_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Vuelve al menú principal y limpia cualquier estado de flujo en curso (email pendiente de
    escribir o de confirmar, canal en curso) — mismo callback_data se reusa como "Cancelar" en
    esos flujos."""
    query = update.callback_query
    await query.answer()
    context.user_data["esperando_email_vip"] = False
    context.user_data.pop("email_vip_pendiente", None)
    context.user_data.pop("canal_id_pendiente", None)
    config = cargar_config()
    await _mostrar(update, context, texto_bienvenida(config), teclado_inicio(config))


async def cb_ver_mis_canales(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los canales a los que el usuario tiene acceso ahora mismo: el gratis siempre (es
    público, todos tienen acceso) y, si tiene alguna suscripción paga activa, un botón por cada
    chat privado que esa suscripción desbloquea (ver CANAL_CHAT_ID). El invite link de cada chat
    privado se genera al vuelo en cada click en vez de guardarse — mismo mecanismo que
    pagos/telegram_client.py::invitar() (duplicado a propósito, mismo criterio de servicios
    autocontenidos que ya aplica CANAL_CHAT_ID), porque el chat exige creates_join_request y un
    link viejo igual necesita que cb_solicitud_union apruebe contra la base de datos. Esto le
    sirve al usuario para recuperar el acceso si perdió el link original, lo dejó expirar, o
    salió del canal por error."""
    query = update.callback_query
    await query.answer()
    config = cargar_config()
    telegram_user_id = update.effective_user.id

    ofertas = [c for c in config["canales"] if c.get("tipo", "oferta") == "oferta"]
    filas = [[InlineKeyboardButton(c["nombre"], url=c["url"])] for c in ofertas if c.get("url")]

    activos = await db.canales_activos(context.bot_data["db_pool"], telegram_user_id)
    for canal_id in activos:
        for chat_id, etiqueta in CANAL_CHAT_ID.get(canal_id, []):
            try:
                invite = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
                )
            except TelegramError:
                log.exception(
                    "No se pudo generar invite link para %s (canal %s, chat %s)",
                    telegram_user_id, canal_id, chat_id,
                )
                continue
            filas.append([InlineKeyboardButton(etiqueta, url=invite.invite_link)])

    filas.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")])
    texto = "📡 Estos son los canales a los que tienes acceso:"
    await _mostrar(update, context, texto, InlineKeyboardMarkup(filas))


async def _iniciar_suscripcion(
    update: Update, context: ContextTypes.DEFAULT_TYPE, canal_id: str, config: dict
) -> None:
    """Primer paso del flujo de suscripción para un canal dado: MercadoPago exige el email del
    pagador para crear la preapproval (no alcanza con el user_id de Telegram). Si ya hay un email
    guardado de una suscripción anterior, se salta directo a confirmarlo; si no, se pide acá y se
    procesa en el próximo mensaje de texto (ver mensaje_no_reconocido, que revisa ese estado
    primero)."""
    canal_cfg = _canales_pagos(config).get(canal_id)
    if not canal_cfg:
        log.warning("Intento de suscripción a un canal_id desconocido: %r", canal_id)
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]])
        await _mostrar(update, context, "Ese canal ya no está disponible.", teclado)
        return

    context.user_data["canal_id_pendiente"] = canal_id
    intro = f"👑 *{canal_cfg['descripcion']}*\n\n" if canal_cfg.get("descripcion") else ""

    # Si ya está activo, cortamos acá — dejarlo seguir podría terminar en una segunda preapproval
    # authorized en paralelo (doble cobro real), no solo una pendiente duplicada.
    telegram_user_id = update.effective_user.id
    if await db.esta_activo(context.bot_data["db_pool"], telegram_user_id, canal_id):
        texto = f"Ya tienes tu suscripción a {canal_cfg['nombre']} activa ✅"
        acceso_hasta = await db.obtener_acceso_hasta(context.bot_data["db_pool"], telegram_user_id, canal_id)
        if acceso_hasta:
            fecha_local = acceso_hasta.astimezone(TZ_CHILE).strftime("%d/%m/%Y")
            texto += f"\n\nTienes acceso hasta el {fecha_local}."
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]])
        await _mostrar(update, context, texto, teclado)
        return

    # Si ya se suscribió antes con éxito, se saltea pedir el email de nuevo — se reusa la misma
    # pantalla de confirmación que el flujo normal (cb_confirmar_email_vip/cb_reescribir_email_vip).
    email_conocido = await db.obtener_email(context.bot_data["db_pool"], telegram_user_id, canal_id)
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
            update, context, f"{intro}¿Sigues usando este correo: {email_seguro}?", teclado
        )
        return

    context.user_data["esperando_email_vip"] = True
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
    await _mostrar(
        update,
        context,
        f"{intro}El pago es 100% seguro y puedes cancelar cuando quieras. Tienes 2 alternativas:\n"
        "1. con tu cuenta de MercadoPago,\n"
        "2. directo con tarjeta, sin necesitar cuenta.\n\n"
        "Para empezar, dime tu email 👇\n\n"
        "_Tip: el correo solo tiene que coincidir con tu cuenta de MercadoPago si eliges pagar "
        "por esa vía. Si pagas con tarjeta, cualquier correo válido sirve._",
        teclado,
    )


async def cb_suscribirme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Punto de entrada desde un botón del menú o desde el DM de "Renovar suscripción"
    (pagos/telegram_client.py::expulsar) — el canal_id viaja en el callback_data."""
    query = update.callback_query
    await query.answer()
    canal_id = query.data.removeprefix("suscribirme_")
    config = cargar_config()
    await _iniciar_suscripcion(update, context, canal_id, config)


async def _crear_preapproval(
    telegram_user_id: int, email: str, canal_cfg: dict, context: ContextTypes.DEFAULT_TYPE
) -> str | None:
    """Crea la preapproval en MercadoPago para el canal indicado y devuelve el init_point, o None
    si falló (ya logueado)."""
    try:
        # Sin preapproval_plan_id a propósito: esa variante exige card_token_id (tarjeta
        # tokenizada de antemano, pensado para un checkout propio) y no genera un init_point
        # para redirigir — la que sí lo genera es esta, "sin plan asociado" con status=pending,
        # confirmado contra la documentación oficial tras un 400 real (card_token_id is required).
        resultado = context.bot_data["mp_sdk"].preapproval().create({
            "reason": f"Suscripción {canal_cfg.get('nombre_corto', canal_cfg['nombre'])} — Descuentos Bigolito",
            # el email va acá además de en payer_email porque preapproval.get("payer_email") nunca
            # viene poblado en la respuesta real de la API de MercadoPago (ver
            # pagos/logica.py::aplicar_estado_preapproval) — es la única forma de que el webhook,
            # que corre en otro proceso/servicio, recupere el email que el usuario escribió acá.
            "external_reference": f"{telegram_user_id}:{canal_cfg['canal_id']}:{email}",
            "payer_email": email,
            "auto_recurring": {
                "frequency": canal_cfg["frecuencia"],
                "frequency_type": canal_cfg["frecuencia_tipo"],
                "transaction_amount": canal_cfg["monto"],
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


def _firmar_link_tarjeta(
    telegram_user_id: int, email: str, canal_cfg: dict, context: ContextTypes.DEFAULT_TYPE,
) -> str:
    """Arma el link a pagar-tarjeta.html firmado con LINK_PAGO_SECRET, para que
    pagos/pagos_tarjeta.py pueda confiar en telegram_user_id/canal_id/email — sin esto, cualquiera
    podría cambiar esos parámetros a mano en la URL, porque el navegador no tiene ninguna sesión
    autenticada de Telegram. monto/nombre_canal viajan sin firmar (solo para mostrar en pantalla,
    el backend siempre resuelve el monto real desde config.json, ver pagos/pagos_tarjeta.py)."""
    canal_id = canal_cfg["canal_id"]
    exp = int(time.time()) + _VENTANA_LINK_TARJETA_SEGUNDOS
    payload = f"{telegram_user_id}:{canal_id}:{email}:{exp}"
    secreto = context.bot_data["link_pago_secret"]
    firma = hmac.new(secreto.encode(), payload.encode(), hashlib.sha256).hexdigest()
    query = urlencode({
        "telegram_user_id": telegram_user_id,
        "canal_id": canal_id,
        "email": email,
        "exp": exp,
        "sig": firma,
        "monto": canal_cfg["monto"],
        "nombre_canal": canal_cfg["nombre"],
    })
    return f"{URL_PAGO_TARJETA}?{query}"


async def _avisar_pago_pendiente_vencido(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edita el mensaje del intento de pago anterior (si lo hay) para avisar que ese link ya no
    sirve — solo prolijidad visual, la preapproval de atrás ya se cancela aparte
    (_cancelar_preapprovals_anteriores). Solo recuerda el intento inmediatamente anterior de esta
    misma sesión del bot; si no lo encuentra o falló editarlo (mensaje borrado, >48hs, etc.) no
    pasa nada, no es crítico."""
    msg_id = context.user_data.pop("pago_pendiente_msg_id", None)
    chat_id = context.user_data.pop("pago_pendiente_chat_id", None)
    if not msg_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text="Este link de pago ya no es válido — generamos uno nuevo más abajo 👇",
        )
    except Exception:
        pass


async def _cancelar_preapprovals_anteriores(
    telegram_user_id: int, canal_id: str, email: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancela cualquier preapproval anterior de este usuario en este canal que MercadoPago
    todavía no dejó de cobrar:
    - 'pending': un intento anterior sin completar, para no acumular duplicadas cada vez que el
      usuario reintenta (bug reproducido en vivo: 6 preapprovals para el mismo usuario en una
      sola tanda de pruebas).
    - 'authorized': una suscripción vieja que dejó de renovarse bien (ej. tarjeta rechazada, el
      usuario fue expulsado por pagos/reconciliacion.py y ahora se resuscribe a mano acá) pero
      que MercadoPago nunca canceló del todo — si no se cierra explícitamente, sigue reintentando
      cobrarla en paralelo con la nueva. Nunca se llega hasta acá con una suscripción realmente
      vigente: _iniciar_suscripcion ya cortó antes si esta_activo() daba True, así que cualquier
      'authorized' que aparezca en esta búsqueda es necesariamente una vieja huérfana.

    No se toca 'cancelled'/'paused' (ya no cobran) ni preapprovals de otro canal. La API de
    MercadoPago no permite buscar por external_reference/status directo (confirmado contra
    mercadopago/resources/preapproval.py y la doc de /preapproval/search) — se busca por
    payer_email y se filtra acá por el prefijo "{telegram_user_id}:{canal_id}" del
    external_reference, no por el valor completo: acepta tanto el formato viejo de 2 partes como
    el actual de 3 (con el email, ver _crear_preapproval), y no depende de que el email de una
    suscripción anterior coincida carácter a carácter con el que se está usando ahora. Best-effort:
    un fallo acá no debe impedir que el usuario cree su preapproval nueva."""
    prefijo_esperado = f"{telegram_user_id}:{canal_id}"
    try:
        resultado = context.bot_data["mp_sdk"].preapproval().search({"payer_email": email})
        resultado.raise_for_status()
    except Exception:
        log.exception("Falló la búsqueda de preapprovals anteriores para %s", telegram_user_id)
        return

    for item in resultado["response"].get("results", []):
        if item.get("status") not in ("pending", "authorized"):
            continue
        referencia = item.get("external_reference") or ""
        if referencia != prefijo_esperado and not referencia.startswith(prefijo_esperado + ":"):
            continue
        try:
            context.bot_data["mp_sdk"].preapproval().update(item["id"], {"status": "cancelled"})
        except Exception:
            log.exception("Falló la cancelación de la preapproval anterior %s", item["id"])


def teclado_error_preapproval(canal_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Reintentar", callback_data=f"suscribirme_{canal_id}")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")],
    ])


async def _procesar_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Valida el formato del email y pide confirmación antes de generar el cobro — es el único
    punto del bot donde el usuario escribe texto libre, así que confirmar antes de llamar a
    MercadoPago evita que un typo mande el link de pago a un email equivocado."""
    email = (update.message.text or "").strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
        await _mostrar(update, context, "Ese email no es válido 🤔 Prueba escribirlo de nuevo:", teclado)
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
        update, context, f"¿Este es tu correo: {email_seguro}?", teclado
    )


async def cb_confirmar_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    canal_id = context.user_data.get("canal_id_pendiente")
    # .pop() en vez de .get(): un doble clic no encuentra email la segunda vez, evitando
    # generar dos preapprovals para el mismo pago.
    email = context.user_data.pop("email_vip_pendiente", None)
    config = cargar_config()
    canal_cfg = _canales_pagos(config).get(canal_id) if canal_id else None
    if not email or not canal_cfg:
        teclado = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]])
        await _mostrar(update, context, "Esta confirmación ya venció. Vuelve a tocar el botón para empezar de nuevo:", teclado)
        return

    telegram_user_id = update.effective_user.id
    await db.actualizar_email(context.bot_data["db_pool"], telegram_user_id, canal_id, email)
    await _avisar_pago_pendiente_vencido(context)
    await _cancelar_preapprovals_anteriores(telegram_user_id, canal_id, email, context)
    init_point = await _crear_preapproval(telegram_user_id, email, canal_cfg, context)
    if not init_point:
        await _mostrar(
            update, context, "No pudimos generar el link de pago. Prueba de nuevo:",
            teclado_error_preapproval(canal_id),
        )
        return

    link_tarjeta = _firmar_link_tarjeta(telegram_user_id, email, canal_cfg, context)
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 MercadoPago", url=init_point)],
        [InlineKeyboardButton("💳 Pagar con tarjeta", url=link_tarjeta)],
        [InlineKeyboardButton("✏️ Usar otro correo", callback_data="reescribir_email_vip")],
        [InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")],
    ])
    await _mostrar(
        update, context,
        f"Listo 🙌 Elige cómo pagar tu {canal_cfg['nombre']}:\n\n"
        "💳 *MercadoPago*\n"
        "💳 *Pagar con tarjeta*\n\n"
        "(Si elegiste MercadoPago y tu pago fue rechazado, toca \"Usar otro correo\".)",
        teclado,
    )
    context.user_data["pago_pendiente_msg_id"] = query.message.message_id
    context.user_data["pago_pendiente_chat_id"] = update.effective_chat.id


async def cb_reescribir_email_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("email_vip_pendiente", None)
    context.user_data["esperando_email_vip"] = True
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="volver_menu")]])
    await _mostrar(
        update, context,
        "Escribe tu email de nuevo:",
        teclado,
    )


async def mensaje_no_reconocido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("esperando_email_vip"):
        await _procesar_email_vip(update, context)
        return
    teclado = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al menú", callback_data="volver_menu")]])
    await _mostrar(update, context, "No entendí ese mensaje 🤔", teclado)


async def cb_solicitud_union(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """El link de invitación a un canal pago (pagos/telegram_client.py::invitar) ya no deja entrar
    directo — pide aprobación a propósito, porque un link "de un solo uso" no verifica quién lo
    usa (si el suscriptor lo reenvía antes de entrar, cualquiera podría ocupar ese cupo). Acá se
    aprueba o rechaza comparando la identidad real de quien pide entrar contra su suscripción —
    primero hay que ubicar a qué canal_id corresponde el chat de la solicitud."""
    solicitud = update.chat_join_request
    chat_id = str(solicitud.chat.id)
    canal_id = next(
        (cid for cid, chats in CANAL_CHAT_ID.items() if chat_id in {c for c, _ in chats}), None,
    )
    if canal_id is None:
        log.warning("Solicitud de unión a un chat_id no configurado: %s", chat_id)
        await solicitud.decline()
        return

    config = cargar_config()
    canal_cfg_rechazo = _canales_pagos(config).get(canal_id, {})
    nombre_canal = canal_cfg_rechazo.get("nombre_corto") or canal_cfg_rechazo.get("nombre", "el canal")

    activo = await db.esta_activo(context.bot_data["db_pool"], solicitud.from_user.id, canal_id)
    if activo:
        await solicitud.approve()
        log.info("Solicitud de unión aprobada: %s (canal %s)", solicitud.from_user.id, canal_id)

        # Debounce: un canal_id con varios chats (ver CANAL_CHAT_ID) genera una solicitud por
        # chat, y el usuario suele tocar todos los links casi al mismo tiempo — sin esto le
        # llegaría el mismo DM una vez por chat aprobado.
        avisos = context.user_data.setdefault("ultimo_aviso_aprobacion", {})
        ahora = time.monotonic()
        ultimo_aviso = avisos.get(canal_id)
        if ultimo_aviso is not None and ahora - ultimo_aviso < VENTANA_DEBOUNCE_APROBACION_SEGUNDOS:
            return
        avisos[canal_id] = ahora

        try:
            # Telegram no siempre lleva al usuario al canal solo tras aprobar una solicitud de
            # unión — sin este aviso, un usuario común se queda esperando sin saber que ya puede
            # entrar. Mismo criterio que invitar(): si el DM falla (nunca le escribió al bot), se
            # loguea sin cortar el flujo — la aprobación en sí ya se hizo.
            await context.bot.send_message(
                chat_id=solicitud.from_user.id,
                text=(
                    "✅ ¡Listo! Tu solicitud fue aprobada. Si Telegram no te abrió el canal solo, "
                    "toca de nuevo el link que te mandamos."
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
        log.info(
            "Solicitud de unión rechazada (sin suscripción activa): %s (canal %s)",
            solicitud.from_user.id, canal_id,
        )
        try:
            await context.bot.send_message(
                chat_id=solicitud.from_user.id,
                text=(
                    "No pudimos darte acceso: no encontramos una suscripción activa a tu nombre. "
                    f"Si ya pagaste y crees que es un error, escríbenos. Si no, únete a {nombre_canal} aquí:"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"👑 Suscribirme a {nombre_canal}", callback_data=f"suscribirme_{canal_id}")]]
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
    if not mp_access_token:
        raise SystemExit("Falta MERCADOPAGO_ACCESS_TOKEN en bot/.env")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Falta DATABASE_URL en bot/.env")

    link_pago_secret = os.environ.get("LINK_PAGO_SECRET")
    if not link_pago_secret:
        raise SystemExit("Falta LINK_PAGO_SECRET en bot/.env")

    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["database_url"] = database_url
    app.bot_data["mp_sdk"] = mercadopago.SDK(mp_access_token)
    app.bot_data["link_pago_secret"] = link_pago_secret

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_suscribirme, pattern="^suscribirme_"))
    app.add_handler(CallbackQueryHandler(cb_confirmar_email_vip, pattern="^confirmar_email_vip$"))
    app.add_handler(CallbackQueryHandler(cb_reescribir_email_vip, pattern="^reescribir_email_vip$"))
    app.add_handler(CallbackQueryHandler(cb_volver_menu, pattern="^volver_menu$"))
    app.add_handler(CallbackQueryHandler(cb_ver_mis_canales, pattern="^ver_mis_canales$"))
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
