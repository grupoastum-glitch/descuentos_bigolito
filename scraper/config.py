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
MAX_PAGINAS_POR_CATEGORIA_DESCUENTOS = 10  # modo hub: techo de seguridad por categoría. Medido en
# sesión 2026-08-14 sobre las 47 categorías reales del hub: 25 de 47 (más de la mitad) siguen
# completamente llenas incluso a la página 6, así que un número fijo chico (el 1 de antes) dejaba
# afuera la mayor parte del catálogo en esas categorías grandes. 10 es un techo moderado.
PAGINAS_ALEATORIAS_POR_CATEGORIA = 4  # de esas hasta 10 páginas, la página 1 siempre se pide (es
# el ancla: la vista más reciente/relevante, y confirma si la categoría tiene contenido) más esta
# cantidad elegida al azar entre las páginas 2..MAX_PAGINAS_POR_CATEGORIA_DESCUENTOS. Con 25 de 47
# categorías llenas incluso a 6 páginas, pedir siempre las 10 completas en más de la mitad de las
# categorías dispararía el tiempo/volumen de requests por corrida. Con este muestreo, cada
# corrida pide como máximo 5 páginas por categoría (1 + 4) en vez de hasta 10, y distintas
# corridas van cubriendo distintas páginas del mismo rango con el tiempo.
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
TIMEOUT_TIENDA_SEGUNDOS = 1200  # 20 min — techo por tienda en main.py::_procesar_tienda; ninguna
# tienda individual debería tardar tanto en condiciones normales, pero un cuelgue real (ej.
# Chromium sin completar una navegación en el contenedor, ver incidencia 2026-08-15) no debe
# bloquear ni la publicación de las tiendas que sí terminaron ni el advisory lock de la próxima
# corrida del cron.

# --- Clasificación de ofertas ---
DESCUENTO_MINIMO_WEB_PCT = 20  # piso para aparecer en el feed de la web
# descuentos así de altos suelen ser errores de precio del comercio (más que ofertas reales) —
# se postean primero (antes que el resto de la corrida, ver main.py) y se avisa aparte al admin
# por si hay que verificar/comprar rápido antes de que el comercio lo corrija.
UMBRAL_DESCUENTO_EXTREMO = 90

# piso para el canal cross-tienda "ofertas_top" (todas las tiendas del proyecto, sin importar su
# canal habitual) — a diferencia de los demás tramos, esta oferta se publica DUPLICADA: además de
# su canal normal, también cae acá si supera este piso (ver ofertas_writer.procesar()). El
# objetivo explícito es cazar errores de precio reales que el consumidor pueda aprovechar, no solo
# "ofertas grandes" — decisión 2026-08-22: no se filtran precios sospechosos (ej. $1, precio
# "normal" tipo placeholder $999.999), es justamente el tipo de hallazgo que se busca acá.
UMBRAL_DESCUENTO_TOP = 80
# (mínimo %, nombre de canal) — de mayor a menor: canal_para_descuento() devuelve el primer
# tramo que matchea, así que un descuento de 50%+ cae en "ofertas_vip" y nunca llega al de 25%
# — cada tramo es exclusivo de su canal, sin duplicar posteos. Sumar un tramo nuevo es agregar
# una línea acá (en la posición correcta según su mínimo) más su @username en
# CANAL_TELEGRAM_USERNAME, sin tocar el resto del código.
TIERS_DESCUENTO = [
    (50, "ofertas_vip"),
    (25, "ofertas_40"),
]

# tiendas de la categoría "Geek/Coleccionables/Gaming" (ver Tiendas/paginas.md) — postean a un
# canal propio (CANAL_TELEGRAM_USERNAME["ofertas_geek"]) en vez de a ofertas_40/ofertas_vip,
# siempre que superen UMBRAL_DESCUENTO_GEEK, sin importar su descuento_pct. Sumar la próxima
# tienda geek (WePlay/Irion/Geekz) es agregar su Tienda.id acá, sin tocar el resto del código.
TIENDAS_GEEK = {"luffytoys", "geekz", "weplay"}
UMBRAL_DESCUENTO_GEEK = 20  # mismo piso que DESCUENTO_MINIMO_WEB_PCT — decisión 2026-08-15

