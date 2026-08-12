"""Orquestador: una corrida completa del scraper de ofertas, multi-tienda (ver config.TIENDAS).

1. Clona el repo (necesita GITHUB_TOKEN).
2. Corre el scraper de cada tienda de forma independiente — si una falla, no bloquea a las
   demás (cada una tiene su propio archivo de estado, ver config.Tienda.ruta_estado).
3. Escribe el estado de cada tienda + el feed combinado web/data/ofertas.json.
4. Publica en Telegram las ofertas nuevas/con récord nuevo, de todas las tiendas juntas.
5. Commitea y pushea — Cloudflare Pages redeploya la web al detectar el push.
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
import descubrimiento  # noqa: E402
import git_publish  # noqa: E402
import monitoreo_tiendas  # noqa: E402
import ofertas_writer  # noqa: E402
import telegram_publisher  # noqa: E402
from fuentes.falabella.listado import obtener_ofertas_listado  # noqa: E402
from fuentes.falabella.producto import obtener_ofertas_productos_seguidos  # noqa: E402
from fuentes.paris.listado import obtener_ofertas_paris  # noqa: E402
from fuentes.ripley.listado import obtener_ofertas_ripley  # noqa: E402
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
            resultado = descubrimiento.elegir_url_ofertas(candidatas_nuevas)

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
                git_publish.publicar_cambios(
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
    detectados, categorias_ok = await obtener_ofertas_ripley(sesion)
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


# id de config.Tienda -> función que corre esa tienda. Se resuelve acá (no en config.py) para
# evitar un import circular: fuentes/* ya hace `import config`.
_RUNNERS = {
    "fal": _correr_falabella,
    "xiaomi": _correr_xiaomi,
    "ripley": _correr_ripley,
    "paris": _correr_paris,
}


async def _correr() -> None:
    if not config.GITHUB_TOKEN:
        raise SystemExit("Falta GITHUB_TOKEN en el entorno")
    if not config.GITHUB_REPO:
        raise SystemExit("Falta GITHUB_REPO en el entorno")

    await asyncio.to_thread(_diagnostico_headless)

    repo_dir = git_publish.clonar_repo()

    detectados_por_tienda: dict[config.Tienda, list[dict]] = {}
    async with FetcherSession(impersonate=config.USER_AGENT_IMPERSONATE, timeout=config.HTTP_TIMEOUT_SEGUNDOS) as sesion:
        for tienda in config.TIENDAS:
            runner = _RUNNERS[tienda.id]
            detectados, ok = await runner(sesion, repo_dir)
            if ok:
                detectados_por_tienda[tienda] = detectados

            if tienda.id != "fal":  # Falabella ya tiene su propia alerta (ver descubrimiento)
                fallos = monitoreo_tiendas.registrar_resultado(repo_dir, tienda.id, ok)
                if fallos and fallos % config.FALLOS_TIENDA_ANTES_DE_ALERTA == 0:
                    await telegram_publisher.avisar_admin(
                        f"{tienda.nombre} lleva {fallos} corridas fallando seguidas. Requiere revisión manual."
                    )

    ofertas_por_tienda: dict[config.Tienda, list[dict]] = {
        tienda: ofertas_writer.procesar(repo_dir, detectados, tienda)
        for tienda, detectados in detectados_por_tienda.items()
    }

    todas_candidatas = [oferta for ofertas in ofertas_por_tienda.values() for oferta in ofertas]
    # mezcla tiendas/productos/porcentajes al azar antes de postear — si no, se nota el orden de
    # scrapeo (una tienda entera, categoría por categoría, antes de pasar a la siguiente). No
    # afecta nada aguas abajo: ofertas_por_tienda (usado para confirmar publicaciones más abajo)
    # es una lista aparte, sin mezclar.
    random.shuffle(todas_candidatas)
    ids_confirmados = await telegram_publisher.publicar_ofertas_nuevas(todas_candidatas)
    for tienda, ofertas in ofertas_por_tienda.items():
        ofertas_writer.confirmar_publicaciones(repo_dir, ofertas, ids_confirmados, tienda)

    ofertas_writer.construir_feed_web(repo_dir, config.TIENDAS)

    marca_tiempo = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rutas_a_publicar = [config.RUTA_OFERTAS_JSON] + [tienda.ruta_estado for tienda in detectados_por_tienda]
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


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
