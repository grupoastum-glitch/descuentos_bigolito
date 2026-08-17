# Bot de Telegram — agregador de descuentos

Bot de comandos que sirve de puerta de entrada a los canales de Telegram, igual que hace
la web. Comparte la misma fuente de datos: **[`../web/data/config.json`](../web/data/config.json)**.
Editar ese archivo actualiza tanto la web como el bot, sin tocar código.

## Cómo correrlo

```bash
cd bot
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

cp .env.example .env   # y pega ahí tu token de BotFather
python bot.py
```

El bot queda corriendo por *long polling* — no necesita servidor público ni webhook para
probarlo en local.

## Comandos

| Comando | Qué hace | De dónde saca los datos |
|---|---|---|
| `/start` | Bienvenida + menú de botones (canales, VIP, ayuda, información) | `marca`, `canales`, `vip`, `contacto` |
| `/stats` | Oculto, admin-only (`BOT_ADMIN_TELEGRAM_USER_ID` en `.env`): resumen de altas, renovaciones e ingresos | `pagos_historial` y `suscripciones` (Postgres, ver `bot/db.py::estadisticas`) |

Toda la interacción con el bot para usuarios normales pasa por los botones del menú de
`/start`, no por comandos de texto adicionales. `/stats` es la única excepción — a propósito no
figura en `config.json::bot.comandos` (el autocompletado de Telegram), para no revelar que
existe a nadie que no sea el admin.

## Perfil del bot (descripción, bio, menú de comandos)

Esto normalmente se configura a mano en BotFather con `/setdescription`, `/setabouttext` y
`/setcommands`. Aquí se aplica automáticamente al arrancar, leyendo el bloque `"bot"` de
`config.json` (`descripcion_larga`, `descripcion_corta`, `comandos`) — es idempotente, así
que reiniciar el bot después de editar esos textos los vuelve a sincronizar con Telegram.

Lo único que **no** se puede hacer por API es la foto de perfil del bot: eso sí requiere
`/setuserpic` en BotFather de forma manual, subiendo una imagen cuadrada (1:1).

## Desplegar en Railway

El repo es un monorepo (`web/` + `bot/`), y el bot necesita leer `web/data/config.json` —
por eso el deploy usa un [`Dockerfile`](Dockerfile) propio en vez del "Root Directory" de
Railway: ese ajuste recorta el checkout a una sola carpeta y `web/` dejaría de existir para
el bot. La configuración se hace 100% a mano en el dashboard de Railway (no hay `railway.json`
en el repo, cada servicio se configura por separado):

1. **Root Directory**: dejarlo **vacío** (no ponerle `bot`).
2. **Dockerfile Path**: `bot/Dockerfile`.
3. **Variables** del servicio → agregar `TELEGRAM_BOT_TOKEN` con el token de BotFather.
   `bot/.env` nunca se sube al repo, así que en Railway el token vive solo ahí.

Cada `git push` a `main` redespliega solo.

## Redeploys sin conflicto

Cada redeploy puede generar un `Conflict: terminated by other getUpdates request`: Railway
mantiene el contenedor viejo vivo en paralelo al nuevo por unos segundos
(`RAILWAY_DEPLOYMENT_OVERLAP_SECONDS`), y Telegram solo permite un *poller* por token — ahí
chocan los dos. Dos variables de servicio en Railway → **Variables** apagan la ventana de
solape:

| Variable | Valor | Por qué |
|---|---|---|
| `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS` | `0` | El contenedor viejo no sigue corriendo junto al nuevo. |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `5` | Si aun así se manda `SIGTERM` al viejo, le da un respiro para cerrar en orden (que `bot.py` ya sabe hacer) antes del `SIGKILL` — por defecto Railway da 0 segundos. |

Como red de seguridad para cualquier solape residual, `bot.py` también registra un manejador
de errores que silencia el traceback de un `Conflict` puntual (solo lo loguea como una línea
de advertencia). A propósito **no** usa `drop_pending_updates`: Telegram ya evita que dos
instancias procesen el mismo mensaje (por eso da 409 en vez de entregarlo dos veces), así que
ese flag no protegía contra nada real — solo hacía que un mensaje llegado justo durante la
ventana de redeploy se perdiera en vez de demorarse unos segundos.

## Robustez

Si `config.json` falta o queda corrupto, el bot no se cae: usa una configuración mínima de
respaldo y cada comando responde con un mensaje de "todavía no hay X cargado" en vez de
lanzar una excepción — mismo criterio que `web/js/data.js` en la web.
