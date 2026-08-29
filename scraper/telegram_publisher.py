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
from typing import Awaitable, Callable

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError, TimedOut

import config
import dias_semana

log = logging.getLogger("scraper.telegram_publisher")

MAX_EVENTOS_HISTORIAL_EN_CAPTION = 3  # techo de legibilidad — Telegram limita el caption de una
# foto a 1024 caracteres, pero acá el límite real es no saturar el mensaje con eventos

_IMAGEN_TIMEOUT_SEGUNDOS = 10  # descarga chica (~30-60KB) desde el propio CDN de la tienda, no
# debería tardar — un timeout corto evita que una imagen colgada demore la corrida entera
_IMAGEN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# comercios cuyo CDN de imágenes bloquea al fetcher de Telegram (`send_photo(photo=<url>)` deja
# que Telegram baje la imagen por su cuenta, y ahí falla) — para estos se descarga la imagen acá
# mismo y se suben los bytes en vez de la URL (ver _descargar_imagen). Confirmado para Sodimac:
# media.sodimac.cl responde con cookie __cf_bm (Cloudflare Bot Management), a diferencia de
# media.falabella.com que no la tiene — un request normal (como el nuestro) pasa sin problema,
# pero el fetcher de Telegram queda bloqueado. Confirmado también para Sparta (2026-08-22):
# sparta.cl sirve las imágenes detrás de Varnish/Fastly, que devuelve HTTP 403 ("Error 54113")
# específicamente para el User-Agent real de Telegram (`TelegramBot (like TwitterBot)`), mientras
# que un request normal pasa con 200. Sumado también Hites (2026-08-22): mismo síntoma reportado
# (se publica sin foto), imágenes servidas desde www.hites.com/dw/image/... — mismo dominio que ya
# está detrás de Cloudflare (ver scraper/fuentes/hites/listado.py) — no reproducible con un
# request manual (igual que Sodimac/Sparta al momento de confirmarlos: el bot-management actúa
# sobre el fingerprint/IP real de los servidores de Telegram, no sobre un curl de prueba). ABC
# (sumada 2026-08-24, ver scraper/fuentes/abc/listado.py) sirve las imágenes con el mismo patrón
# exacto que Hites — www.abc.cl/dw/image/... (dominio propio, no un CDN aparte), también detrás de
# Cloudflare, mismo motor Demandware — candidata fuerte a necesitar este mismo fix, pero se decidió
# NO sumarla preventivamente (para no pagar el costo de descarga+reupload en Railway sin necesidad
# confirmada) — se espera confirmar con una corrida real si se publica sin foto antes de agregarla.
# Si otra tienda presenta el mismo síntoma (se publica sin foto pese a que `imagen` es una URL
# válida), sumarla acá.
_COMERCIOS_CON_CDN_BLOQUEADO = {"Sodimac", "Sparta", "Hites"}


