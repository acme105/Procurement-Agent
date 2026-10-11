// Shell: top bar, two-item nav, hash router. Views live in /web/views/<name>.js and export render(root).
import { h, errorBox } from './components.js';

const ROUTES = [
  ['live', 'List of RFQs', 'Build by chat'],
  ['compare-live', 'Compare quotes', 'Proposals and analyst'],
];
const DEFAULT = 'live';

function renderTop() {
  document.getElementById('topbar').replaceChildren(
    h('img', { class: 'brand-logo', src: '/web/logo.svg', alt: 'Procurement' }),
    h('strong', {}, 'Procurement'),
    h('div', { class: 'event' }, h('span', { class: 'meta' }, 'Sourcing · RFQ co-pilot and quote comparison')));
}

function renderNav(active) {
  document.getElementById('nav').replaceChildren(...ROUTES.map(([id, label]) =>
    h('a', { href: `#/${id}`, class: id === active ? 'active' : '' }, h('span', {}, label))));
}

async function route() {
  const id = location.hash.replace(/^#\//, '').split('?')[0] || DEFAULT;
  const known = ROUTES.find(r => r[0] === id) ? id : DEFAULT;
  renderNav(known);
  const root = document.getElementById('view');
  root.classList.remove('fill-view');
  root.replaceChildren(h('div', { class: 'empty' }, 'Loading…'));
  try {
    const mod = await import(`./views/${known}.js`);
    await mod.render(root);
  } catch (e) {
    root.replaceChildren(errorBox(e));
  }
}

renderTop();
window.addEventListener('hashchange', route);
route();
