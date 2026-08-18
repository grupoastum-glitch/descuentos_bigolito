# Scraper de ofertas — multi-tienda (ver `config.TIENDAS`)

Job que corre cada hora (Cron Schedule de Railway, no un proceso 24/7): scrapea la colección
de ofertas de Falabella y los productos puntuales listados en
[`productos_seguidos.json`](productos_seguidos.json), actualiza
[`../web/data/ofertas.json`](../web/data/ofertas.json), encola en Postgres las ofertas nuevas
para publicar, y pushea los cambios a `main` — eso dispara el redeploy automático de la web en
Cloudflare Pages.

**Este job NO publica a Telegram directo.** Eso lo hace [`publicar.py`](publicar.py), un
servicio separado que corre 24/7 (no cron, igual que `bot/bot.py`) y drena la cola de Postgres
a su propio ritmo — ver "Desplegar el publicador" más abajo. Se separó así porque el volumen de
ofertas por publicar en una corrida puede tardar horas en drenarse (Telegram limita a un mensaje
cada `config.TELEGRAM_DELAY_SEGUNDOS` por canal), y ese tiempo no debe bloquear el advisory lock
que necesita la próxima corrida horaria del scraper para arrancar (ver `run_lock.py`).

## Cómo funciona

1. Clona el repo (necesita `GITHUB_TOKEN`) — así siempre lee la versión más reciente de
   `productos_seguidos.json`, aunque se haya editado directo en GitHub sin redeployar nada.
2. Scrapea `falabella.com/falabella-cl/collection/ofertas` (ya viene pre-filtrada a productos
   con descuento) y cada producto activo de `productos_seguidos.json`.
3. Cada tienda scrapea distinto (ver `fuentes/<tienda>/`): Falabella es una app Next.js, lee el
   bloque `<script id="__NEXT_DATA__">` (ver [`fuentes/falabella/parsing.py`](fuentes/falabella/parsing.py));
   Xiaomi (`mi.com/cl`) trae los datos en `window.__PRELOADED_STATE__` en sus páginas de
   categoría (ver [`fuentes/xiaomi/listado.py`](fuentes/xiaomi/listado.py)). Ambas evitan parsear
   clases CSS (que cambian con cada build) leyendo el JSON que el propio sitio ya embebe.
4. Decide qué es "nuevo" por tienda contra el estado en Postgres (tabla `productos`, ver
   `scraper/db.py`), que guarda el historial de **todos** los productos vistos alguna vez, no
   solo los activos. Cada publicación confirmada por Telegram se guarda ahí al toque, pero eso
   lo hace `publicar.py` (ver más abajo), no este job — así una corrida interrumpida a mitad de
   camino, o una publicación que tarda horas en drenarse, no reenvía lo que ya se publicó de
   verdad.
5. Escribe `ofertas.json` combinando las ofertas activas de todas las tiendas por encima del
   piso mínimo, y encola en `cola_publicacion` (Postgres) solo las que son récord de
   precio/descuento nuevo (ver `ofertas_writer.py`) — así una oferta que sigue vigente sin
   cambios no se re-postea cada hora. `publicar.py` es quien las manda a Telegram de verdad.
6. Commitea y pushea `ofertas.json` a `main` (el estado de cada tienda vive en Postgres, no en
   el repo).

## Alcance inicial (a propósito, acotado)

- Falabella Chile y Xiaomi Chile (`mi.com/cl`) — agregar una tienda nueva implica un módulo en
  `fuentes/<tienda>/` más una entrada en `config.TIENDAS`, ver el resto del código como ejemplo.