async def _descargar_imagen(url: str) -> bytes | None:
    """Baja la imagen nosotros mismos en vez de dejar que Telegram la busque por su cuenta —
    ver _COMERCIOS_CON_CDN_BLOQUEADO. Devuelve None ante cualquier problema (red, status,
    contenido no imagen) para que el caller decida el fallback, igual que ante una URL rota."""
    try:
        async with httpx.AsyncClient(timeout=_IMAGEN_TIMEOUT_SEGUNDOS) as cliente:
            respuesta = await cliente.get(url, headers={"User-Agent": _IMAGEN_USER_AGENT})
        if respuesta.status_code != 200:
            log.warning("Descarga de imagen falló (HTTP %s): %s", respuesta.status_code, url)
            return None
        if not respuesta.headers.get("content-type", "").startswith("image/"):
            log.warning("Descarga de imagen no es una imagen (%s): %s", respuesta.headers.get("content-type"), url)
            return None
        return respuesta.content
    except httpx.HTTPError:
        log.warning("Excepción descargando imagen: %s", url, exc_info=True)
        return None


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
    ]
    if oferta["descuento_pct"] >= config.UMBRAL_DESCUENTO_EXTREMO:
        lineas.append("🚨 DESCUENTO EXTREMO — posible error de precio, aprovecha antes que lo corrijan 🚨")
    lineas += [
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
    if oferta["comercio"] in config.SOICOS_DEEPLINK_TEMPLATES:
        lineas.append(f'🔗 <a href="{url}">VER PRODUCTO</a> 👈 👀')
    else:
        lineas.append("🔗 VER PRODUCTO 👀")
        lineas.append(url)

    # backstop defensivo — el recorte de eventos de arriba ya debería alcanzar, pero un título
    # muy largo podría igual pasarse del límite de Telegram para el caption de una foto (1024,
    # menor al límite de 4096 de un mensaje de texto plano).
    return "\n".join(lineas)[:1024]


_EXTRACTO_DIGEST_MAX = 90  # largo de la línea de descripción por cupón en el digest — con ~28
# cupones totales el mensaje no se acerca al límite real (4096, texto plano), pero mantiene el
# mensaje escaneable (ver formatear_digest_cupones)

_ETIQUETA_COMERCIO_DIGEST = {"Shell": "🐚 Shell", "Copec": "⛽ Copec"}


def _extracto_digest(texto: str | None, maximo: int = _EXTRACTO_DIGEST_MAX) -> str:
    if not texto:
        return ""
    limpio = html.escape(texto.strip())
    return limpio if len(limpio) <= maximo else limpio[: maximo - 1].rstrip() + "…"


def _socio_distinto(cupon: dict) -> bool:
    """True si vale la pena mostrar el socio aparte del título — no si está vacío, es el
    genérico "General"/nombre del comercio, o (caso real: "Mach") es literalmente el mismo
    texto que el título, lo que se vería como "🏦 Mach — Mach"."""
    socio = cupon.get("socio")
    if not socio or socio in ("General", cupon["comercio"]):
        return False
    return socio.strip().casefold() != cupon["titulo"].strip().casefold()


def _linea_cupon_digest(cupon: dict, con_extracto: bool) -> str:
    """Formato completo (título + extracto de descripción opcional) — solo para la sección de
    HOY, la razón de ser del digest. Ver _linea_cupon_compacta para las secciones secundarias
    (todos los días / día no confirmado), que no lo necesitan cada día."""
    titulo = html.escape(cupon["titulo"])
    if _socio_distinto(cupon):
        linea = f"🏦 <b>{html.escape(cupon['socio'])}</b> — {titulo}"
    else:
        linea = f"🏦 <b>{titulo}</b>"
    if con_extracto:
        extracto = _extracto_digest(cupon.get("descripcion"))
        if extracto:
            linea += f"\n{extracto}"
    return linea


def _linea_cupon_compacta(cupon: dict) -> str:
    """Una línea por cupón, sin descripción — para las secciones secundarias del digest (info de
    referencia que se repite todos los días, no hace falta el detalle completo cada vez)."""
    titulo = html.escape(cupon["titulo"])
    if _socio_distinto(cupon):
        return f"• <b>{html.escape(cupon['socio'])}</b> — {titulo}"
    return f"• {titulo}"


def formatear_digest_cupones(grupos: dict | None) -> str | None:
    """Arma el mensaje de texto plano del digest diario de cupones de combustible (ver
    scraper/cupones_writer.py::construir_grupos_digest) — 3 grupos:

    - `grupos["hoy"]`: dict {comercio: [cupones de día específico vigentes hoy]} — formato
      completo (título + extracto), es la razón de ser del digest.
    - `grupos["todos"]`: lista combinada (ambos comercios) de cupones CONFIRMADOS como "todos los
      días" por la fuente — formato compacto (una línea, sin extracto).
    - `grupos["sin_dia"]`: dict {comercio: [cupones sin día detectado]} — mismo formato compacto,
      pero en su propia sección con la incertidumbre explícita en el título ("día no confirmado"):
      tratarlos igual que "todos los días" sería decirle al usuario algo que no sabemos que sea
      cierto (ver cupones_writer.construir_grupos_digest).

    None si no hay nada que mostrar en ningún grupo.

    Backstop de largo en 3 niveles (límite real de un mensaje de texto: 4096, no 1024 como una
    foto) — solo afecta la sección de HOY (las secundarias ya son compactas siempre): con extracto
    de descripción por cupón -> si se pasa de ~3800 caracteres, solo títulos -> backstop final
    [:4096]."""
    if not grupos or not (
        any(grupos["hoy"].values()) or grupos["todos"] or any(grupos["sin_dia"].values())
    ):
        return None

    hoy_capitalizado = dias_semana.nombre_dia_chile().capitalize()
    fecha = datetime.now(config.TZ_CHILE).strftime("%d/%m")

    def _armar(con_extracto_hoy: bool) -> str:
        lineas = [f"⛽🎟️ Cupones combustible de hoy — {hoy_capitalizado} {fecha}"]
        for comercio, cupones in grupos["hoy"].items():
            if not cupones:
                continue
            lineas += ["", f"<b>{_ETIQUETA_COMERCIO_DIGEST.get(comercio, comercio)}</b>"]
            lineas += [_linea_cupon_digest(c, con_extracto_hoy) for c in cupones]
        if grupos["todos"]:
            lineas += ["", "<b>🔁 Todos los días</b>"]
            lineas += [_linea_cupon_compacta(c) for c in grupos["todos"]]
        for comercio, cupones in grupos["sin_dia"].items():
            if not cupones:
                continue
            etiqueta = _ETIQUETA_COMERCIO_DIGEST.get(comercio, comercio)
            lineas += ["", f"<b>{etiqueta} (día no confirmado — revisa vigencia en la app)</b>"]
            lineas += [_linea_cupon_compacta(c) for c in cupones]
        return "\n".join(lineas)

    texto = _armar(con_extracto_hoy=True)
    if len(texto) > 3800:
        texto = _armar(con_extracto_hoy=False)
    return texto[:4096]


async def avisar_admin(mensaje: str) -> bool:
    """Manda un mensaje de texto plano a TELEGRAM_ADMIN_CHAT_ID (avisos operativos, no ofertas).
    Si no está configurado, solo queda en el log — no rompe la corrida. Devuelve True si el envío
    se confirmó (usado por el dedup de avisos de error de precio en main.py)."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_ADMIN_CHAT_ID:
        log.warning("TELEGRAM_ADMIN_CHAT_ID no configurado, no se puede avisar: %s", mensaje)
        return False

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        try:
            await bot.send_message(chat_id=config.TELEGRAM_ADMIN_CHAT_ID, text=f"⚠️ {mensaje}")
            return True
        except TelegramError:
            log.exception("Falló el aviso al admin: %s", mensaje)
            return False


async def _enviar_con_reintento(bot: Bot, chat_id: str, oferta: dict) -> bool:
    """Manda la oferta con foto (bot.send_photo) si hay `imagen` — el preview automático de
    Telegram a partir del link no es confiable para todas las tiendas (Paris, por ejemplo, no
    trae imagen en su preview), así que se manda explícita. Si `send_photo` falla por un
    problema de la imagen en sí (URL rota, formato no soportado, etc. — típicamente
    BadRequest), se cae a texto plano en el mismo intento en vez de perder la oferta; los
    errores transitorios de Telegram (RetryAfter/TimedOut) se dejan propagar para que el loop
    de abajo espere y reintente el envío completo. Devuelve True si se logró publicar (con o
    sin foto), False si se agotaron los reintentos (pérdida definitiva).

    Para los comercios de _COMERCIOS_CON_CDN_BLOQUEADO, la imagen se descarga acá mismo y se
    sube como bytes en vez de pasarle la URL a Telegram (ver _descargar_imagen) — al resto de
    las tiendas no se les toca el comportamiento, siguen mandando la URL directa como siempre."""
    texto = oferta["texto"] if oferta.get("tipo") == "digest_cupones" else _formatear_caption(oferta)
    for intento in range(config.TELEGRAM_REINTENTOS_MAX + 1):
        try:
            imagen = oferta.get("imagen")
            if imagen and oferta.get("comercio") in _COMERCIOS_CON_CDN_BLOQUEADO:
                imagen = await _descargar_imagen(imagen)
                if imagen is None:
                    log.warning(
                        "No se pudo descargar la imagen de %s para %s, fallback a texto: %s",
                        oferta.get("comercio"), chat_id, oferta.get("imagen"),
                    )
            if imagen:
                try:
                    await bot.send_photo(
                        chat_id=chat_id, photo=imagen, caption=texto, parse_mode=ParseMode.HTML,
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


async def _publicar_canal(
    bot: Bot,
    chat_id: str,
    ofertas: list[dict],
    ids_confirmados: set[str],
    on_publicada: Callable[[dict], Awaitable[None]] | None,
) -> None:
    """Publica en orden las ofertas de UN canal, respetando su propio throttle de
    TELEGRAM_DELAY_SEGUNDOS. Pensado para correr en paralelo con los workers de los demás
    canales (ver publicar_ofertas_nuevas) — el límite de flood control de Telegram es por chat,
    no global del bot, así que un canal lento no le roba tiempo de espera a otro."""
    ultimo_posteo: float | None = None
    for oferta in ofertas:
        if ultimo_posteo is not None:
            espera = config.TELEGRAM_DELAY_SEGUNDOS - (time.monotonic() - ultimo_posteo)
            if espera > 0:
                await asyncio.sleep(espera)

        try:
            publicado = await _enviar_con_reintento(bot, chat_id, oferta)
            if publicado:
                log.info("Publicado en %s: %s", chat_id, oferta["titulo"])
                ids_confirmados.add(oferta["id"])
                if on_publicada is not None:
                    try:
                        await on_publicada(oferta)
                    except Exception:
                        # un error transitorio (ej. de la base de datos) no debe cortar el
                        # envío del resto — la peor consecuencia es la misma que existía
                        # antes de este callback (una posible republicación futura), no
                        # perder el envío que ya se hizo.
                        log.exception(
                            "on_publicada falló para %s — el mensaje SÍ se mandó a Telegram, "
                            "el evento de historial no quedó registrado.", oferta["id"],
                        )
            else:
                log.error(
                    "Se agotaron los reintentos por flood control en %s para la oferta %s — "
                    "sigue disponible como candidata, se reintenta en la próxima corrida",
                    chat_id, oferta["id"],
                )
        except TelegramError:
            log.exception("Falló el posteo a %s para la oferta %s", chat_id, oferta["id"])
        ultimo_posteo = time.monotonic()


async def publicar_ofertas_nuevas(
    ofertas: list[dict],
    on_publicada: Callable[[dict], Awaitable[None]] | None = None,
) -> set[str]:
    """Intenta publicar cada oferta en su canal. Por cada una que se manda de verdad, llama
    `on_publicada(oferta)` (si se pasó) para que el caller persista el evento de historial al
    toque — no hace falta esperar a que termine todo el loop (ver ofertas_writer.registrar_evento_publicado).
    Devuelve igual el set completo de `id` confirmados, útil para logging/observabilidad.

    Los canales se publican en paralelo, uno por canal (ver _publicar_canal) — con miles de
    candidatas en una corrida grande, un solo loop secuencial para todos los canales hacía que
    el canal más liviano esperara horas detrás del más pesado (ej. Falabella/Sodimac) aunque el
    límite de flood control de Telegram sea por chat, no global. Agrupar por canal preserva el
    mismo throttle de TELEGRAM_DELAY_SEGUNDOS por canal, solo que ya no se bloquean entre sí."""
    ids_confirmados: set[str] = set()
    if not ofertas:
        return ids_confirmados
    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN no configurado, se omite el posteo a canales")
        return ids_confirmados

    ofertas_por_canal: dict[str, list[dict]] = {}
    for oferta in ofertas:
        valor = config.CANAL_TELEGRAM_USERNAME.get(oferta.get("canal"))
        if not valor:
            # tier sin canal activo todavía (ver config.CANAL_TELEGRAM_USERNAME) — se omite,
            # sigue disponible como candidata en la próxima corrida (no se confirma acá)
            continue
        # canal privado (sin @username, ver config.CANAL_TELEGRAM_USERNAME): la Bot API lo
        # direcciona por su chat_id numérico, siempre negativo — canal público: por @username.
        chat_id = valor if valor.startswith("-") else f"@{valor}"
        ofertas_por_canal.setdefault(chat_id, []).append(oferta)

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    async with bot:
        await asyncio.gather(
            *(
                _publicar_canal(bot, chat_id, ofertas_canal, ids_confirmados, on_publicada)
                for chat_id, ofertas_canal in ofertas_por_canal.items()
            ),
            return_exceptions=True,
        )
    return ids_confirmados
