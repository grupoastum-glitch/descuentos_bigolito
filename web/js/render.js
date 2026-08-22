/* ---------------------------------------------------------------
   Construcción del DOM.

   Todo se arma con createElement y textContent. Nunca innerHTML con
   datos: el contenido de las ofertas viene de Telegram y meterlo
   como HTML sería una inyección directa en la página.
   --------------------------------------------------------------- */

/* Clases repetidas, en un solo sitio para poder retocar el estilo
   sin ir persiguiéndolas por todo el archivo. */
const PILDORA =
  'block w-full py-4 px-6 text-center bg-white dark:bg-superficie ' +
  'hover:bg-gray-50 dark:hover:bg-superficie-alta text-gray-900 dark:text-white ' +
  'font-medium rounded-full border border-gray-200 dark:border-white/5 ' +
  'shadow-sm transition active:scale-[0.98]';

const TARJETA =
  'aparecer rounded-3xl bg-white dark:bg-superficie border border-gray-200 ' +
  'dark:border-white/5 shadow-sm p-4';

const TARJETA_VIP =
  'aparecer rounded-3xl bg-white dark:bg-superficie border-2 border-vip shadow-sm shadow-vip/20 p-4';

function crear(etiqueta, clases, contenido) {
  const nodo = document.createElement(etiqueta);
  if (clases) nodo.className = clases;
  if (contenido != null) nodo.textContent = contenido;
  return nodo;
}

/* Lucide no trae logos de marca (instagram, tiktok...), así que esos
   pocos se dibujan a mano como SVG en vez de caer en el fallback de
   sanearIconosFaltantes(). Se arman con createElementNS, no innerHTML,
   por la misma razón que el resto del archivo evita innerHTML. */
const NS_SVG = 'http://www.w3.org/2000/svg';

const ICONOS_MARCA = {
  instagram:
    'M12 0C8.74 0 8.333.014 7.053.072 5.775.132 4.905.333 4.14.63c-.789.306-1.459.717-2.126 1.384S.935 3.35.63 4.14C.333 4.905.131 5.775.072 7.053.014 8.333 0 8.74 0 12s.014 3.667.072 4.947c.06 1.277.261 2.148.558 2.913.306.788.717 1.459 1.384 2.126.667.666 1.336 1.079 2.126 1.384.766.296 1.636.499 2.913.558C8.333 23.986 8.74 24 12 24s3.667-.014 4.947-.072c1.277-.06 2.148-.262 2.913-.558.788-.306 1.459-.718 2.126-1.384.666-.667 1.079-1.335 1.384-2.126.296-.765.499-1.636.558-2.913.058-1.28.072-1.687.072-4.947s-.014-3.667-.072-4.947c-.06-1.277-.262-2.149-.558-2.913-.306-.789-.718-1.459-1.384-2.126C21.319 1.347 20.651.935 19.86.63c-.765-.297-1.636-.499-2.913-.558C15.667.014 15.26 0 12 0zm0 2.16c3.203 0 3.585.016 4.85.071 1.17.055 1.805.249 2.227.415.562.217.96.477 1.382.896.419.42.679.819.896 1.381.164.422.36 1.057.413 2.227.057 1.266.07 1.646.07 4.85s-.015 3.585-.074 4.85c-.061 1.17-.256 1.805-.421 2.227-.224.562-.479.96-.899 1.382-.419.419-.824.679-1.38.896-.42.164-1.065.36-2.235.413-1.274.057-1.649.07-4.859.07-3.211 0-3.586-.015-4.859-.074-1.171-.061-1.816-.256-2.236-.421-.569-.224-.96-.479-1.379-.899-.421-.419-.69-.824-.9-1.38-.165-.42-.359-1.065-.42-2.235-.045-1.26-.061-1.649-.061-4.844 0-3.196.016-3.586.061-4.861.061-1.17.255-1.814.42-2.234.21-.57.479-.96.9-1.381.419-.419.81-.689 1.379-.898.42-.166 1.051-.361 2.221-.421 1.275-.045 1.65-.06 4.859-.06l.045.03zM12 5.838c-3.405 0-6.162 2.76-6.162 6.162 0 3.405 2.76 6.162 6.162 6.162 3.405 0 6.162-2.76 6.162-6.162 0-3.405-2.76-6.162-6.162-6.162zM12 16c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4zm7.846-10.405c0 .795-.646 1.44-1.44 1.44-.795 0-1.44-.646-1.44-1.44 0-.794.646-1.439 1.44-1.439.793 0 1.44.645 1.44 1.439z',
  tiktok:
    'M16.6 5.82c-.94-.84-1.53-2.02-1.6-3.32V2h-3.09v13.4a2.592 2.592 0 0 1-4.65 1.56 2.592 2.592 0 0 1 2.65-4.06V9.66a5.85 5.85 0 0 0-1.19-.12A5.85 5.85 0 0 0 2.87 15.4 5.85 5.85 0 0 0 8.71 21.24a5.85 5.85 0 0 0 5.85-5.85V9.01a7.35 7.35 0 0 0 4.31 1.38V7.3a4.28 4.28 0 0 1-2.27-1.48z'
};

