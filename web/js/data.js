/* ---------------------------------------------------------------
   Carga y normalización de los JSON.

   Regla de oro: los datos de ofertas.json vienen de mensajes de
   Telegram scrapeados, o sea que son irregulares y no confiables.
   Todo lo que sale de aquí ya viene limpio, validado y con los
   campos opcionales rellenos, para que render.js no tenga que
   defenderse de nada.
   --------------------------------------------------------------- */

const RUTA_CONFIG = './data/config.json';
const RUTA_OFERTAS = './data/ofertas.json';
const MAX_OFERTAS = 30;

/* Si config.json no carga, la página sigue en pie con esto. */
const CONFIG_MINIMA = {
  marca: {
    nombre: 'Descuentos Bigolito',
    emoji: '🐶',
    saludo: '',
    titulo: '¡Bienvenido!',
    descripcion: ''
  },
  canales: [],
  vip: null,
  redes: []
};

async function leerJSON(ruta) {
  const respuesta = await fetch(ruta, { cache: 'no-cache' });
  if (!respuesta.ok) throw new Error(`${ruta} respondió ${respuesta.status}`);
  return respuesta.json();
}

function esTextoUtil(valor) {
  return typeof valor === 'string' && valor.trim() !== '';
}

function texto(valor) {
  return esTextoUtil(valor) ? valor.trim() : null;
}

/* Solo se aceptan http(s). Un `javascript:...` colado en el JSON no
   puede terminar siendo un enlace ejecutable. */
function urlSegura(valor) {
  if (!esTextoUtil(valor)) return null;
  try {
    const url = new URL(valor.trim(), window.location.href);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function fechaValida(valor) {
  if (!esTextoUtil(valor)) return null;
  const marca = Date.parse(valor);
  return Number.isNaN(marca) ? null : new Date(marca).toISOString();
}

/* ------------------------------ config ------------------------------ */

function normalizarCanal(canal) {
  if (!canal || typeof canal !== 'object') return null;
  const url = urlSegura(canal.url);
  const nombre = texto(canal.nombre);
  if (!url || !nombre) return null;
  return { nombre, url, descripcion: texto(canal.descripcion) };
}

function normalizarConfig(datos) {
  const marca = datos && typeof datos.marca === 'object' && datos.marca ? datos.marca : {};
  const canales = Array.isArray(datos?.canales)
    ? datos.canales.map(normalizarCanal).filter(Boolean)
    : [];

  const vipCrudo = normalizarCanal(datos?.vip);
  const vip = vipCrudo
    ? { ...vipCrudo, icono: texto(datos.vip.icono) || 'crown', nota: texto(datos.vip.nota) }
    : null;

  const redes = Array.isArray(datos?.redes)
    ? datos.redes
        .map((red) => {
          const base = normalizarCanal(red);
          return base ? { ...base, icono: texto(red.icono) || 'link' } : null;
        })
        .filter(Boolean)
    : [];

  return {
    marca: {
      nombre: texto(marca.nombre) || CONFIG_MINIMA.marca.nombre,
      emoji: texto(marca.emoji) || '',
      saludo: texto(marca.saludo) || '',
      titulo: texto(marca.titulo) || CONFIG_MINIMA.marca.titulo,
      descripcion: texto(marca.descripcion) || ''
    },
    canales,
    vip,
    redes
  };
}

export async function cargarConfig() {
  try {
    return normalizarConfig(await leerJSON(RUTA_CONFIG));
  } catch (error) {
    console.warn('[data] No se pudo cargar config.json:', error.message);
    return CONFIG_MINIMA;
  }
}

/* ------------------------------ ofertas ------------------------------ */

function normalizarOferta(oferta) {
  if (!oferta || typeof oferta !== 'object') return null;

  /* id y título son obligatorios: sin ellos la tarjeta no tiene
     nada que mostrar ni forma de deduplicarse. El id se acepta como
     texto o como número, porque los ids de Telegram son numéricos. */
  const idNumerico = typeof oferta.id === 'number' && Number.isFinite(oferta.id);
  const id = idNumerico ? String(oferta.id) : texto(oferta.id);
  const titulo = texto(oferta.titulo);
  if (!id || !titulo) return null;

  return {
    id,
    titulo,
    descuento: texto(oferta.descuento),
    comercio: texto(oferta.comercio),
    categoria: texto(oferta.categoria),
    cupon: texto(oferta.cupon),
    detalle: texto(oferta.detalle),
    url: urlSegura(oferta.url),
    imagen: urlSegura(oferta.imagen),
    fecha: fechaValida(oferta.fecha),
    canal: texto(oferta.canal)
  };
}

/* Las ofertas sin fecha no se descartan: van al final de la lista y
   se muestran sin marca de tiempo. Es preferible a perderlas. */
function ordenarPorFecha(a, b) {
  const fechaA = a.fecha ? Date.parse(a.fecha) : -Infinity;
  const fechaB = b.fecha ? Date.parse(b.fecha) : -Infinity;
  return fechaB - fechaA;
}

export async function cargarOfertas() {
  try {
    const datos = await leerJSON(RUTA_OFERTAS);
    const crudas = Array.isArray(datos?.ofertas) ? datos.ofertas : [];

    /* Deduplicar por id: el mismo mensaje puede llegar por más de un
       canal si el scraper sigue varios a la vez. */
    const vistas = new Map();
    for (const oferta of crudas) {
      const limpia = normalizarOferta(oferta);
      if (limpia && !vistas.has(limpia.id)) vistas.set(limpia.id, limpia);
    }

    return {
      actualizado: fechaValida(datos?.actualizado),
      ofertas: [...vistas.values()].sort(ordenarPorFecha).slice(0, MAX_OFERTAS),
      error: false
    };
  } catch (error) {
    console.warn('[data] No se pudo cargar ofertas.json:', error.message);
    return { actualizado: null, ofertas: [], error: true };
  }
}
