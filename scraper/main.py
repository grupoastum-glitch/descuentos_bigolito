"""Orquestador: una corrida completa del scraper de ofertas, multi-tienda (ver config.TIENDAS).

1. Clona el repo (necesita GITHUB_TOKEN) y conecta a Postgres (necesita DATABASE_URL).
2. Corre el scraper de cada tienda de forma independiente — si una falla, no bloquea a las
   demás. Apenas cada una termina, se escribe su estado en Postgres (tabla `productos` — ver
   scraper/db.py), sin esperar a las demás tiendas — si la corrida se corta a mitad de camino,
   lo de las tiendas que ya terminaron no se pierde.
3. Encola en Postgres (tabla `cola_publicacion`, ver scraper/db.py) las ofertas nuevas/con récord
   nuevo de todas las tiendas juntas (mezcladas al azar) — NO las publica acá. El envío real a
   Telegram lo hace scraper/publicar.py, un proceso aparte que corre 24/7 (no cron), para que un
   volumen grande de ofertas por publicar (limitado por el ritmo de Telegram, no por nosotros) no
   bloquee el advisory lock de la próxima corrida horaria.
4. Escribe el feed combinado web/data/ofertas.json y lo commitea/pushea — Cloudflare Pages
   redeploya la web al detectar el push.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from scrapling.fetchers import FetcherSession, StealthySession

load_dotenv(Path(__file__).resolve().parent / ".env")

import config  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import db  # noqa: E402
import descubrimiento  # noqa: E402
import git_publish  # noqa: E402
import monitoreo_tiendas  # noqa: E402
import ofertas_writer  # noqa: E402
import run_lock  # noqa: E402
import telegram_publisher  # noqa: E402
from fuentes.abc.listado import obtener_ofertas_abc  # noqa: E402
from fuentes.bestmart.listado import obtener_ofertas_bestmart  # noqa: E402
from fuentes.clubdeperrosygatos.listado import obtener_ofertas_clubdeperrosygatos  # noqa: E402
from fuentes.decathlon.listado import obtener_ofertas_decathlon  # noqa: E402
from fuentes.dust2.listado import obtener_ofertas_dust2  # noqa: E402
from fuentes.easy.listado import obtener_ofertas_easy  # noqa: E402
from fuentes.falabella.listado import obtener_ofertas_listado  # noqa: E402
from fuentes.falabella.producto import obtener_ofertas_productos_seguidos  # noqa: E402
from fuentes.geekz.listado import obtener_ofertas_geekz  # noqa: E402
from fuentes.gympro.listado import obtener_ofertas_gympro  # noqa: E402
from fuentes.hites.listado import obtener_ofertas_hites  # noqa: E402
from fuentes.hm.listado import obtener_ofertas_hm  # noqa: E402
from fuentes.luffytoys.listado import obtener_ofertas_luffytoys  # noqa: E402
from fuentes.myshop.listado import obtener_ofertas_myshop  # noqa: E402
from fuentes.novapet.listado import obtener_ofertas_novapet  # noqa: E402
from fuentes.paris.listado import obtener_ofertas_paris  # noqa: E402
from fuentes.pcexpress.listado import obtener_ofertas_pcexpress  # noqa: E402
from fuentes.pcfactory.listado import obtener_ofertas_pcfactory  # noqa: E402
from fuentes.pethome.listado import obtener_ofertas_pethome  # noqa: E402
from fuentes.petsonline.listado import obtener_ofertas_petsonline  # noqa: E402
from fuentes.ripley.listado import obtener_ofertas_ripley  # noqa: E402
from fuentes.sipoonline.listado import obtener_ofertas_sipoonline  # noqa: E402
from fuentes.sodimac.listado import obtener_ofertas_sodimac  # noqa: E402
from fuentes.sparta.listado import obtener_ofertas_sparta  # noqa: E402
from fuentes.spdigital.listado import obtener_ofertas_spdigital  # noqa: E402
from fuentes.superzoo.listado import obtener_ofertas_superzoo  # noqa: E402
from fuentes.weplay.listado import obtener_ofertas_weplay  # noqa: E402
from fuentes.xiaomi.listado import obtener_ofertas_xiaomi  # noqa: E402

CLAVE_DESCUBRIMIENTO_FALABELLA = "falabella_ofertas"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scraper.main")


def _cargar_productos_seguidos(repo_dir: Path) -> list[dict]:
    ruta = repo_dir / config.RUTA_PRODUCTOS_SEGUIDOS
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo).get("productos", [])
    except (OSError, json.JSONDecodeError):
        log.warning("No se pudo leer productos_seguidos.json, se sigue sin tracking de precio")
        return []


_DIAGNOSTICO_HEADLESS_URL = "https://www.mi.com/cl/product/xiaomi-su7-1-18-die-cast-model-car/buy/"
_DIAGNOSTICO_HEADLESS_SELECTOR = ".information-section__product-price .mi-price strong"
_DIAGNOSTICO_HEADLESS_PRECIO_ESPERADO = "69.990"

# WePlay y Ripley (fuentes/weplay|ripley/listado.py) no corren su sesión headless
# (AsyncStealthySession) al mismo tiempo — comparten este lock. El contenedor de Railway
# (2 vCPU/1GB) se satura de memoria con dos Chromium concurrentes (incidencia 2026-08-15:
# Page.goto nunca completaba una navegación, la corrida entera quedó colgada esperando).
_LOCK_HEADLESS = asyncio.Lock()


def _diagnostico_headless() -> None:
    """Chequeo de una sola vez: ¿corre Chromium headless en este contenedor? No participa de
    la lógica real (no toca estado/feed/Telegram) — solo deja evidencia en el log para decidir
    si vale la pena construir la verificación real de precios de Xiaomi contra el navegador."""
    t0 = time.monotonic()
    try:
        with StealthySession(
            headless=True, network_idle=False, wait_selector=_DIAGNOSTICO_HEADLESS_SELECTOR, max_pages=1,
        ) as sesion:
            pagina = sesion.fetch(_DIAGNOSTICO_HEADLESS_URL)
        html = pagina.body if isinstance(pagina.body, str) else pagina.body.decode("utf-8", errors="replace")
        encontrado = _DIAGNOSTICO_HEADLESS_PRECIO_ESPERADO in html
        log.info(
            "Diagnóstico headless: status=%s tiempo=%.2fs precio_encontrado=%s",
            pagina.status, time.monotonic() - t0, encontrado,
        )
    except Exception:
        log.exception("Diagnóstico headless: falló tras %.2fs", time.monotonic() - t0)


async def _calentar_sesion(sesion: FetcherSession) -> str:
    """GET a la home antes de pedir el listado, para que cualquier cookie de tienda/región
    que Falabella setee ahí viaje también en la request al listado. Devuelve el HTML de la
    home, reusado por el fallback de descubrimiento si hace falta (evita pedirla dos veces)."""
    respuesta = await sesion.get(config.FALABELLA_HOME_URL)
    log.info("Warm-up: %s -> %s (status %s)", config.FALABELLA_HOME_URL, respuesta.url, respuesta.status)
    return respuesta.body.decode("utf-8", errors="replace")


async def _correr_falabella(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    """Devuelve (items_detectados, ok). ok=False si esta tienda no consiguió nada esta corrida
    (una tienda fallando no debe impedir que las demás se procesen y publiquen igual)."""
    productos_seguidos = _cargar_productos_seguidos(repo_dir)
    cache_rutas = descubrimiento.cargar_cache(repo_dir)
    url_listado = cache_rutas.get(CLAVE_DESCUBRIMIENTO_FALABELLA, {}).get("url") or config.FALABELLA_OFERTAS_URL

    home_html = await _calentar_sesion(sesion)
    detectados, paginas_ok = await obtener_ofertas_listado(sesion, url_listado)

    if paginas_ok == 0:
        log.error(
            "El listado de ofertas falló en las %s páginas de esta corrida (%s) — probable "
            "bloqueo/redirect o cambio de estructura. Se intenta descubrimiento automático.",
            config.PAGINAS_LISTADO, url_listado,
        )
        ya_descartadas = descubrimiento.urls_descartadas(repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA)

        recuperada = None
        for url_vieja in ya_descartadas:
            detectados, paginas_ok = await obtener_ofertas_listado(sesion, url_vieja)
            if paginas_ok > 0:
                recuperada = url_vieja
                break

        fallos_consecutivos = 0
        if recuperada:
            log.info(
                "Descubrimiento: la URL descartada %s volvió a funcionar, se usa directo sin "
                "consultar al LLM",
                recuperada,
            )
            descubrimiento.guardar_cache(
                repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA,
                descubrimiento.ResultadoDescubrimiento(
                    url=recuperada, confianza="alta",
                    razonamiento="URL descartada previamente volvió a funcionar (reintento directo)",
                ),
            )
            descubrimiento.limpiar_historial_fallidos(repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA)
        else:
            candidatas = await descubrimiento.mapear_candidatas(sesion, config.FALABELLA_HOME_URL, home_html)
            candidatas_nuevas = [url for url in candidatas if url not in ya_descartadas]
            if not candidatas_nuevas:
                log.error(
                    "Descubrimiento: las %s candidatas encontradas ya se habían descartado en "
                    "corridas anteriores, no queda nada nuevo para probar",
                    len(candidatas),
                )
            resultado = await asyncio.to_thread(descubrimiento.elegir_url_ofertas, candidatas_nuevas)

            if resultado and resultado.confianza == "alta":
                log.info(
                    "Descubrimiento eligió %s (confianza %s): %s",
                    resultado.url, resultado.confianza, resultado.razonamiento,
                )
                detectados, paginas_ok = await obtener_ofertas_listado(sesion, resultado.url)
                if paginas_ok > 0:
                    descubrimiento.guardar_cache(repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA, resultado)
                    descubrimiento.limpiar_historial_fallidos(repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA)
                else:
                    log.error("La URL descubierta (%s) tampoco funcionó, se aborta la corrida", resultado.url)
                    fallos_consecutivos = descubrimiento.registrar_intento_fallido(
                        repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA, [resultado.url]
                    )
            else:
                log.error(
                    "Descubrimiento no encontró una URL con confianza suficiente (resultado=%s), "
                    "se aborta la corrida",
                    resultado,
                )
                urls_a_descartar = [resultado.url] if resultado else []
                fallos_consecutivos = descubrimiento.registrar_intento_fallido(
                    repo_dir, CLAVE_DESCUBRIMIENTO_FALABELLA, urls_a_descartar
                )

        if paginas_ok == 0:
            # el registro de fallos recién escrito solo vive en este clon temporal del
            # repo — hay que publicarlo ahora, aparte, porque el resto de la corrida
            # de Falabella termina acá y nunca llega al commit final de más abajo.
            if (repo_dir / config.RUTA_DESCUBRIMIENTO_FALLIDOS).exists():
                await asyncio.to_thread(
                    git_publish.publicar_cambios,
                    repo_dir,
                    rutas=[config.RUTA_DESCUBRIMIENTO_FALLIDOS],
                    mensaje="chore(descubrimiento): registro de intento fallido",
                )
            if fallos_consecutivos and fallos_consecutivos % config.FALLOS_DESCUBRIMIENTO_ANTES_DE_ALERTA == 0:
                await telegram_publisher.avisar_admin(
                    f"El descubrimiento automático de la URL de ofertas de Falabella lleva "
                    f"{fallos_consecutivos} corridas fallando seguidas. Requiere revisión manual."
                )
            log.error(
                "Falabella: se descarta esta corrida sin tocar su estado ni el feed, "
                "para no vaciar el feed en producción."
            )
            return [], False

    detectados += await obtener_ofertas_productos_seguidos(sesion, productos_seguidos)
    return detectados, True


async def _correr_xiaomi(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_xiaomi(sesion)
    if categorias_ok == 0:
        log.error("Xiaomi: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_ripley(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    # sesion (FetcherSession compartida) no se usa acá: obtener_ofertas_ripley arma su propia
    # sesión headless internamente (ver fuentes/ripley/listado.py) — se mantiene en la firma
    # para no romper la llamada genérica runner(sesion, repo_dir) de _procesar_tienda.
    detectados, categorias_ok = await obtener_ofertas_ripley(_LOCK_HEADLESS)
    if categorias_ok == 0:
        log.error("Ripley: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_paris(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, ok = await obtener_ofertas_paris(sesion)
    if ok == 0:
        log.error("Paris: se descarta esta corrida sin tocar su estado, no se pudo leer el listado.")
        return [], False
    return detectados, True


async def _correr_hites(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_hites(sesion)
    if categorias_ok == 0:
        log.error("Hites: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_abc(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_abc(sesion)
    if categorias_ok == 0:
        log.error("ABC: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_hm(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, departamentos_ok = await obtener_ofertas_hm(sesion)
    if departamentos_ok == 0:
        log.error("H&M: se descarta esta corrida sin tocar su estado, no se leyó ningún departamento.")
        return [], False
    return detectados, True


async def _correr_luffytoys(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_luffytoys(sesion)
    if paginas_ok == 0:
        log.error("LuffyToys: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_geekz(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_geekz(sesion)
    if paginas_ok == 0:
        log.error("Geekz: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_weplay(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    # sesion (FetcherSession compartida) no se usa acá: obtener_ofertas_weplay arma su propia
    # sesión headless internamente (ver fuentes/weplay/listado.py) — se mantiene en la firma
    # para no romper la llamada genérica runner(sesion, repo_dir) de _procesar_tienda.
    detectados, categorias_ok = await obtener_ofertas_weplay(_LOCK_HEADLESS)
    if categorias_ok == 0:
        log.error("WePlay: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_easy(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_easy(sesion)
    if paginas_ok == 0:
        log.error("Easy: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_sodimac(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_sodimac(sesion)
    if categorias_ok == 0:
        log.error("Sodimac: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_bestmart(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_bestmart(sesion)
    if paginas_ok == 0:
        log.error("Bestmart: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_spdigital(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_spdigital(sesion)
    if categorias_ok == 0:
        log.error("SPDigital: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_pcfactory(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_pcfactory(sesion)
    if categorias_ok == 0:
        log.error("PCFactory: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_dust2(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_dust2(sesion)
    if paginas_ok == 0:
        log.error("Dust2.gg: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_myshop(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, fuentes_ok = await obtener_ofertas_myshop(sesion)
    if fuentes_ok == 0:
        log.error("MyShop: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_pcexpress(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, fuentes_ok = await obtener_ofertas_pcexpress(sesion)
    if fuentes_ok == 0:
        log.error("PC Express: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_sipoonline(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_sipoonline(sesion)
    if categorias_ok == 0:
        log.error("Sipoonline: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_superzoo(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, categorias_ok = await obtener_ofertas_superzoo(sesion)
    if categorias_ok == 0:
        log.error("SuperZoo: se descarta esta corrida sin tocar su estado, no se leyó ninguna categoría.")
        return [], False
    return detectados, True


async def _correr_clubdeperrosygatos(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_clubdeperrosygatos(sesion)
    if paginas_ok == 0:
        log.error("Club de Perros y Gatos: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_pethome(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_pethome(sesion)
    if paginas_ok == 0:
        log.error("PetHome: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_novapet(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_novapet(sesion)
    if paginas_ok == 0:
        log.error("NovaPet: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_petsonline(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_petsonline(sesion)
    if paginas_ok == 0:
        log.error("PetsOnline: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_decathlon(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_decathlon(sesion)
    if paginas_ok == 0:
        log.error("Decathlon: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_sparta(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_sparta(sesion)
    if paginas_ok == 0:
        log.error("Sparta: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


async def _correr_gympro(sesion: FetcherSession, repo_dir: Path) -> tuple[list[dict], bool]:
    detectados, paginas_ok = await obtener_ofertas_gympro(sesion)
    if paginas_ok == 0:
        log.error("GymPro: se descarta esta corrida sin tocar su estado, no se leyó ninguna página.")
        return [], False
    return detectados, True


# id de config.Tienda -> función que corre esa tienda. Se resuelve acá (no en config.py) para
# evitar un import circular: fuentes/* ya hace `import config`.
_RUNNERS = {
    "fal": _correr_falabella,
    "xiaomi": _correr_xiaomi,
    "ripley": _correr_ripley,
    "paris": _correr_paris,
    "hites": _correr_hites,
    "abc": _correr_abc,
    "hm": _correr_hm,
    "luffytoys": _correr_luffytoys,
    "geekz": _correr_geekz,
    "weplay": _correr_weplay,
    "easy": _correr_easy,
    "sodimac": _correr_sodimac,
    "bestmart": _correr_bestmart,
    "spdigital": _correr_spdigital,
    "pcfactory": _correr_pcfactory,
    "dust2": _correr_dust2,
    "myshop": _correr_myshop,
    "pcexpress": _correr_pcexpress,
    "sipoonline": _correr_sipoonline,
    "superzoo": _correr_superzoo,
    "clubdeperrosygatos": _correr_clubdeperrosygatos,
    "pethome": _correr_pethome,
    "novapet": _correr_novapet,
    "petsonline": _correr_petsonline,
    "decathlon": _correr_decathlon,
    "sparta": _correr_sparta,
    "gympro": _correr_gympro,
}


async def _correr() -> None:
    if not config.GITHUB_TOKEN:
        raise SystemExit("Falta GITHUB_TOKEN en el entorno")
    if not config.GITHUB_REPO:
        raise SystemExit("Falta GITHUB_REPO en el entorno")
    if not config.DATABASE_URL:
        raise SystemExit("Falta DATABASE_URL en el entorno")

    await asyncio.to_thread(_diagnostico_headless)

    tiendas = config.tiendas_activas()
    if not tiendas:
        raise SystemExit("SCRAPER_TIENDAS no matchea ninguna tienda de config.TIENDAS — revisar el valor")
    log.info(
        "Tiendas activas en esta instancia (%s): %s",
        config.SCRAPER_NOMBRE or "principal", ", ".join(t.nombre for t in tiendas),
    )

    repo_dir = git_publish.clonar_repo()
    pool = await db.conectar()

    con_lock = await run_lock.adquirir_lock(pool)
    if con_lock is None:
        log.warning("Corrida abortada: ya hay otra corrida activa (ver log de run_lock arriba).")
        await db.cerrar()
        return

    try:
        # Las tiendas se scrapean todas en paralelo (asyncio.gather más abajo), pero cada una
        # sigue escribiendo su resultado a Postgres (procesar()) apenas ELLA termina, sin
        # esperar a las demás — mismo motivo que antes de paralelizar: si la corrida se corta a
        # mitad de camino (crash, redeploy), el estado de las tiendas que sí alcanzaron a
        # terminar ya quedó guardado, en vez de perderse. El envío a Telegram sigue siendo al
        # final, con todas las tiendas mezcladas (ver shuffle más abajo), para que no se note el
        # orden de scrapeo.
        async def _procesar_tienda(sesion, tienda) -> list[dict]:
            runner = _RUNNERS[tienda.id]
            try:
                detectados, ok = await asyncio.wait_for(
                    runner(sesion, repo_dir), timeout=config.TIMEOUT_TIENDA_SEGUNDOS
                )
            except asyncio.TimeoutError:
                log.error(
                    "%s: no terminó en %ss, se descarta esta corrida (posible cuelgue)",
                    tienda.nombre, config.TIMEOUT_TIENDA_SEGUNDOS,
                )
                detectados, ok = [], False
            candidatas: list[dict] = []
            if ok:
                candidatas = await ofertas_writer.procesar(pool, detectados, tienda)

            if tienda.id != "fal":  # Falabella ya tiene su propia alerta (ver descubrimiento)
                fallos = monitoreo_tiendas.registrar_resultado(repo_dir, tienda.id, ok)
                if fallos and fallos % config.FALLOS_TIENDA_ANTES_DE_ALERTA == 0:
                    await telegram_publisher.avisar_admin(
                        f"{tienda.nombre} lleva {fallos} corridas fallando seguidas. Requiere revisión manual."
                    )
            return candidatas

        todas_candidatas: list[dict] = []
        async with FetcherSession(impersonate=config.USER_AGENT_IMPERSONATE, timeout=config.HTTP_TIMEOUT_SEGUNDOS) as sesion:
            resultados = await asyncio.gather(*(
                _procesar_tienda(sesion, tienda) for tienda in tiendas
            ))
            for candidatas in resultados:
                todas_candidatas.extend(candidatas)

        # descuentos así de extremos suelen ser errores de precio del comercio — se postean primero
        # (no se mezclan con el resto) y se avisa aparte al admin para que pueda verificar/comprar
        # rápido antes de que el comercio lo corrija.
        extremas = [o for o in todas_candidatas if o["descuento_pct"] >= config.UMBRAL_DESCUENTO_EXTREMO]
        resto = [o for o in todas_candidatas if o["descuento_pct"] < config.UMBRAL_DESCUENTO_EXTREMO]
        for oferta in extremas:
            await telegram_publisher.avisar_admin(
                f"🚨 Posible error de precio: {oferta['titulo']} — {oferta['descuento_pct']}% off "
                f"en {oferta['comercio']}. {oferta['url']}"
            )

        # mezcla tiendas/productos/porcentajes al azar antes de postear — si no, se nota el orden de
        # scrapeo (una tienda entera, categoría por categoría, antes de pasar a la siguiente).
        random.shuffle(resto)
        todas_candidatas = extremas + resto

        # ya no se publica acá directo (podía tardar horas con mucho volumen y, mientras tanto,
        # bloqueaba el advisory lock para la próxima corrida horaria) — se encola en Postgres y
        # scraper/publicar.py (proceso aparte, 24/7) es quien la drena a su propio ritmo.
        await db.encolar_publicaciones(pool, todas_candidatas)

        await ofertas_writer.construir_feed_web(repo_dir, pool)

        marca_tiempo = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rutas_a_publicar = [config.RUTA_OFERTAS_JSON]
        if (repo_dir / config.RUTA_RUTAS_DESCUBIERTAS).exists():
            rutas_a_publicar.append(config.RUTA_RUTAS_DESCUBIERTAS)
        if (repo_dir / config.RUTA_FALLOS_TIENDAS).exists():
            rutas_a_publicar.append(config.RUTA_FALLOS_TIENDAS)
        if (repo_dir / config.RUTA_DESCUBRIMIENTO_FALLIDOS).exists():
            rutas_a_publicar.append(config.RUTA_DESCUBRIMIENTO_FALLIDOS)
        git_publish.publicar_cambios(
            repo_dir,
            rutas=rutas_a_publicar,
            mensaje=f"chore(ofertas): actualización automática {marca_tiempo}",
        )
    finally:
        await run_lock.liberar_lock(con_lock, pool)
        await db.cerrar()


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