function iconoMarca(nombreIcono, clases) {
  const svg = document.createElementNS(NS_SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'currentColor');
  if (clases) svg.setAttribute('class', clases);
  const trazo = document.createElementNS(NS_SVG, 'path');
  trazo.setAttribute('d', ICONOS_MARCA[nombreIcono]);
  svg.append(trazo);
  return svg;
}

function icono(nombre, clases = 'w-5 h-5') {
  if (ICONOS_MARCA[nombre]) return iconoMarca(nombre, clases);
  const nodo = crear('i', clases);
  nodo.setAttribute('data-lucide', nombre);
  return nodo;
}

function enlaceExterno(url) {
  const nodo = crear('a');
  nodo.href = url;
  nodo.target = '_blank';
  nodo.rel = 'noopener noreferrer';
  return nodo;
}

function vaciar(contenedor) {
  contenedor.replaceChildren();
}

/* Lucide sustituye cada <i data-lucide> por un <svg>. Los que quedan
   sin sustituir son nombres que no existen (Lucide retiró los logos de
   marca: instagram, facebook, twitter...). En vez de dejar un botón en
   blanco, se pone la inicial de su etiqueta. */
function sanearIconosFaltantes() {
  document.querySelectorAll('i[data-lucide]').forEach((nodo) => {
    const etiqueta = nodo.closest('[aria-label], [title]');
    const inicial = (
      etiqueta?.getAttribute('aria-label') ||
      etiqueta?.getAttribute('title') ||
      '?'
    ).trim().charAt(0).toUpperCase();
    const repuesto = crear(
      'span',
      `${nodo.className} inline-flex items-center justify-center text-xs font-black`,
      inicial
    );
    nodo.replaceWith(repuesto);
  });
}

export function refrescarIconos() {
  if (window.lucide) window.lucide.createIcons();
  sanearIconosFaltantes();
}

/* ------------------------------ tiempo ------------------------------ */

const RELATIVO = new Intl.RelativeTimeFormat('es', { numeric: 'auto' });
const UNIDADES = [
  ['year', 31536000],
  ['month', 2592000],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60]
];

export function tiempoRelativo(isoFecha) {
  const segundos = (Date.parse(isoFecha) - Date.now()) / 1000;
  for (const [unidad, tamano] of UNIDADES) {
    if (Math.abs(segundos) >= tamano) {
      return RELATIVO.format(Math.round(segundos / tamano), unidad);
    }
  }
  return 'recién';
}

/* ------------------------------ canales ------------------------------ */

function botonCanal(canal) {
  const boton = enlaceExterno(canal.url);
  boton.className = PILDORA;
  boton.append(crear('span', 'block', canal.nombre));
  if (canal.descripcion) {
    boton.append(
      crear(
        'span',
        'block mt-0.5 text-xs font-normal text-gray-500 dark:text-neutral-400',
        canal.descripcion
      )
    );
  }
  return boton;
}

