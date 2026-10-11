// Shared UI helpers for the Live RFQ and Compare quotes pages.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export async function api(path, { method = 'GET', body, form, raw = false } = {}) {
  const headers = {};
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
  const res = await fetch(path, { method, headers, body: payload });
  if (raw) return res;
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const err = new Error((data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : res.statusText);
    err.status = res.status; err.data = data; throw err;
  }
  return data;
}

// Indian grouping; null/undefined -> em dash (never ₹0)
export function inr(x, dp = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return '—';
  return '₹' + Number(x).toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

export function toast(msg, ms = 3500) {
  const t = h('div', { class: 'toast' }, msg);
  document.body.append(t);
  setTimeout(() => t.remove(), ms);
}

export function errorBox(err) {
  return h('div', { class: 'callout' }, h('strong', {}, `Error ${err.status || ''}. `), err.message);
}

// Hash query params: #/compare-live?id=RFQ-L-004
export function qs() { const q = location.hash.split('?')[1] || ''; return new URLSearchParams(q); }
export function setQs(patch, { replace = false } = {}) {
  const base = (location.hash.split('?')[0]) || '#/';
  const p = qs();
  for (const [k, v] of Object.entries(patch)) { if (v === null || v === undefined || v === '') p.delete(k); else p.set(k, v); }
  const s = p.toString();
  const url = base + (s ? '?' + s : '');
  if (replace) history.replaceState(null, '', url); else location.hash = url;
}

const IST = 'Asia/Kolkata';
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function istParts(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone: IST, year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).formatToParts(d).map(p => [p.type, p.value]));
  return { y: parts.year, m: MON[Number(parts.month) - 1], d: Number(parts.day), hh: parts.hour === '24' ? '00' : parts.hour, mm: parts.minute, ss: parts.second };
}
export function istDateTime(iso, { seconds = false, year = true } = {}) {
  if (!iso) return '—';
  const p = istParts(iso);
  return p ? `${p.d} ${p.m}${year ? ' ' + p.y : ''}, ${p.hh}:${p.mm}${seconds ? ':' + p.ss : ''}` : String(iso);
}

export function tabs(items, active, onPick) {
  return h('div', { class: 'tabs', role: 'tablist' }, ...items.map(([id, label, n]) =>
    h('button', { class: id === active ? 'active' : '', role: 'tab', 'aria-selected': id === active ? 'true' : 'false', onclick: () => onPick(id) },
      label, n !== undefined && n !== null ? h('span', { class: 'n' }, String(n)) : null)));
}

export function modal(title, body, actions = [], { sub } = {}) {
  const scrim = h('div', { class: 'modal-scrim', role: 'dialog', 'aria-modal': 'true' });
  const close = () => scrim.remove();
  const box = h('div', { class: 'modal' },
    h('div', { class: 'row', style: { justifyContent: 'space-between', alignItems: 'flex-start' } },
      h('div', {}, sub ? h('div', { class: 'mono small', style: { color: 'var(--primary)' } }, sub) : null, h('div', { class: 'h5' }, title)),
      h('button', { class: 'btn ghost sm', 'aria-label': 'Close', onclick: close }, '✕')),
    h('div', { class: 'mt16' }, body),
    actions.length ? h('div', { class: 'actions' }, ...actions.map(a => typeof a === 'function' ? a(close) : a)) : null);
  scrim.append(box);
  scrim.addEventListener('click', (e) => { if (e.target === scrim) close(); });
  document.body.append(scrim);
  return close;
}

export async function download(path, { method = 'GET', body, fallbackName = 'download' } = {}) {
  const res = await api(path, { method, body, raw: true });
  if (!res.ok) {
    let msg = res.statusText;
    try { const d = await res.json(); msg = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch { /* not json */ }
    const e = new Error(msg); e.status = res.status; throw e;
  }
  const blob = await res.blob();
  const cd = res.headers.get('content-disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
  const a = h('a', { href: URL.createObjectURL(blob), download: m ? decodeURIComponent(m[1]) : fallbackName });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}