- El posteo automático a Telegram hoy está activo en dos canales/tiers, exclusivos por tramo (ver
  `config.TIERS_DESCUENTO` y `config.CANAL_TELEGRAM_USERNAME`): `ofertas_40` (25-49% de
  descuento) y `ofertas_vip` (50% o más — un producto de 55% cae directo en VIP y nunca se
  postea en el canal gratis). Sumar un tier nuevo es agregar una línea en ambos, en la posición
  correcta según su mínimo, **una vez que el bot ya sea admin** de ese canal (permiso "Publicar
  mensajes") — sin tocar el resto del código. El canal depende solo del % de descuento, es el
  mismo para todas las tiendas.
- El orden de posteo se mezcla al azar (`random.shuffle` sobre la lista combinada de todas las
  tiendas, justo antes de publicar) para que no se note el orden de scrapeo (una tienda entera
  antes de pasar a la siguiente).

## Cómo correrlo en local

```bash
cd scraper
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

cp .env.example .env   # completar GITHUB_TOKEN, TELEGRAM_BOT_TOKEN
python main.py
```

Para probar sin tocar el repo real de producción, apuntá `GITHUB_TOKEN`/`GITHUB_REPO` a un
fork o una rama de prueba en `.env` antes de correrlo.

## Variables de entorno

| Variable | Para qué |
|---|---|
| `GITHUB_TOKEN` | Fine-grained PAT restringido a este repo, permiso `Contents: Read and write`. Clona y pushea. |
| `GITHUB_REPO` | `owner/repo`. Requerido — sin default, para no publicar por error en el repo equivocado. |
| `GITHUB_BRANCH` | Rama a la que pushear (default: `main`). |
| `DATABASE_URL` | Conexión a Postgres (addon de Railway) — estado de productos/historial de precios y lock entre corridas, ver `scraper/db.py` y `scraper/run_lock.py`. Requerida, sin default. |
| `TELEGRAM_BOT_TOKEN` | El mismo token del bot (`bot/.env`) — necesita permiso de admin en cada canal de `config.CANAL_TELEGRAM_USERNAME`. |

## Desplegar en Railway

Es un **servicio nuevo y separado** del bot (mismo proyecto de Railway), no un proceso que se
mezcla con `bot/bot.py` — el bot es *long polling* 24/7 con footprint mínimo, el scraper es un
job pesado que solo debe correr una vez por hora.

1. New Service → mismo repo de GitHub → **Root Directory vacío** (igual que el bot, necesita
   el checkout completo para llegar a `web/data/`).
2. **Dockerfile Path**: `scraper/Dockerfile`.
3. **Cron Schedule**: `0 * * * *`.
4. **Variables**: `GITHUB_TOKEN`, `DATABASE_URL` (referencia al addon de Postgres del mismo
   proyecto Railway — no se conecta sola, hay que agregarla a mano en la pestaña Variables del
   servicio), `TELEGRAM_BOT_TOKEN`.
5. **Watch Paths**: `scraper/**` — para que un push que solo toca `bot/`/`web/`/docs no dispare
   un redeploy de este servicio (y no corte una corrida activa).

## Desplegar el publicador en Railway

Servicio nuevo y separado (mismo proyecto de Railway, mismo repo) — corre 24/7, **sin Cron
Schedule**, igual que `bot/bot.py`. Es el único proceso que lee `cola_publicacion` y manda
mensajes a Telegram; el scraper de arriba solo escribe ahí.

1. New Service → mismo repo de GitHub → **Root Directory vacío**.
2. **Dockerfile Path**: `scraper/Dockerfile.publicar` (no instala Chromium — `publicar.py` no
   scrapea nada).
3. **Sin Cron Schedule** — Restart Policy por defecto (siempre corriendo).
4. **Variables**: `DATABASE_URL` (mismo Postgres que el scraper), `TELEGRAM_BOT_TOKEN` (mismo
   token). No necesita `GITHUB_TOKEN`/`GITHUB_REPO` — no toca git.
5. **Watch Paths**: `scraper/**` — mismo motivo que el scraper.

Revisar en el log al arrancar: `Publicador arrancado, drenando cola_publicacion cada 15s cuando
está vacía.` — si no aparece, revisar `DATABASE_URL`/`TELEGRAM_BOT_TOKEN`.