# tiendas de la categoría "Deco Hogar" (ferretería/mejoramiento del hogar, ver Tiendas/paginas.md)
# — mismo mecanismo que TIENDAS_GEEK: postean exclusivas a
# CANAL_TELEGRAM_USERNAME["ofertas_deco_hogar"] si superan UMBRAL_DESCUENTO_DECO_HOGAR.
TIENDAS_DECO_HOGAR = {"easy", "sodimac"}
UMBRAL_DESCUENTO_DECO_HOGAR = 25  # subido de 20 a 25 — decisión 2026-08-17, para mitigar el
# volumen de candidatas por corrida (ver sesión de optimización de cron/publicación)

# tiendas de la categoría "Tech" (tecnología/gaming, ver Tiendas/paginas.md) — mismo mecanismo
# que TIENDAS_GEEK/TIENDAS_DECO_HOGAR: postean exclusivas a
# CANAL_TELEGRAM_USERNAME["ofertas_tech"] si superan UMBRAL_DESCUENTO_TECH.
TIENDAS_TECH = {"bestmart", "spdigital", "pcfactory", "xiaomi", "dust2", "myshop", "pcexpress", "sipoonline"}  # Xiaomi sumada 2026-08-17 —
# antes posteaba a los tramos genéricos ofertas_40/ofertas_vip, ahora exclusiva a ofertas_tech
UMBRAL_DESCUENTO_TECH = 25  # subido de 20 a 25 — decisión 2026-08-17, mismo motivo que deco hogar

# tiendas de la categoría "Mascotas" (ver Tiendas/paginas.md) — mismo mecanismo que
# TIENDAS_GEEK/TIENDAS_DECO_HOGAR/TIENDAS_TECH: postean exclusivas a
# CANAL_TELEGRAM_USERNAME["ofertas_mascotas"] si superan UMBRAL_DESCUENTO_MASCOTAS.
TIENDAS_MASCOTAS = {"superzoo", "clubdeperrosygatos", "pethome", "novapet", "petsonline"}
UMBRAL_DESCUENTO_MASCOTAS = 20  # bajado de 25 a 20 el 2026-08-22: con solo SuperZoo en el canal
# (densidad de descuento real ~4.7% de productos, más baja que en Tech) el piso de 25% dejaba muy
# poco margen de candidatas nuevas por corrida — una vez publicadas, el canal dependía casi
# enteramente de la Regla 3 (repost cada 6h) hasta la próxima oferta genuina. Decisión del usuario
# para sostener un flujo más constante de publicaciones.

# tiendas de la categoría "Fitness" (deportes/actividad física en general, ver Tiendas/paginas.md)
# — mismo mecanismo que TIENDAS_GEEK/TIENDAS_DECO_HOGAR/TIENDAS_TECH/TIENDAS_MASCOTAS: postean
# exclusivas a CANAL_TELEGRAM_USERNAME["ofertas_fitness"] si superan UMBRAL_DESCUENTO_FITNESS.
TIENDAS_FITNESS = {"decathlon", "sparta", "gympro"}
UMBRAL_DESCUENTO_FITNESS = 25  # mismo piso que Tech/Mascotas — decisión 2026-08-22

# cada cuántas horas se le da otra chance a un producto que sigue siendo récord (precio mínimo o
# mayor descuento) pero no cambió desde la última vez que se publicó — evita que ofertas buenas
# queden "enterradas" para suscriptores nuevos. Sin tope de publicaciones por día: un producto
# que se mantiene como récord puede repostearse varias veces el mismo día, una por cada ventana
# de este tamaño que pase.
HORAS_REPUBLICACION_REGLA3 = 6

# techo de candidatas de Regla 3 (re-publicación, no récord nuevo) que se admiten por tienda en
# una misma corrida — sin esto, el pool de productos "récord eterno" crece sin límite a medida
# que se trackean más productos/tiendas, y en una corrida puede generar más volumen del que
# Telegram puede drenar y del que conviene guardar en cola_publicacion (ver Postgres, sesión
# 2026-08-20: la tabla llegó a 115MB/106k filas). Regla 1/2 (récords genuinos) nunca se cortan.
# Elegido con datos reales de una corrida: Falabella 2663 y Sodimac 1201 candidatas de Regla 3
# (las únicas dos que superan este número hoy); el resto de las tiendas no lo toca. Las
# candidatas que exceden el tope no se pierden, quedan disponibles en la próxima corrida.
MAX_REGLA3_POR_TIENDA_POR_CORRIDA = 400

