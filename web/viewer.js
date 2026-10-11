// Proposal viewer: full-screen modal for one proposal file. PDF pages arrive as PNGs rendered on the server (2x),
// images as uploaded; spreadsheets and documents as a table or text. Zoom 25-400% (buttons, keys, Ctrl/Cmd + wheel),
// scroll both ways, drag to pan, page indicator and jump.
import { h, api, toast } from './components.js';

const ZMIN = 0.25, ZMAX = 4;

export async function openViewer(rid, n, { page, onRemove, rfq = false } = {}) {
  let meta;
  try { meta = await api(rfq ? `/api/live/rfqs/${rid}/rfq/view` : `/api/live/rfqs/${rid}/proposals/${n}/view`); } catch (e) { toast(e.message); return; }
  const isPdf = /\.pdf$/i.test(meta.filename);
  let zoom = 1, fit = true;

  const body = h('div', { class: 'viewer-body', tabindex: '0' });
  const zlabel = h('span', { class: 'mono small', style: { minWidth: '48px', textAlign: 'center' } }, '100%');
  const pageIn = h('input', { class: 'inline mono', style: { width: '52px', textAlign: 'center' }, value: '1' });
  const pageOf = h('span', { class: 'small muted' });
  const close = () => { scrim.remove(); document.removeEventListener('keydown', onKey); window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp); };

  const imgs = (meta.pages || []).map((src) => h('img', { src, draggable: 'false', alt: meta.filename,
    onload: (e) => { e.target.dataset.w = e.target.naturalWidth / (isPdf ? 2 : 1); if (fit) fitWidth(); else apply(); } }));
  if (meta.kind === 'pages') body.append(h('div', { class: 'viewer-pages' }, ...imgs));
  else if (meta.kind === 'table') {
    body.append(h('div', { class: 'viewer-doc' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ...meta.columns.map(c => h('th', {}, c)))),
      h('tbody', {}, ...meta.rows.map(r => h('tr', {}, ...r.map(c => h('td', {}, c ?? ''))))))));
  } else body.append(h('pre', { class: 'viewer-doc viewer-text' }, meta.text));

  function apply() {
    zlabel.textContent = `${Math.round(zoom * 100)}%`;
    if (meta.kind === 'pages') for (const im of imgs) im.style.width = `${(Number(im.dataset.w) || im.naturalWidth || 800) * zoom}px`;
    else body.firstChild.style.fontSize = `${13 * zoom}px`;
  }
  function setZoom(z, anchor) {
    const old = zoom;
    zoom = Math.min(ZMAX, Math.max(ZMIN, z)); fit = false;
    const ax = anchor ? anchor.x : body.clientWidth / 2, ay = anchor ? anchor.y : body.clientHeight / 2;
    const cx = (body.scrollLeft + ax) / old, cy = (body.scrollTop + ay) / old;   // keep the point under the cursor still
    apply();
    body.scrollLeft = cx * zoom - ax; body.scrollTop = cy * zoom - ay;
  }
  function fitWidth() {
    const w = Math.max(...imgs.map(im => Number(im.dataset.w) || 0), 0);
    zoom = w ? Math.min(ZMAX, Math.max(ZMIN, (body.clientWidth - 48) / w)) : 1; fit = true; apply();
  }
  function currentPage() {
    const top = body.scrollTop + body.clientHeight / 3;
    let p = 1; imgs.forEach((im, i) => { if (im.offsetTop <= top) p = i + 1; });
    return p;
  }
  function goPage(p) {
    const im = imgs[Math.min(imgs.length, Math.max(1, p)) - 1];
    if (im) body.scrollTop = im.offsetTop - 16;
  }
  body.addEventListener('scroll', () => { if (imgs.length) pageIn.value = String(currentPage()); });
  pageIn.addEventListener('change', () => goPage(Number(pageIn.value) || 1));
  body.addEventListener('wheel', (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const r = body.getBoundingClientRect();
    setZoom(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), { x: e.clientX - r.left, y: e.clientY - r.top });
  }, { passive: false });
  let drag = null;                                   // click-and-drag to pan
  body.addEventListener('mousedown', (e) => { if (e.button === 0 && meta.kind === 'pages') { drag = { x: e.clientX, y: e.clientY, l: body.scrollLeft, t: body.scrollTop }; body.classList.add('dragging'); } });
  const onMove = (e) => { if (drag) { body.scrollLeft = drag.l - (e.clientX - drag.x); body.scrollTop = drag.t - (e.clientY - drag.y); } };
  const onUp = () => { drag = null; body.classList.remove('dragging'); };
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onUp);
  function onKey(e) {
    if (e.key === 'Escape') close();
    else if ((e.key === '+' || e.key === '=') && !e.target.matches('input')) setZoom(zoom * 1.25);
    else if (e.key === '-' && !e.target.matches('input')) setZoom(zoom / 1.25);
  }
  document.addEventListener('keydown', onKey);

  const btn = (label, onclick, title) => h('button', { class: 'btn sm', title: title || label, onclick }, label);
  const bar = h('div', { class: 'viewer-bar' },
    h('div', { style: { minWidth: 0 } }, h('div', { class: 'strong' }, meta.org || ''), h('div', { class: 'meta', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, meta.filename)),
    h('div', { class: 'spacer' }),
    imgs.length > 1 ? h('div', { class: 'row' }, h('span', { class: 'small muted' }, 'Page'), pageIn, pageOf) : null,
    h('div', { class: 'row' }, btn('−', () => setZoom(zoom / 1.25), 'Zoom out (−)'), zlabel, btn('+', () => setZoom(zoom * 1.25), 'Zoom in (+)'),
      meta.kind === 'pages' ? btn('Fit width', fitWidth) : null, btn('100%', () => setZoom(1))),
    h('a', { class: 'btn sm', href: meta.file_url, download: meta.filename }, 'Download'),
    onRemove ? h('button', { class: 'btn sm danger', onclick: async () => { if (confirm(`Remove ${meta.org}'s proposal from this RFQ?`)) { close(); await onRemove(); } } }, 'Remove') : null,
    h('button', { class: 'btn ghost sm', 'aria-label': 'Close', onclick: close }, '✕'));
  pageOf.textContent = `/ ${imgs.length}`;
  const scrim = h('div', { class: 'viewer-scrim', role: 'dialog', 'aria-modal': 'true', 'aria-label': `${meta.org} proposal` },
    h('div', { class: 'viewer' }, bar, body));
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); });
  document.body.append(scrim);
  body.focus();
  apply();
  if (page) setTimeout(() => goPage(page), 400);
}
