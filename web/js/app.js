/* ---------------------------------------------------------------
   Punto de entrada: tema, navegación y arranque.
   --------------------------------------------------------------- */

import { cargarConfig, cargarOfertas } from './data.js';
import { refrescarIconos, renderCanales, renderOfertas, renderRedes } from './render.js';

/* Pestañas del nav inferior. Para añadir una: se mete aquí, se añade su
   id a SECCIONES_ACTIVAS y se crea el <section id="section-…"> en
   index.html; el nav se regenera solo. Las que estén aquí pero no en
   SECCIONES_ACTIVAS se pintan atenuadas como "próximamente". */
const SECCIONES = [
  { id: 'home', icono: 'home', etiqueta: 'Inicio' }
];

const SECCIONES_ACTIVAS = ['home'];
const CLAVE_TEMA = 'tema';

let seccionActual = 'home';

/* ------------------------------ tema ------------------------------ */

function pintarIconoTema(esClaro) {
  const caja = document.getElementById('caja-icono-tema');
  if (!caja) return;
  const nodo = document.createElement('i');
  nodo.setAttribute('data-lucide', esClaro ? 'sun' : 'moon');
  nodo.className = 'w-5 h-5';
  caja.replaceChildren(nodo);
  refrescarIconos();
}

function aplicarTema(esClaro) {
  document.documentElement.classList.toggle('dark', !esClaro);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', esClaro ? '#F5F5F7' : '#0B0B0E');
  pintarIconoTema(esClaro);
}

/* Oscuro por defecto: solo se pone en claro si se guardó así. */
function iniciarTema() {
  aplicarTema(localStorage.getItem(CLAVE_TEMA) === 'claro');
}

function alternarTema() {
  const seraClaro = document.documentElement.classList.contains('dark');
  localStorage.setItem(CLAVE_TEMA, seraClaro ? 'claro' : 'oscuro');
  aplicarTema(seraClaro);
}

/* --------------------------- navegación --------------------------- */

function cambiarSeccion(id) {
  if (!SECCIONES_ACTIVAS.includes(id)) return;
  seccionActual = id;

  SECCIONES.forEach((seccion) => {
    document.getElementById(`section-${seccion.id}`)?.classList.toggle('hidden', seccion.id !== id);
    const boton = document.getElementById(`nav-${seccion.id}`);
    if (boton && !boton.disabled) boton.classList.toggle('opacity-100', seccion.id === id);
    if (boton && !boton.disabled) boton.classList.toggle('opacity-60', seccion.id !== id);
    boton?.setAttribute('aria-current', seccion.id === id ? 'page' : 'false');
  });

  document.getElementById('main-content')?.scrollTo({ top: 0 });
}

function renderNav() {
  const nav = document.getElementById('nav-inferior');
  if (!nav) return;
  nav.replaceChildren();

  SECCIONES.forEach((seccion) => {
    const activa = SECCIONES_ACTIVAS.includes(seccion.id);
    const boton = document.createElement('button');
    boton.type = 'button';
    boton.id = `nav-${seccion.id}`;
    boton.className = activa
      ? 'text-white transition transform hover:scale-110 ' +
        (seccion.id === seccionActual ? 'opacity-100' : 'opacity-60')
      : 'text-white opacity-25 cursor-not-allowed';
    boton.setAttribute('aria-label', seccion.etiqueta);
    boton.title = activa ? seccion.etiqueta : `${seccion.etiqueta} · próximamente`;

    if (activa) {
      boton.addEventListener('click', () => cambiarSeccion(seccion.id));
    } else {
      boton.disabled = true;
    }

    const nodo = document.createElement('i');
    nodo.setAttribute('data-lucide', seccion.icono);
    nodo.className = 'w-6 h-6';
    boton.append(nodo);
    nav.append(boton);
  });

  refrescarIconos();
}

/* ------------------------------ marca ------------------------------ */

function texto(id, valor) {
  const nodo = document.getElementById(id);
  if (nodo && valor) nodo.textContent = valor;
}

function aplicarMarca(marca) {
  const nombreCompleto = [marca.nombre, marca.emoji].filter(Boolean).join(' ');
  texto('marca-nombre', nombreCompleto);
  texto('marca-saludo', marca.saludo);
  texto('hero-titulo', marca.titulo);
  texto('hero-descripcion', marca.descripcion);
  document.title = `${nombreCompleto} | Ofertas y descuentos`;
}

/* ----------------------------- arranque ----------------------------- */

async function iniciar() {
  iniciarTema();
  renderNav();
  document.getElementById('boton-tema')?.addEventListener('click', alternarTema);

  const [config, ofertas] = await Promise.all([cargarConfig(), cargarOfertas()]);

  aplicarMarca(config.marca);
  renderCanales(document.getElementById('canales-lista'), config.canales, config.vip);
  renderRedes(document.getElementById('redes-lista'), config.redes);
  renderOfertas(document.getElementById('ofertas-feed'), ofertas);

  refrescarIconos();
}

iniciar();