# Destino de Telegram por canal (el bot debe ser admin ahí con permiso "Publicar mensajes"):
# @username para un canal público, o chat_id numérico (siempre negativo, ej. "-1004438197572")
# para uno privado — canal_para_descuento() no distingue, telegram_publisher.py sí, al armar el
# chat_id final. Sus claves son, a la vez, los únicos canales que reciben posteo automático —
# sumar/sacar una entrada acá activa/desactiva el posteo en ese canal, sin ningún otro cambio de
# código.
CANAL_TELEGRAM_USERNAME = {
    "ofertas_40": "descuentos_bigolito",
    "ofertas_vip": "-1004438197572",
    "ofertas_geek": "-1003952570153",
    "ofertas_deco_hogar": "-1003961858440",
    "ofertas_tech": "-1004332754687",
    "ofertas_mascotas": "-1004304511002",
    "ofertas_fitness": "-1004357028199",
    "ofertas_top": "-1004484005134",
}

# tienda_id -> (canal, umbral) para las categorías especiales (geek, deco hogar, ...) — cada una
# postea exclusiva a su canal propio (no también a ofertas_40/ofertas_vip) si supera su umbral.
# Construido desde TIENDAS_GEEK/TIENDAS_DECO_HOGAR para no repetir un `if tienda_id in ...` por
# cada categoría nueva — se generalizó a este mapeo recién con la segunda categoría real
# (deco hogar), no antes.
_CANAL_ESPECIAL_POR_TIENDA = {
    **{tid: ("ofertas_geek", UMBRAL_DESCUENTO_GEEK) for tid in TIENDAS_GEEK},
    **{tid: ("ofertas_deco_hogar", UMBRAL_DESCUENTO_DECO_HOGAR) for tid in TIENDAS_DECO_HOGAR},
    **{tid: ("ofertas_tech", UMBRAL_DESCUENTO_TECH) for tid in TIENDAS_TECH},
    **{tid: ("ofertas_mascotas", UMBRAL_DESCUENTO_MASCOTAS) for tid in TIENDAS_MASCOTAS},
    **{tid: ("ofertas_fitness", UMBRAL_DESCUENTO_FITNESS) for tid in TIENDAS_FITNESS},
}


def canal_para_descuento(pct: int) -> str | None:
    for minimo, canal in TIERS_DESCUENTO:
        if pct >= minimo:
            return canal
    return None


def canal_para_oferta(tienda_id: str, pct: int) -> str | None:
    """Como canal_para_descuento(), pero primero chequea si la tienda pertenece a una categoría
    especial (_CANAL_ESPECIAL_POR_TIENDA) — esas van exclusivas a su canal propio (no también a
    ofertas_40/ofertas_vip) si superan su umbral. El resto de las tiendas cae al criterio de
    siempre, sin cambios."""
    especial = _CANAL_ESPECIAL_POR_TIENDA.get(tienda_id)
    if especial:
        canal, umbral = especial
        return canal if pct >= umbral else None
    return canal_para_descuento(pct)


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

# --- Base de datos (estado de productos/historial de precios y lock entre corridas — ver
# scraper/db.py y scraper/run_lock.py; reemplaza los estado_precios_<tienda>.json y el
# run_lock.json de antes, ambos vivían commiteados a git) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Identidad de esta instancia de scraper (namespacing del lock entre corridas concurrentes de
# distintas instancias — ver run_lock.py). Vacío = instancia única de hoy, sin cambio de
# comportamiento. Setear un nombre distinto por instancia recién hace falta el día que corra más
# de un scraper en paralelo (ej. uno separado para tiendas geek).
SCRAPER_NOMBRE = os.environ.get("SCRAPER_NOMBRE", "")
SCRAPER_TIENDAS = os.environ.get("SCRAPER_TIENDAS", "")  # ids separados por coma (ver TIENDAS
# abajo), ej. "luffytoys,geekz,weplay". Vacío = todas (instancia única de hoy).

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