function botonVip(vip) {
  const boton = enlaceExterno(vip.url);
  boton.className =
    'relative flex flex-col items-center justify-center w-full py-4 pl-6 pr-16 mt-2 ' +
    'text-center bg-vip hover:brightness-95 text-vip-texto font-bold rounded-full transition ' +
    'active:scale-[0.98] overflow-hidden shadow-lg shadow-vip/20';

  boton.append(crear('span', null, vip.nombre));
  if (vip.descripcion) {
    boton.append(crear('span', 'block mt-0.5 text-[11px] font-semibold opacity-80', vip.descripcion));
  }

  const insignia = crear(
    'span',
    'absolute right-2 top-1/2 -translate-y-1/2 w-11 h-11 bg-marca rounded-2xl ' +
      'flex items-center justify-center rotate-[8deg] shadow-md'
  );
  insignia.append(icono(vip.icono, 'w-6 h-6 text-white -rotate-[8deg]'));
  boton.append(insignia);

  return boton;
}

export function renderCanales(contenedor, canales, vip) {
  vaciar(contenedor);

  if (!canales.length && !vip) {
    contenedor.append(
      crear(
        'p',
        'text-sm text-gray-500 dark:text-neutral-500 py-6',
        'Añade tus canales en data/config.json para que aparezcan aquí.'
      )
    );
    return;
  }

  canales.forEach((canal) => contenedor.append(botonCanal(canal)));
  if (vip) {
    contenedor.append(botonVip(vip));
    if (vip.nota) {
      contenedor.append(
        crear('p', 'text-center text-[11px] text-gray-500 dark:text-neutral-500 -mt-1.5', vip.nota)
      );
    }
  }
}

/* ------------------------------ redes ------------------------------ */

export function renderRedes(contenedor, redes) {
  vaciar(contenedor);
  redes.forEach((red) => {
    const boton = enlaceExterno(red.url);
    boton.className =
      'w-11 h-11 rounded-full bg-white dark:bg-superficie border border-gray-200 ' +
      'dark:border-white/5 flex items-center justify-center text-gray-600 ' +
      'dark:text-neutral-300 hover:text-marca dark:hover:text-white transition ' +
      'active:scale-95';
    boton.setAttribute('aria-label', red.nombre);
    boton.append(icono(red.icono, 'w-[18px] h-[18px]'));
    contenedor.append(boton);
  });
}

/* ------------------------------ cupón ------------------------------ */

async function copiarAlPortapapeles(valor) {
  try {
    await navigator.clipboard.writeText(valor);
    return true;
  } catch {
    /* clipboard exige contexto seguro (https o localhost). Si no lo
       hay, se cae a la selección manual del texto. */
    return false;
  }
}

function bloqueCupon(cupon) {
  const fila = crear(
    'div',
    'mt-3 flex items-stretch gap-2 rounded-2xl border border-dashed ' +
      'border-gray-300 dark:border-neutral-700 p-1.5'
  );

  const codigo = crear(
    'code',
    'flex-1 px-3 py-2 font-mono text-sm font-bold tracking-wider ' +
      'text-gray-900 dark:text-white truncate select-all',
    cupon
  );

  const boton = crear(
    'button',
    'shrink-0 px-3 py-2 rounded-xl bg-marca hover:bg-marca-oscuro text-white ' +
      'text-xs font-bold transition active:scale-95 flex items-center gap-1.5'
  );
  boton.type = 'button';
  boton.append(icono('copy', 'w-3.5 h-3.5'), crear('span', null, 'Copiar'));

  boton.addEventListener('click', async () => {
    const copiado = await copiarAlPortapapeles(cupon);
    vaciar(boton);
    boton.append(
      icono(copiado ? 'check' : 'alert-circle', 'w-3.5 h-3.5'),
      crear('span', null, copiado ? '¡Listo!' : 'Selecciona')
    );
    refrescarIconos();

    if (!copiado) {
      /* Sin permiso de portapapeles, al menos se deja el código
         seleccionado para que baste con Ctrl+C. */
      const rango = document.createRange();
      rango.selectNodeContents(codigo);
      const seleccion = window.getSelection();
      seleccion.removeAllRanges();
      seleccion.addRange(rango);
    }

    setTimeout(() => {
      vaciar(boton);
      boton.append(icono('copy', 'w-3.5 h-3.5'), crear('span', null, 'Copiar'));
      refrescarIconos();
    }, 2000);
  });

  fila.append(codigo, boton);
  return fila;
}

/* ------------------------------ ofertas ------------------------------ */

