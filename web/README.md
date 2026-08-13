# Plantilla web — agregador de ofertas + canales de Telegram

App de una sola página, estilo aplicación móvil, pensada como escaparate público de un
proyecto de ofertas en Telegram: muestra tus canales, el acceso VIP y un feed con las
últimas ofertas que genere tu scraper.

**Sin build, sin dependencias, sin backend.** Es HTML + Tailwind (por CDN) + JavaScript
vanilla. No hay `npm install` que hacer.

---

## Cómo verlo en local

`fetch()` y los módulos ES **no funcionan abriendo `index.html` con doble clic**: el
navegador bloquea ambos bajo el protocolo `file://`. Hay que servir la carpeta:

```bash
cd web
python -m http.server 8000
```

Y abrir <http://localhost:8000>. Con VS Code también sirve la extensión **Live Server**
(clic derecho sobre `index.html` → *Open with Live Server*).

## Cómo publicarlo

Cualquier hosting estático. Se sube la carpeta `web/` tal cual:

- **Cloudflare Pages** — *Create project → Direct Upload* y arrastrar la carpeta.
- **Netlify** — arrastrar la carpeta a <https://app.netlify.com/drop>.
- **GitHub Pages** — subir el contenido a la rama `gh-pages`.

---

## Estructura

```
web/
├── index.html          App shell: cabecera, sección home y nav inferior
├── manifest.json       PWA: permite instalarla en la pantalla de inicio
├── css/styles.css      Lo poco que Tailwind no cubre
├── js/
│   ├── app.js          Tema, navegación y arranque
│   ├── data.js         Carga y limpieza de los JSON
│   └── render.js       Construcción del DOM
├── data/
│   ├── config.json     Marca, canales y VIP   ← se edita a mano
│   └── ofertas.json    Feed de ofertas        ← lo regenera el scraper
└── images/             Logo, iconos PWA e imagen de Open Graph
```

---

## Personalización

### 1. Marca, canales y VIP → `data/config.json`

No hay que tocar código: se edita el JSON y se recarga.

```json
{
  "marca": {
    "nombre": "TU MARCA",
    "emoji": "🎯",
    "saludo": "Hola! 👋",
    "titulo": "¡Bienvenido!",
    "descripcion": "Únete a nuestra comunidad..."
  },
  "canales": [
    { "nombre": "Canal de ofertas 40%", "url": "https://t.me/tu_canal_40", "descripcion": "opcional" }
  ],
  "vip":   { "nombre": "Acceso VIP", "url": "https://t.me/tu_canal_vip", "icono": "crown" },
  "redes": [ { "nombre": "Instagram", "url": "https://instagram.com/...", "icono": "instagram" } ]
}
```

