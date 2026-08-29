"""Mantiene el snapshot de cupones de combustible (Copec/Shell — ver
scraper/fuentes/copec|shell/listado.py) en Postgres (tabla `cupones_combustible`, ver db.py) y
arma el digest diario que scraper/combustible.py manda a Telegram.

Paralelo a ofertas_writer.py, pero deliberadamente más chico: un cupón no tiene "producto" ni
precio, así que no aplican las Reglas 1/2/3 (mínimo histórico de precio) ni el concepto de
"republicar cuando cambia" — acá la publicación es un DIGEST diario (un mensaje con los cupones
vigentes hoy), no un post por cupón. `procesar_cupones()` solo mantiene el snapshot al día;
`construir_grupos_digest()` decide qué entra en el mensaje de hoy.

Como ambas fuentes leen su catálogo completo cada corrida (sin muestreo de categorías/páginas), un
cupón no visto en una corrida exitosa se marca inactivo de inmediato — a diferencia de
GRACIA_INACTIVO_HORAS en productos, acá no hace falta período de gracia."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import asyncpg

import db
import dias_semana

log = logging.getLogger("scraper.cupones_writer")

_ORDEN_COMERCIOS = ("Shell", "Copec")


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_identidad(tienda_id: str, socio: str, titulo: str) -> str:
    return hashlib.sha1(f"{tienda_id}|{socio}|{titulo}".encode("utf-8")).hexdigest()[:16]


async def procesar_cupones(
    pool: asyncpg.Pool, items_detectados: list[dict], tienda_id: str, comercio: str
) -> None:
    """Actualiza el snapshot de `cupones_combustible` a partir de los cupones detectados en esta
    corrida — upsert + marcar inactivos, sin decidir publicación (ver docstring del módulo)."""
    ahora = _ahora_iso()

    por_id = {}
    for item in items_detectados:
        socio = item.get("socio") or "General"
        cupon_id = _hash_identidad(tienda_id, socio, item["titulo"])
        por_id[cupon_id] = item  # dedupe: si dos cards colisionan en socio+título, se queda la última

    ids_vistos = list(por_id.keys())
    anteriores = await db.cargar_cupones(pool, ids_vistos)

    registros = []
    for cupon_id, item in por_id.items():
        anterior = anteriores.get(cupon_id)
        primera_deteccion = anterior["primera_deteccion"] if anterior else ahora

        registros.append({
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
            "primera_deteccion": primera_deteccion,
            "ultima_actualizacion": ahora,
            "activo": True,
        })

    await db.upsert_cupones(pool, registros)
    await db.marcar_cupones_inactivos(pool, tienda_id, ids_vistos)

    log.info("%s: %s cupones tocados en el snapshot.", comercio, len(registros))


def construir_grupos_digest(activos: list[dict], hoy: str) -> dict | None:
    """Filtra el snapshot activo a lo que corresponde mostrar en el digest de HOY, en 3 grupos:

    - "hoy": cupones de día específico que aplican hoy, agrupados por comercio.
    - "todos": cupones CONFIRMADOS como "todos los días" por la fuente (ej. Copec siempre trae su
      `tag-dia`, aunque diga "Todos los días" explícito).
    - "sin_dia": cupones sin ningún día detectado (`dia_semana is None`) — hoy solo pasa con
      Shell, que no expone día en ningún campo estructurado y solo se infiere cuando el texto lo
      menciona explícitamente (ver dias_semana.inferir_dia_semana_texto). Tratarlos igual que
      "todos los días" sería engañoso: no sabemos si de verdad aplican siempre o si son de un
      día específico que no logramos detectar — van aparte, con esa incertidumbre explícita en
      el mensaje (ver telegram_publisher.formatear_digest_cupones).

    None si no queda nada que mostrar en ningún grupo."""
    hoy_por_comercio: dict[str, list[dict]] = {comercio: [] for comercio in _ORDEN_COMERCIOS}
    todos_confirmado: list[dict] = []
    sin_dia_confirmado: dict[str, list[dict]] = {comercio: [] for comercio in _ORDEN_COMERCIOS}

    for cupon in activos:
        texto_dia = cupon.get("dia_semana")
        if texto_dia is None:
            sin_dia_confirmado.setdefault(cupon["comercio"], []).append(cupon)
            continue
        dias = dias_semana.normalizar_dia_semana(texto_dia)
        if "todos" in dias:
            todos_confirmado.append(cupon)
        elif dias_semana.dia_aplica_hoy(dias, hoy):
            hoy_por_comercio.setdefault(cupon["comercio"], []).append(cupon)

    if (
        not any(hoy_por_comercio.values())
        and not todos_confirmado
        and not any(sin_dia_confirmado.values())
    ):
        return None
    return {"hoy": hoy_por_comercio, "todos": todos_confirmado, "sin_dia": sin_dia_confirmado}