function tarjetaOferta(oferta) {
  const esVip = oferta.canal === 'ofertas_vip';
  const tarjeta = crear('article', esVip ? TARJETA_VIP : TARJETA);

  const cabecera = crear('div', 'flex items-start justify-between gap-3');
  const izquierda = crear('div', 'flex-1 min-w-0');

  const meta = crear(
    'div',
    'flex items-center gap-1.5 text-[11px] font-semibold uppercase ' +
      'tracking-wide text-gray-400 dark:text-neutral-500'
  );
  /* En oscuro se usa el tono claro de la marca: el tono base no tiene
     contraste suficiente sobre la superficie oscura. */
  if (oferta.comercio) {
    meta.append(crear('span', 'text-marca dark:text-marca-claro', oferta.comercio));
  }
  if (oferta.comercio && oferta.fecha) meta.append(crear('span', null, '·'));
  if (oferta.fecha) {
    const momento = crear('time', 'normal-case font-medium', tiempoRelativo(oferta.fecha));
    momento.dateTime = oferta.fecha;
    meta.append(momento);
  }
  if (meta.childNodes.length) izquierda.append(meta);

  izquierda.append(
    crear('h3', 'linea-2 mt-1 font-bold text-[15px] leading-snug text-gray-900 dark:text-white', oferta.titulo)
  );
  if (oferta.detalle) {
    izquierda.append(
      crear('p', 'linea-2 mt-1 text-xs leading-relaxed text-gray-500 dark:text-neutral-400', oferta.detalle)
    );
  }
  cabecera.append(izquierda);

  if (oferta.descuento) {
    const insignias = crear('div', 'shrink-0 flex flex-col items-end gap-1');
    insignias.append(
      crear(
        'div',
        'px-3 py-1.5 rounded-xl bg-marca/10 dark:bg-marca/20 text-marca ' +
          'dark:text-marca-claro font-black text-sm whitespace-nowrap',
        oferta.descuento
      )
    );
    if (esVip) {
      insignias.append(
        crear(
          'span',
          'px-2 py-0.5 rounded-full bg-vip text-vip-texto text-[10px] font-black tracking-wide',
          'VIP'
        )
      );
    }
    cabecera.append(insignias);
  }
  tarjeta.append(cabecera);

  if (oferta.cupon) tarjeta.append(bloqueCupon(oferta.cupon));

  if (oferta.url) {
    const enlace = enlaceExterno(oferta.url);
    enlace.className =
      'mt-3 inline-flex items-center gap-1 text-xs font-bold text-marca ' +
      'dark:text-marca-claro hover:underline';
    const textoEnlace = oferta.comercio ? `Ver en ${oferta.comercio}` : 'Ver oferta';
    enlace.append(crear('span', null, textoEnlace), icono('arrow-up-right', 'w-3.5 h-3.5'));
    tarjeta.append(enlace);
  }

  return tarjeta;
}

function estadoVacio(mensaje) {
  const caja = crear('div', 'text-center py-10 px-6');
  caja.append(
    icono('inbox', 'w-8 h-8 mx-auto text-gray-300 dark:text-neutral-700'),
    crear('p', 'mt-3 text-sm text-gray-500 dark:text-neutral-500', mensaje)
  );
  return caja;
}

export function renderOfertas(contenedor, datos) {
  vaciar(contenedor);

  if (datos.error) {
    contenedor.append(
      estadoVacio('No pudimos cargar las ofertas ahora mismo. Vuelve a intentarlo en un rato.')
    );
    return;
  }

  if (!datos.ofertas.length) {
    contenedor.append(estadoVacio('Todavía no hay ofertas publicadas. ¡Vuelve pronto!'));
    return;
  }

  const cabecera = crear('div', 'flex items-baseline justify-between mb-3 px-1');
  cabecera.append(
    crear('h2', 'text-lg font-black text-gray-900 dark:text-white', 'Últimas ofertas')
  );
  if (datos.actualizado) {
    cabecera.append(
      crear(
        'span',
        'text-[11px] text-gray-400 dark:text-neutral-500',
        `Actualizado ${tiempoRelativo(datos.actualizado)}`
      )
    );
  }
  contenedor.append(cabecera);

  const lista = crear('div', 'space-y-3');
  datos.ofertas.forEach((oferta) => lista.append(tarjetaOferta(oferta)));
  contenedor.append(lista);
}