Los campos `icono` usan nombres de [Lucide](https://lucide.dev/icons/).

> **Ojo con los logos de marca.** Lucide retiró de su catálogo los iconos de Instagram,
> Facebook, X/Twitter y YouTube, así que esos nombres ya no existen. Usa alternativas
> genéricas (`camera`, `music-2`, `send`, `globe`, `message-circle`) o pon tu propio SVG.
> Si escribes un nombre que no existe, el botón muestra la inicial de su nombre en vez de
> quedarse en blanco.

### 2. Colores → bloque `tailwind.config` en `index.html`

Está todo en un solo sitio, cerca del inicio del archivo:

| Token | Uso | Valor de relleno |
|---|---|---|
| `marca` | Nav inferior, acentos, botón de copiar | `#5B4BE8` |
| `marca-oscuro` / `marca-claro` | Hover y texto sobre fondo oscuro | `#4A3BD1` / `#A99BFF` |
| `vip` / `vip-texto` | Botón de acceso VIP | `#E1A32B` / `#1A1206` |
| `superficie` / `superficie-alta` | Tarjetas en modo oscuro | `#1C1C21` / `#25252B` |
| `fondo` | Fondo en modo oscuro | `#0B0B0E` |

### 3. Logo e imágenes → `images/`

Reemplaza manteniendo los nombres: `icono-192.png` (favicon y PWA) e `icono-512.png` (PWA)
y `og-image.png` (1200×630, es la miniatura al compartir el enlace en Telegram o WhatsApp).
`logo.svg` quedó sin uso (era el placeholder original).

### 4. Textos de la cabecera y del `<title>`

Salen de `config.json`. Los de respaldo en `index.html` (`<title>`, `og:title`,
`description`) conviene cambiarlos también, porque son los que leen Google y Telegram
antes de que corra el JavaScript.

---

## Contrato de `data/ofertas.json`

Este es el archivo que debe escribir el scraper de Telegram.

```json
{
  "actualizado": "2026-08-09T19:00:00Z",
  "ofertas": [
    {
      "id": "msg_10235",
      "titulo": "Audífonos inalámbricos con 62% de descuento",
      "descuento": "62%",
      "comercio": "Falabella",
      "categoria": "tecnologia",
      "cupon": "TECH62",
      "detalle": "Stock limitado. El precio puede volver a subir en cualquier momento.",
      "url": "https://t.me/tu_canal_60/10235",
      "imagen": null,
      "fecha": "2026-08-09T14:30:00Z",
      "canal": "ofertas_60"
    }
  ]
}
```

| Campo | Obligatorio | Notas |
|---|---|---|
| `id` | **Sí** | Texto o número. Se usa para deduplicar; ideal `msg_<id de Telegram>`. |
| `titulo` | **Sí** | Se recorta visualmente a 2 líneas. |
| `descuento` | No | Texto libre: `"30%"`, `"2x1"`, `"$5.000 off"`. Va en la insignia. |
| `comercio` | No | Nombre de la tienda o app. |
| `categoria` | No | Reservado para filtros futuros; hoy no se muestra. |
| `cupon` | No | Si viene, aparece el bloque con botón de copiar. |
| `detalle` | No | Letra chica: topes, restricciones. Se recorta a 2 líneas. |
| `url` | No | Solo `http(s)`. Enlace al mensaje original: es lo que devuelve tráfico a Telegram. |
| `imagen` | No | Reservado; hoy no se muestra. |
| `fecha` | No | ISO 8601. Ordena el feed y genera el "hace 2 horas". Sin ella, va al final. |
| `canal` | No | De qué canal salió. Reservado para filtros futuros. |

Comportamiento ante datos sucios, que es lo normal viniendo de Telegram:

- Las ofertas sin `id` o sin `titulo` se descartan en silencio.
- Los `id` repetidos se colapsan en uno (el mismo mensaje puede llegar por varios canales).
- Las URL que no sean `http`/`https` se ignoran (bloquea `javascript:` y similares).
- Se ordena por `fecha` descendente y se muestran las **30 más recientes**.
- Si el archivo falta o está roto, la página **sigue funcionando** con los canales y un
  estado vacío en el feed. Que se caiga el scraper no puede tumbar el sitio.

Todo el contenido se inserta con `textContent`, nunca como HTML, así que un mensaje de
Telegram con etiquetas dentro se muestra como texto literal y no se ejecuta.

---

## Añadir una pestaña nueva

Hoy el nav inferior tiene un único destino, Inicio. Para añadir otro, por ejemplo un
listado de tarjetas:

1. Añade la pestaña al array `SECCIONES` en [`js/app.js`](js/app.js), con su `id`, `icono`
   (nombre de [Lucide](https://lucide.dev/icons/)) y `etiqueta`.
2. Añade ese mismo `id` a `SECCIONES_ACTIVAS` para que el botón quede habilitado en vez de
   atenuado.
3. Crea `<section id="section-tu-id" class="hidden">…</section>` dentro de `<main>` en
   `index.html`.
4. Añade su función de render en `js/render.js` y llámala desde `iniciar()`.

El nav inferior se genera solo a partir de esos arrays: no hay que tocar su HTML. Si quieres
dejar una pestaña visible pero deshabilitada como "próximamente", añádela a `SECCIONES` y
deja su `id` fuera de `SECCIONES_ACTIVAS`.
