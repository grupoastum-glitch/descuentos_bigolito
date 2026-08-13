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

function icono(nombre, clases = 'w-5 h-5') {
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
  boton.textContent = canal.nombre;
  if (canal.descripcion) boton.title = canal.descripcion;
  return boton;
}

function botonVip(vip) {
  const boton = enlaceExterno(vip.url);
  boton.className =
    'relative flex items-center justify-center w-full py-4 mt-2 bg-vip ' +
    'hover:brightness-95 text-vip-texto font-bold rounded-full transition ' +
    'active:scale-[0.98] overflow-hidden shadow-lg shadow-vip/20';

  boton.append(crear('span', null, vip.nombre));

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
  if (vip) contenedor.append(botonVip(vip));
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
