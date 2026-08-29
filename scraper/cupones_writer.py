"""Decide qué cupones de combustible (Copec/Shell — ver scraper/fuentes/copec|shell/listado.py)
corresponde publicar, y persiste su estado en Postgres (tabla `cupones_combustible`, ver db.py).

Paralelo a ofertas_writer.py, pero deliberadamente más chico: un cupón no tiene "producto" ni
precio, así que no aplican las Reglas 1/2/3 (mínimo histórico de precio). El ciclo de vida acá es:

- Nuevo (id no visto antes) -> publicar.
- Cambio de contenido real (`hash_contenido` distinto del `hash_publicado` con el que se confirmó
  la última publicación) -> publicar de nuevo.
- Recordatorio: sin cambios, pero pasaron >= config.HORAS_REPUBLICACION_CUPON desde la última
  publicación confirmada -> publicar de nuevo (mismo espíritu que la Regla 3 de productos, sin
  lógica de récord histórico).

`hash_contenido` (estado observado, se actualiza SIEMPRE en cada corrida) y `hash_publicado`
(último hash con el que Telegram confirmó el envío, solo lo actualiza registrar_cupon_publicado)
son campos separados a propósito: si se guardaran juntos, una corrida que detecta un cambio pero
falla al publicar (Telegram caído, etc.) dejaría el cambio marcado como "ya publicado" sin haberlo
publicado nunca. Mismo motivo por el que ofertas_writer separa el estado de `productos` del log de
confirmaciones en `historial_precios`.

Como ambas fuentes leen su catálogo completo cada corrida (sin muestreo de categorías/páginas), un
cupón no visto en una corrida exitosa se marca inactivo de inmediato — a diferencia de
GRACIA_INACTIVO_HORAS en productos, acá no hace falta período de gracia."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import asyncpg

import config
import db

log = logging.getLogger("scraper.cupones_writer")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _horas_entre(fecha_iso_desde: str, fecha_iso_hasta: str) -> float:
    desde = datetime.strptime(fecha_iso_desde, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    hasta = datetime.strptime(fecha_iso_hasta, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (hasta - desde).total_seconds() / 3600


def _hash_identidad(tienda_id: str, socio: str, titulo: str) -> str:
    return hashlib.sha1(f"{tienda_id}|{socio}|{titulo}".encode("utf-8")).hexdigest()[:16]


def _hash_contenido(item: dict) -> str:
    partes = "|".join(str(item.get(campo)) for campo in (
        "valor_descuento", "tope_clp", "dia_semana", "vigencia_desde", "vigencia_hasta",
        "codigo", "como_activar", "descripcion",
    ))
    return hashlib.sha1(partes.encode("utf-8")).hexdigest()[:16]


async def procesar_cupones(
    pool: asyncpg.Pool, items_detectados: list[dict], tienda_id: str, comercio: str
) -> list[dict]:
    """Actualiza `cupones_combustible` a partir de los cupones detectados en esta corrida.
    Devuelve las candidatas a publicar en Telegram (ver docstring del módulo)."""
    ahora = _ahora_iso()

    por_id = {}
    for item in items_detectados:
        socio = item.get("socio") or "General"
        cupon_id = _hash_identidad(tienda_id, socio, item["titulo"])
        por_id[cupon_id] = item  # dedupe: si dos cards colisionan en socio+título, se queda la última

    ids_vistos = list(por_id.keys())
    anteriores = await db.cargar_cupones(pool, ids_vistos)

    registros = []
    candidatas = []
    for cupon_id, item in por_id.items():
        anterior = anteriores.get(cupon_id)
        hash_contenido = _hash_contenido(item)
        primera_deteccion = anterior["primera_deteccion"] if anterior else ahora

        if anterior is None or anterior["hash_publicado"] != hash_contenido:
            publicar = True
        else:
            ultima_publicacion = anterior["ultima_publicacion"]
            publicar = (
                ultima_publicacion is not None
                and _horas_entre(ultima_publicacion, ahora) >= config.HORAS_REPUBLICACION_CUPON
            )

        registro = {
            "id": cupon_id,
            "tienda_id": tienda_id,
            "comercio": comercio,
            "socio": item.get("socio") or "General",
            "titulo": item["titulo"],
            "descripcion": item.get("descripcion"),
            "tipo_descuento": item.get("tipo_descuento"),
            "valor_descuento": item.get("valor_descuento"),
            "tope_clp": item.get("tope_clp"),
            "dia_semana": item.get("dia_semana"),
            "vigencia_desde": item.get("vigencia_desde"),
            "vigencia_hasta": item.get("vigencia_hasta"),
            "codigo": item.get("codigo"),
            "como_activar": item.get("como_activar"),
            "url_fuente": item["url_fuente"],
            "imagen": item.get("imagen"),
            "hash_contenido": hash_contenido,
            "primera_deteccion": primera_deteccion,
            "ultima_actualizacion": ahora,
            "activo": True,
        }
        registros.append(registro)

        if publicar:
            candidatas.append({
                "id": cupon_id,
                "tipo": "cupon",
                "canal": "ofertas_combustible",
                "descuento_pct": 0,  # sentinel: el resto del pipeline (main.py/db.py) indexa este
                # campo sin chequear "tipo" — 0 nunca entra en el loop de UMBRAL_DESCUENTO_EXTREMO
                "titulo": registro["titulo"],
                "comercio": comercio,
                "socio": registro["socio"],
                "descripcion": registro["descripcion"],
                "tipo_descuento": registro["tipo_descuento"],
                "valor_descuento": registro["valor_descuento"],
                "tope_clp": registro["tope_clp"],
                "dia_semana": registro["dia_semana"],
                "vigencia_desde": registro["vigencia_desde"],
                "vigencia_hasta": registro["vigencia_hasta"],
                "codigo": registro["codigo"],
                "como_activar": registro["como_activar"],
                "url": registro["url_fuente"],
                "imagen": registro["imagen"],
                "hash_contenido": hash_contenido,
            })

    await db.upsert_cupones(pool, registros)
    await db.marcar_cupones_inactivos(pool, tienda_id, ids_vistos)

    log.info(
        "%s: %s cupones tocados. %s candidatas a publicar en Telegram.",
        comercio, len(registros), len(candidatas),
    )
    return candidatas


async def registrar_cupon_publicado(pool: asyncpg.Pool, cupon: dict) -> None:
    """Callback para telegram_publisher (vía publicar.py) — se llama una vez por cada cupón que
    Telegram confirma que se mandó de verdad. Recién acá se marca `hash_publicado`/
    `ultima_publicacion` (ver docstring del módulo)."""
    await db.marcar_cupon_publicado(pool, cupon["id"], cupon["hash_contenido"])
