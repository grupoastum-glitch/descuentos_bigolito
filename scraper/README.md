# Scraper de ofertas — multi-tienda (ver `config.TIENDAS`)

Job que corre cada hora (Cron Schedule de Railway, no un proceso 24/7): scrapea la colección
de ofertas de Falabella y los productos puntuales listados en
[`productos_seguidos.json`](productos_seguidos.json), actualiza
[`../web/data/ofertas.json`](../web/data/ofertas.json), postea en Telegram las ofertas nuevas,
y pushea los cambios a `main` — eso dispara el redeploy automático de la web en Cloudflare
Pages.

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
4. Decide qué es "nuevo" por tienda, cada una con su propio archivo de estado (ver
   `config.TIENDAS`) que guarda el historial de **todos** los productos vistos alguna vez, no
   solo los activos.
5. Escribe `ofertas.json` combinando las ofertas activas de todas las tiendas por encima del
   piso mínimo, y postea en Telegram solo las que son récord de precio/descuento nuevo (ver
   `ofertas_writer.py`) — así una oferta que sigue vigente sin cambios no se re-postea cada hora.
6. Commitea y pushea `ofertas.json` + el estado de cada tienda a `main`.

## Alcance inicial (a propósito, acotado)

- Falabella Chile y Xiaomi Chile (`mi.com/cl`) — agregar una tienda nueva implica un módulo en
  `fuentes/<tienda>/` más una entrada en `config.TIENDAS`, ver el resto del código como ejemplo.
- El posteo automático a Telegram hoy está activo en dos canales/tiers, exclusivos por tramo (ver
  `config.TIERS_DESCUENTO` y `config.CANAL_TELEGRAM_USERNAME`): `ofertas_40` (40-59% de
  descuento) y `ofertas_vip` (60% o más — un producto de 65% cae directo en VIP y nunca se
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
| `TELEGRAM_BOT_TOKEN` | El mismo token del bot (`bot/.env`) — necesita permiso de admin en cada canal de `config.CANAL_TELEGRAM_USERNAME`. |

## Desplegar en Railway

Es un **servicio nuevo y separado** del bot (mismo proyecto de Railway), no un proceso que se
mezcla con `bot/bot.py` — el bot es *long polling* 24/7 con footprint mínimo, el scraper es un
job pesado que solo debe correr una vez por hora.

1. New Service → mismo repo de GitHub → **Root Directory vacío** (igual que el bot, necesita
   el checkout completo para llegar a `web/data/`).
2. **Dockerfile Path**: `scraper/Dockerfile`.
3. **Cron Schedule**: `0 * * * *`.
4. **Variables**: `GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`.