# --- Afiliados (Soicos) ---
# Plantilla de deeplink por tienda (nombre igual al de Tienda.nombre en TIENDAS). Vacío = se
# publica la URL original sin cambios. Completar tienda por tienda a medida que Soicos aprueba
# cada programa y entrega el formato real de link. "{url}" se reemplaza por la URL del producto
# url-encodeada.
SOICOS_DEEPLINK_TEMPLATES: dict[str, str] = {
    # aid=56144 es el ID de afiliado, fijo para toda la cuenta de Soicos — pid es el ID del
    # programa, uno distinto por tienda (confirmado en vivo con el "Generador de Deeplinks" de
    # Soicos: pid=15773 = "Sodimac (CL)").
    "Sodimac": "https://ad.soicos.com/sclick?aid=56144&pid=15773&dl={url}",
}


@dataclass(frozen=True)
class Tienda:
    id: str  # prefijo de clave del producto (f"{id}_{producto_id}") y tienda_id en la tabla
             # `productos` de Postgres — ver TIENDAS, no cambiar el de Falabella
    nombre: str  # para el feed web y el hashtag del caption de Telegram


# El id de Falabella es "fal" (no "falabella") a propósito: ofertas_writer ya arma las claves del
# estado como f"fal_{producto_id}" desde antes de que existiera este framework — cambiar el
# prefijo reindexaría los ~2841 productos ya trackeados en la tabla `productos` y el scraper los
# trataría como nunca vistos, disparando una republicación masiva (mismo riesgo que ya se evitó
# con la migración de historial y el rename del archivo de estado, cuando el estado todavía era
# JSON). Xiaomi no tiene ese problema, su id puede ser legible desde el día uno.
TIENDAS = [
    Tienda(id="fal", nombre="Falabella"),
    Tienda(id="xiaomi", nombre="Xiaomi"),
    Tienda(id="ripley", nombre="Ripley"),
    Tienda(id="paris", nombre="Paris"),
    Tienda(id="hites", nombre="Hites"),
    Tienda(id="abc", nombre="ABC"),
    Tienda(id="hm", nombre="H&M"),
    Tienda(id="luffytoys", nombre="LuffyToys"),
    Tienda(id="geekz", nombre="Geekz"),
    Tienda(id="weplay", nombre="WePlay"),
    Tienda(id="easy", nombre="Easy"),
    Tienda(id="sodimac", nombre="Sodimac"),
    Tienda(id="bestmart", nombre="Bestmart"),
    Tienda(id="spdigital", nombre="SPDigital"),
    Tienda(id="pcfactory", nombre="PCFactory"),
    Tienda(id="dust2", nombre="Dust2.gg"),
    Tienda(id="myshop", nombre="MyShop"),
    Tienda(id="pcexpress", nombre="PC Express"),
    Tienda(id="sipoonline", nombre="Sipoonline"),
    Tienda(id="superzoo", nombre="SuperZoo"),
    Tienda(id="clubdeperrosygatos", nombre="Club de Perros y Gatos"),
    Tienda(id="pethome", nombre="PetHome"),
    Tienda(id="novapet", nombre="NovaPet"),
    Tienda(id="petsonline", nombre="PetsOnline"),
    Tienda(id="decathlon", nombre="Decathlon"),
    Tienda(id="sparta", nombre="Sparta"),
    Tienda(id="gympro", nombre="GymPro"),
]


def tiendas_activas() -> list[Tienda]:
    """Subconjunto de TIENDAS que corre esta instancia — ver SCRAPER_TIENDAS. Vacío = todas
    (comportamiento de instancia única)."""
    if not SCRAPER_TIENDAS:
        return TIENDAS
    ids = {id_.strip() for id_ in SCRAPER_TIENDAS.split(",") if id_.strip()}
    return [t for t in TIENDAS if t.id in ids]


# A propósito bajo (no los 30 que web/js/data.js soportaría como máximo): la web es la
# puerta de entrada a Telegram, no un reemplazo de los canales. Mostrar solo una muestra
# chica es lo que le da sentido a unirse al canal para ver el resto.
MAX_OFERTAS_WEB_TEASER = 8
MAX_OFERTAS_VIP_WEB_TEASER = 2  # de las 8 totales, cuántas como máximo pueden ser del tramo VIP
# (50%+) — evita que ofertas VIP, que suelen salir en tandas, le ganen el lugar a las gratis en
# el teaser de la web. Ver feed_activo() en db.py.
