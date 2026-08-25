"""Cron diario (Railway) — red de seguridad para cuando un webhook de MercadoPago se perdió (ej.
el servicio de pagos estaba caído en ese momento). No es el mecanismo principal — eso es
pagos/webhook.py, que reacciona en tiempo real; esto solo corrige drift, en cuatro fases:
1. Reconfirma cada suscripción marcada como activa localmente contra su estado real en MercadoPago.
2. Descubre preapprovals 'authorized' que no tengamos reflejadas como activas localmente —
   clientes nuevos o resuscripciones cuyo webhook se perdió del todo (ver
   _descubrir_preapprovals_perdidas).
3. Avisa por DM a quienes su prueba gratis está por vencer (exclusivo de la prueba, ver
   _avisar_pruebas_por_vencer — una suscripción paga no recibe este aviso, su cobro es automático).
4. Expulsa a quienes su período de gracia (pago o de prueba) ya venció.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import db  # noqa: E402 (después de load_dotenv a propósito, config lee os.environ)
import logica  # noqa: E402
import mercadopago_client  # noqa: E402
import telegram_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pagos.reconciliacion")


async def _descubrir_preapprovals_perdidas(pool) -> None:
    """Red de seguridad para cuando un webhook de autorización se perdió (ej. pagos/webhook.py
    estaba caído en ese momento) — dos casos:
    1. Cliente nuevo para un canal: el par (telegram_user_id, canal_id) nunca existió en
       `suscripciones`. La fase de arriba (listar_activas) no lo detecta porque solo reconfirma
       filas que ya existen.
    2. Resuscripción perdida: el par ya existe pero con estado != 'activa' (canceló/venció antes)
       y hay una preapproval 'authorized' **distinta** (id nuevo) para ese mismo par en
       MercadoPago — la resuscripción real, cuyo webhook nunca llegó.

    En ambos casos se aplica igual que un webhook normal. Deliberadamente NUNCA se toca un par
    cuyo estado local YA es 'activa' — esos quedan exclusivamente a cargo de la fase de arriba,
    que reconfirma cada uno contra su preapproval_id exacto; así es estructuralmente imposible
    que este descubrimiento pise una suscripción vigente con una preapproval vieja o equivocada.

    Tampoco se toca un par cuyo estado local no es 'activa' si la preapproval 'authorized'
    encontrada es la MISMA que ya teníamos guardada (mismo id): eso no es una resuscripción, es la
    preapproval original todavía reintentando cobrar en segundo plano tras un pago fallido
    (MercadoPago puede tardar varios días en cancelarla de verdad, ver
    PENDIENTE_produccion_mercadopago.md) — rescatarla reactivaría a alguien que en realidad no
    pagó nada nuevo (bug reproducido en vivo con una preapproval de prueba).

    Se llama después de esa fase a propósito: si una fila local 'activa' estaba desactualizada
    (ej. su preapproval real ya fue cancelada en MercadoPago porque el usuario se resuscribió y
    esa cancelación también se perdió), la fase de arriba ya la corrigió en este mismo run, así
    que acá se puede agarrar la resuscripción el mismo día en vez de esperar a mañana."""
    # Todo lo previo al procesamiento por ítem queda en un único try/except: un fallo acá (ej. la
    # búsqueda a MercadoPago, o la consulta a nuestra propia base) no debe poder escaparse de esta
    # función y cortar el resto de _correr() (en particular la fase de vencidas que corre
    # después) — mismo criterio de aislamiento que ya usa el resto de este archivo.
    try:
        preapprovals = await asyncio.to_thread(mercadopago_client.buscar_todas_preapprovals)
        registrados = await db.listar_pares_registrados(pool)
        autorizadas = [p for p in preapprovals if p.get("status") == "authorized"]
        pendientes = []
        for p in autorizadas:
            referencia = logica.parse_external_reference(p.get("external_reference"))
            if referencia is None:
                continue
            telegram_user_id, canal_id, _email = referencia
            fila = registrados.get((telegram_user_id, canal_id))
            es_resuscripcion = (
                fila is not None
                and fila["estado"] != "activa"
                and fila["mercadopago_preapproval_id"] != p.get("id")
            )
            if fila is None or es_resuscripcion:
                # None → cliente nuevo para este canal. es_resuscripcion → preapproval nueva de
                # verdad para un par que no estaba activo; aplicar_estado_preapproval hace el
                # upsert correcto en cualquiera de los dos casos.
                pendientes.append(p)
    except Exception:
        log.exception("Falló preparar el descubrimiento de preapprovals perdidas.")
        return

    log.info(
        "%s preapprovals autorizadas en MercadoPago, %s no coinciden con un par ya activo localmente.",
        len(autorizadas), len(pendientes),
    )
    for preapproval in pendientes:
        try:
            await logica.aplicar_estado_preapproval(pool, preapproval)
        except Exception:
            log.exception(
                "Falló aplicar_estado_preapproval en el descubrimiento para %s",
                preapproval.get("id"),
            )


async def _avisar_pruebas_por_vencer(pool) -> None:
    """Tercera fase: DM previo a quienes su prueba gratis está por vencer (ver
    db.listar_pruebas_por_vencer). No tiene equivalente para suscripciones pagas — el cobro
    recurrente de MercadoPago es automático, no hay nada que "avisar" de antemano ahí. Cada fila
    se procesa por separado: que falle una (ej. nunca le escribió al bot) no debe impedir avisar
    al resto ni cortar la fase de expulsión que corre después."""
    pruebas = await db.listar_pruebas_por_vencer(pool)
    log.info("%s pruebas gratis por vencer, mandando aviso.", len(pruebas))

    for fila in pruebas:
        dias_restantes = max((fila["acceso_hasta"] - datetime.now(timezone.utc)).days, 0) + 1
        try:
            await telegram_client.avisar_vencimiento_prueba(
                fila["telegram_user_id"], fila["canal_id"], dias_restantes,
            )
            await db.marcar_recordatorio_prueba_enviado(pool, fila["telegram_user_id"], fila["canal_id"])
        except Exception:
            log.exception(
                "Falló avisar el vencimiento de prueba de %s en canal %s",
                fila["telegram_user_id"], fila["canal_id"],
            )


async def _correr() -> None:
    pool = await db.conectar()
    try:
        activas = await db.listar_activas(pool)
        log.info("%s suscripciones activas a reconfirmar.", len(activas))

        for fila in activas:
            preapproval_id = fila["mercadopago_preapproval_id"]
            try:
                preapproval = await asyncio.to_thread(
                    mercadopago_client.obtener_preapproval, preapproval_id,
                )
                await logica.aplicar_estado_preapproval(pool, preapproval)
            except Exception:
                # una suscripción con problemas (ej. la preapproval ya no existe en MP) no debe
                # cortar la reconciliación del resto — se reintenta mañana.
                log.exception(
                    "Falló la reconciliación de la preapproval %s (usuario %s, canal %s)",
                    preapproval_id, fila["telegram_user_id"], fila["canal_id"],
                )

        # segunda fase: preapprovals de MercadoPago que no tenemos reflejadas localmente (ver
        # docstring de _descubrir_preapprovals_perdidas) — corre después de la reconfirmación de
        # arriba a propósito, para aprovechar sus correcciones en este mismo run.
        await _descubrir_preapprovals_perdidas(pool)

        # tercera fase: aviso previo a quienes su prueba gratis está por vencer — antes de la
        # fase de expulsión a propósito, para no avisar a alguien que ya se está por expulsar en
        # esta misma corrida (listar_pruebas_por_vencer excluye lo ya vencido, así que no se
        # solapan de todas formas, pero el orden deja la lectura de los logs más clara).
        await _avisar_pruebas_por_vencer(pool)

        # cuarta fase: cuyo período ya pagado (o de prueba) venció — recién acá se corta el acceso
        # de verdad (ver PLAN_periodo_gracia_cancelacion.md). Incluye filas 'activa' cuyo cobro
        # recurrente falló en silencio (MercadoPago sigue reintentando sin haber cambiado el
        # status todavía), no solo cancelaciones/pausas explícitas. No requiere consultar a
        # MercadoPago, es una comparación de fecha local.
        vencidas = await db.listar_vencidas(pool)
        log.info("%s suscripciones con período de gracia vencido.", len(vencidas))

        for fila in vencidas:
            # 'vencida' (impago, recuperable si un reintento posterior tiene éxito — ver
            # pagos/logica.py::aplicar_pago_recurrente) vs 'expirada' (cancelación/pausa explícita
            # del usuario, final).
            nuevo_estado = "vencida" if fila["estado"] == "activa" else "expirada"
            try:
                await telegram_client.expulsar(fila["telegram_user_id"], fila["canal_id"])
                await db.marcar_estado(pool, fila["telegram_user_id"], fila["canal_id"], nuevo_estado)
            except Exception:
                # se reintenta mañana, mismo criterio que la fase de reconfirmación de arriba.
                log.exception(
                    "Falló la expulsión por vencimiento de %s en canal %s",
                    fila["telegram_user_id"], fila["canal_id"],
                )

        log.info("Reconciliación terminada.")
    finally:
        await db.cerrar()


def main() -> None:
    asyncio.run(_correr())


if __name__ == "__main__":
    main()
