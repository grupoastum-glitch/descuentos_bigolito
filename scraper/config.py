"""Configuración y constantes del scraper de ofertas."""
from __future__ import annotations

import os
from dataclasses import dataclass

# --- Fuente: Falabella Chile ---
FALABELLA_HOME_URL = "https://www.falabella.com/falabella-cl"
FALABELLA_OFERTAS_URL = "https://www.falabella.com/falabella-cl/page/descuentos"
# ^ /collection/ofertas fue eliminado por Falabella (redirige a home, ver sesión 2026-08-10).
# Esta es la página actual, encontrada a mano navegando el menú Ofertas del sitio — no está
# linkeada en el HTML crudo de la home (la arma un menú con JS), por eso el fallback de
# descubrimiento nunca la iba a encontrar solo. Existe también /page/oportunidades-unicas
# (ofertas CMR, precio con tarjeta propia de Falabella) — descartada a propósito: esos precios
# no aplican a cualquier comprador. Si esto también deja de andar, no asumir que el
# descubrimiento automático la va a encontrar de nuevo — puede hacer falta repetir esto a mano.
# Es una página CMS curada (hub de links a categorías con descuento), no un listado directo —
# ver scraper/fuentes/falabella/listado.py, modo hub.
PAGINAS_LISTADO = 5  # ~240 productos revisados por corrida, en modo directo
PAGINAS_POR_CATEGORIA_DESCUENTOS = 1  # modo hub: páginas por cada categoría descubierta (~48-60 productos c/u)
MAX_CATEGORIAS_DESCUENTOS = 60  # modo hub: techo defensivo de requests/tiempo (hoy el hub linkea ~47)
USER_AGENT_IMPERSONATE = "chrome"  # perfil de impersonación TLS/headers (curl_cffi)

# cuántas páginas/categorías se piden en simultáneo (modo hub y modo directo). Cada "carril"
# sigue respetando DELAY_MIN/MAX_SEGUNDOS entre sus propias requests — esto no elimina la pausa
# anti-detección, solo corre varios carriles en paralelo. Valor conservador a propósito (sube el
# ritmo de requests a Falabella ~Nx); subirlo requiere confirmar en un run real que no aumente la
# tasa de categorías fallidas/redirects.
CONCURRENCIA_LISTADO = 3

DELAY_MIN_SEGUNDOS = 2.0
DELAY_MAX_SEGUNDOS = 5.0
# pausa antes de reintentar el único fetch cuya falla aborta toda la tienda (árbol de
# categorías de Ripley/Xiaomi, ver fuentes/<tienda>/listado.py::_fetch_con_reintento) —
# constante separada de DELAY_MIN/MAX_SEGUNDOS porque es un propósito distinto (dar tiempo a
# que un bloqueo transitorio se libere, no espaciar requests para evitar detección)
REINTENTO_FETCH_INICIAL_SEGUNDOS = 5
HTTP_TIMEOUT_SEGUNDOS = 30  # por request a Falabella — sin esto, un request colgado cuelga todo el job
GIT_TIMEOUT_SEGUNDOS = 60  # por comando git (clone/push) — mismo motivo

# --- Clasificación de ofertas ---
DESCUENTO_MINIMO_WEB_PCT = 20  # piso para aparecer en el feed de la web
# descuentos así de altos suelen ser errores de precio del comercio (más que ofertas reales) —
# se postean primero (antes que el resto de la corrida, ver main.py) y se avisa aparte al admin
# por si hay que verificar/comprar rápido antes de que el comercio lo corrija.
UMBRAL_DESCUENTO_EXTREMO = 90
# (mínimo %, nombre de canal) — de mayor a menor: canal_para_descuento() devuelve el primer
# tramo que matchea, así que un descuento de 60%+ cae en "ofertas_vip" y nunca llega al de 40%
# — cada tramo es exclusivo de su canal, sin duplicar posteos. Sumar un tramo nuevo es agregar
# una línea acá (en la posición correcta según su mínimo) más su @username en
# CANAL_TELEGRAM_USERNAME, sin tocar el resto del código.
TIERS_DESCUENTO = [
    (60, "ofertas_vip"),
    (40, "ofertas_40"),
]

# cada cuántas horas se le da otra chance a un producto que sigue siendo récord (precio mínimo o
# mayor descuento) pero no cambió desde la última vez que se publicó — evita que ofertas buenas
# queden "enterradas" para suscriptores nuevos. Sin tope de publicaciones por día: un producto
# que se mantiene como récord puede repostearse varias veces el mismo día, una por cada ventana
# de este tamaño que pase.
HORAS_REPUBLICACION_REGLA3 = 6

