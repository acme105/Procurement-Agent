// Shell: top bar, two-item nav, hash router. Views live in /web/views/<name>.js and export render(root).
import { h, api, errorBox, signInAgain } from './components.js';

const ROUTES = [
  ['live', 'List of RFQs', 'Build by chat'],
  ['compare-live', 'Compare quotes', 'Proposals and analyst'],
];
const DEFAULT = 'live';

function renderTop(login) {
  document.getElementById('topbar').replaceChildren(
    h('img', { class: 'brand-logo', src: '/web/logo.svg', alt: 'Procurement' }),
    h('strong', {}, 'Procurement'),
    h('div', { class: 'event' }, h('span', { class: 'meta' }, 'Sourcing · RFQ co-pilot and quote comparison')),
    h('div', { class: 'spacer' }),
    login ? h('button', { class: 'btn sm', onclick: signOut }, 'Sign out') : null);
}

async function signOut() {
  try { await fetch('/api/auth/logout', { method: 'POST' }); } catch { /* leaving anyway */ }
  location.replace('/login?reason=signedout');
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

(async function start() {
  let me = { login_required: false };
  try { me = await api('/api/auth/me'); }
  catch (e) { if (e.status === 401) return signInAgain(e.data && e.data.reason === 'expired' ? 'expired' : 'missing'); /* offline: show the app and let each page report it */ }
  renderTop(me.login_required);
  window.addEventListener('hashchange', route);
  route();
})();
