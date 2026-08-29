"""Sintetiza, con Claude Haiku, una frase corta por cupón de combustible para el digest diario
(ver scraper/combustible.py y telegram_publisher.formatear_digest_cupones).

Por qué acá sí conviene un LLM y en el resto del scraper no: los datos "duros" de cada cupón (día
de la semana, banco/socio, vigencia) ya se extraen de forma estructurada y determinística en
fuentes/copec|shell/listado.py (taxonomías reales de Shell, campos de la página de detalle de
Copec) — ahí un LLM no aportaría nada, sería más caro y menos confiable que leer el dato
directamente. Lo que SÍ es un problema real de lenguaje es convertir la descripción en prosa de
cada promo (redactada por marketing, con un estilo muy distinto entre Copec y Shell — ver
docstrings de esas fuentes) en una frase corta y consistente de "cuánto ahorras + cómo activarlo".
Un regex fijo por sitio es frágil ante cualquier cambio de redacción; esto es exactamente el tipo
de tarea de síntesis de texto libre para la que un LLM chico y barato tiene sentido.

Se llama UNA vez por cupón (ver db.cargar_cupones_sin_resumen/guardar_resumenes — el resultado
queda cacheado en `resumen_digest`), en un solo batch para todos los cupones nuevos del día, no
una llamada por cupón. Si falla o no hay API key, se devuelve {} y quien llama cae a un resumen
por defecto — el digest nunca deja de mandarse por esto.
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("scraper.cupones_sintesis")

_MAX_TOKENS_POR_CUPON = 40
_MAX_TOKENS_BASE = 200


def _bloque_cupon(cupon: dict) -> str:
    partes = [
        f"id: {cupon['id']}",
        f"comercio: {cupon['comercio']}",
        f"socio/banco: {cupon.get('socio') or 'ninguno'}",
        f"título: {cupon['titulo']}",
        f"descripción: {cupon.get('descripcion') or '(sin descripción)'}",
    ]
    if cupon.get("como_activar"):
        partes.append(f"método de activación (dato ya confirmado): {cupon['como_activar']}")
    if cupon.get("codigo"):
        partes.append(f"código exacto (dato ya confirmado): {cupon['codigo']}")
    return "\n".join(partes)


def sintetizar_resumenes(cupones: list[dict]) -> dict[str, str]:
    """Devuelve {id: resumen} para los cupones que se pudieron sintetizar. Puede devolver menos
    ids que los pedidos (o {} completo) si el LLM no respondió para todos o si algo falló — nunca
    lanza excepción."""
    if not cupones:
        return {}
    if not config.ANTHROPIC_API_KEY:
        log.error("Cupones combustible: falta ANTHROPIC_API_KEY, no se puede sintetizar el resumen")
        return {}

    import anthropic

    ids_pedidos = {c["id"] for c in cupones}
    cliente = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (
        "Estos son cupones/descuentos de combustible en gasolineras chilenas (Copec o Shell). "
        "Para cada uno, escribe una frase corta en español de Chile (máximo 12 palabras) que "
        "combine, si el texto lo dice, CUÁNTO se ahorra (monto o % por litro) y CÓMO se activa "
        "(con qué tarjeta/app/código) — lo más accionable posible para alguien que la lee en un "
        "mensaje de Telegram y tiene que decidir en 2 segundos si le sirve.\n\n"
        "Si la descripción no menciona un monto exacto, no inventes uno: describe solo cómo "
        "activarlo. Si ya viene un 'código exacto' o 'método de activación' confirmado en los "
        "datos, úsalo tal cual en vez de parafrasearlo. No repitas el título ni el nombre del "
        "banco si ya va a mostrarse aparte — anda directo al beneficio y la acción.\n\n"
        + "\n\n".join(_bloque_cupon(c) for c in cupones)
    )
    herramienta = {
        "name": "registrar_resumenes",
        "description": "Registra la frase-resumen sintetizada para cada cupón, por id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "resumenes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "El id del cupón, tal cual se dio."},
                            "resumen": {"type": "string"},
                        },
                        "required": ["id", "resumen"],
                    },
                }
            },
            "required": ["resumenes"],
        },
    }

    try:
        respuesta = cliente.messages.create(
            model=config.ANTHROPIC_MODEL_COMBUSTIBLE,
            max_tokens=_MAX_TOKENS_BASE + _MAX_TOKENS_POR_CUPON * len(cupones),
            messages=[{"role": "user", "content": prompt}],
            tools=[herramienta],
            tool_choice={"type": "tool", "name": "registrar_resumenes"},
        )
        bloque = next(b for b in respuesta.content if b.type == "tool_use")
        crudos = bloque.input.get("resumenes", [])
        resultado = {
            item["id"]: item["resumen"].strip()
            for item in crudos
            if item.get("id") in ids_pedidos and item.get("resumen", "").strip()
        }
        log.info("Cupones combustible: %s/%s resúmenes sintetizados", len(resultado), len(cupones))
        return resultado
    except Exception:
        log.exception("Cupones combustible: falló la llamada a Claude o el parseo de su respuesta")
        return {}