# @username de Telegram por canal (el bot debe ser admin ahí con permiso "Publicar mensajes").
# Sus claves son, a la vez, los únicos canales que reciben posteo automático — sumar/sacar una
# entrada acá activa/desactiva el posteo en ese canal, sin ningún otro cambio de código.
CANAL_TELEGRAM_USERNAME = {
    "ofertas_40": "descuentos_bigolito",
    "ofertas_vip": "super_descuentos_bigolito",
}


def canal_para_descuento(pct: int) -> str | None:
    for minimo, canal in TIERS_DESCUENTO:
        if pct >= minimo:
            return canal
    return None


# --- Descubrimiento de URLs (fallback cuando el listado conocido deja de funcionar) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL_DESCUBRIMIENTO = "claude-haiku-4-5-20251001"
RUTA_RUTAS_DESCUBIERTAS = "scraper/rutas_descubiertas.json"
RUTA_DESCUBRIMIENTO_FALLIDOS = "scraper/descubrimiento_fallidos.json"
# cada cuántos fallos consecutivos de descubrimiento se re-avisa por Telegram (para no
# mandar una alerta en cada corrida mientras el problema sigue sin resolverse)
FALLOS_DESCUBRIMIENTO_ANTES_DE_ALERTA = 3

# --- Monitoreo de tiendas sin descubrimiento de URL propio (Xiaomi/Ripley/Paris) ---
RUTA_FALLOS_TIENDAS = "scraper/fallos_tiendas.json"
# mismo umbral que FALLOS_DESCUBRIMIENTO_ANTES_DE_ALERTA, constante separada porque son
# mecanismos independientes (ver monitoreo_tiendas.py)
FALLOS_TIENDA_ANTES_DE_ALERTA = 3

# --- Publicación en git ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GIT_AUTHOR_NAME = "ofertas-bot"
GIT_AUTHOR_EMAIL = "bot@localhost"
GIT_CLONE_DIR = os.environ.get("SCRAPER_CLONE_DIR", "/tmp/ofertas-repo")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# chat_id (privado o de un grupo/canal admin) donde avisar problemas operativos, ej. el
# descubrimiento de URL fallando varias corridas seguidas. Vacío = no se manda nada, solo log.
TELEGRAM_ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
TELEGRAM_DELAY_SEGUNDOS = 3  # espacio entre dos posteos al MISMO canal (ver
# telegram_publisher.publicar_ofertas_nuevas) — el límite de flood control de Telegram es por
# chat, no global, así que posteos a canales distintos no esperan entre sí. Mayor al mínimo
# real necesario para flood control: elegido a propósito para que una corrida con muchas
# ofertas nuevas (ej. Paris) no se sienta como spam en el canal.
TELEGRAM_REINTENTOS_MAX = 2  # reintentos si Telegram igual devuelve RetryAfter

# --- Rutas dentro del repo clonado ---
RUTA_OFERTAS_JSON = "web/data/ofertas.json"
RUTA_PRODUCTOS_SEGUIDOS = "scraper/productos_seguidos.json"


@dataclass(frozen=True)
class Tienda:
    id: str  # prefijo de clave en el estado — ver TIENDAS, no cambiar el de Falabella
    nombre: str  # para el feed web y el hashtag del caption de Telegram
    ruta_estado: str


# El id de Falabella es "fal" (no "falabella") a propósito: ofertas_writer ya arma las claves del
# estado como f"fal_{producto_id}" desde antes de que existiera este framework — cambiar el
# prefijo reindexaría los ~2841 productos ya trackeados y el scraper los trataría como nunca
# vistos, disparando una republicación masiva (mismo riesgo que ya se evitó con la migración de
# historial y el rename del archivo de estado). Xiaomi no tiene ese problema, su id puede ser
# legible desde el día uno.
TIENDAS = [
    Tienda(id="fal", nombre="Falabella", ruta_estado="scraper/estado_precios_falabella.json"),
    Tienda(id="xiaomi", nombre="Xiaomi", ruta_estado="scraper/estado_precios_xiaomi.json"),
    Tienda(id="ripley", nombre="Ripley", ruta_estado="scraper/estado_precios_ripley.json"),
    Tienda(id="paris", nombre="Paris", ruta_estado="scraper/estado_precios_paris.json"),
]


# A propósito bajo (no los 30 que web/js/data.js soportaría como máximo): la web es la
# puerta de entrada a Telegram, no un reemplazo de los canales. Mostrar solo una muestra
# chica es lo que le da sentido a unirse al canal para ver el resto.
MAX_OFERTAS_WEB_TEASER = 8
