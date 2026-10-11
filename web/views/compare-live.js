// Compare quotes (Live RFQ): the comparison for one finalised RFQ on the left, the analyst chat on the right.
// Proposals are the RFQ's uploads (one file per organisation). A run extracts them and saves the tables
// (app/live_compare.py); the analyst answers from those tables (app/live_analyst.py) and can add parameters.
import { h, api, toast, modal, tabs, inr, istDateTime, qs, setQs, download } from '../components.js';
import { openViewer } from '../viewer.js';

const TABS = [['vendor_summary', 'Summary'], ['line_matrix', 'Lines'], ['cherry_pick', 'Cherry-pick'], ['questionnaire', 'Q&A'],
  ['parameters', 'Custom'], ['derived', 'Analyst tables']];
const SUGGEST = ['Which compliant vendor is cheapest overall?', 'Why did the failing vendors fail?',
  'What else should we compare these proposals on?', 'Compare laptop warranty across vendors'];
const FZ = (w) => ({ left: '0px', width: `${w}px`, minWidth: `${w}px`, maxWidth: `${w}px` });
const NUM = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 });

function fmt(col, v) {
  if (v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v))) return '—';
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  if (typeof v === 'number') return /INR|Price|Cost/.test(col) ? inr(v, 0) : NUM.format(v);
  return String(v);
}
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Small, safe markdown for chat replies: escape first, then tables, lists, bold, code, paragraphs.
function md(text) {
  const lines = esc(text || '').split('\n'), out = [];
  const inline = (s) => s.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map(c => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      i--;
      out.push(`<table><tr>${head.map(c => `<th>${c}</th>`).join('')}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</table>`);
    } else if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l), items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, '')));
      i--;
      out.push(`<${ordered ? 'ol' : 'ul'}>${items.map(x => `<li>${x}</li>`).join('')}</${ordered ? 'ol' : 'ul'}>`);
    } else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  return out.join('');
}

export async function render(root) {
  const rfqs = await api('/api/live/compare/rfqs');
  let rid = qs().get('id') || (rfqs[0] && rfqs[0].id);
  if (!rid) {
    root.replaceChildren(h('div', { class: 'empty' }, 'No finalised RFQs yet. Finalise one in Live RFQ, then compare its quotes here.'));
    return;
  }
  let home = null, runId = null, run = null, tab = 'vendor_summary', derivedSel = null, chat = [], busy = false, polling = false;

  const left = h('div', { class: 'cmp-left' });
  const chatLog = h('div', { class: 'stack', style: { flex: 1, overflow: 'auto', gap: '10px', paddingRight: '4px' } });
  const input = h('input', { placeholder: 'Ask about the quotes…', style: { flex: 1, minWidth: 0 } });
  const sendBtn = h('button', { class: 'btn primary' }, 'Ask');
  const typing = h('div', { class: 'small muted', style: { visibility: 'hidden' } }, 'Ana is working…');
  const chatCard = h('div', { class: 'card cmp-chat', style: { display: 'flex', flexDirection: 'column', gap: '12px', padding: '16px' } },
    h('div', { class: 'row', style: { justifyContent: 'space-between' } },
      h('div', { class: 'row' }, h('div', { class: 'h5' }, 'Ana'), h('span', { class: 'pill xs tone-indigo' }, 'ANALYST')),
      h('button', { class: 'btn ghost sm', onclick: async () => { chat = (await api(`/api/live/rfqs/${rid}/analyst/reset`, { method: 'POST' })).chat; drawChat(); } }, 'New chat')),
    chatLog, typing, h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, input, sendBtn));
  root.classList.add('fill-view');                 // this view fills the window height; app.js removes the class on navigation
  root.replaceChildren(h('div', { class: 'cmp-grid' }, left, chatCard));

  async function load({ keepRun = false } = {}) {
    home = await api(`/api/live/rfqs/${rid}/compare`);
    if (!keepRun || !runId) runId = home.latest || (home.runs.length ? home.runs[home.runs.length - 1].run_id : null);
    run = runId ? await api(`/api/live/rfqs/${rid}/runs/${runId}`) : null;
    chat = (await api(`/api/live/rfqs/${rid}/analyst`)).chat;
    drawLeft(); drawChat();
    if (run && run.manifest.status === 'running') pollRun();
    if (qs().get('view') === 'rfq') openViewer(rid, 0, { rfq: true });
    const pv = Number(qs().get('view'));
    if (pv && home.proposals.some(p => p.n === pv)) openProposal(pv, Number(qs().get('page')) || undefined);
  }

  const byOrg = () => Object.fromEntries(home.proposals.map(p => [p.org.toLowerCase(), p]));
  const viewVendor = (org, page) => { const p = byOrg()[String(org).toLowerCase()]; if (p) openProposal(p.n, page); else toast(`${org}'s file is no longer on this RFQ`); };
  const openProposal = (n, page) => openViewer(rid, n, { page, onRemove: async () => {
    try { await api(`/api/live/rfqs/${rid}/proposals/${n}`, { method: 'DELETE' }); toast('Proposal removed'); load({ keepRun: true }); } catch (e) { toast(e.message); }
  } });

  // ---- left: header, proposals, run, stats, tabs ----
  function drawLeft() {
    const m = run && run.manifest;
    const pick = h('select', { 'aria-label': 'RFQ', style: { maxWidth: '420px' }, onchange: (e) => { rid = e.target.value; runId = null; setQs({ id: rid }, { replace: true }); load(); } },
      ...rfqs.map(r => h('option', { value: r.id, selected: r.id === rid }, `${r.id} · ${(r.scope || 'Untitled').slice(0, 60)}`)));
    const inRun = new Map((m ? m.proposals : []).map(p => [p.n, p]));
    const changed = m ? home.proposals.filter(p => !inRun.has(p.n)).length + m.proposals.filter(p => !home.proposals.some(q => q.n === p.n)).length : 0;
    const bad = m ? m.proposals.filter(p => p.status === 'failed') : [];
    const head = h('div', { class: 'cmp-head' },
      pick,
      h('div', { class: 'cmp-btns' },
        h('button', { class: 'btn sm', onclick: () => openViewer(rid, 0, { rfq: true }) }, 'View RFQ'),
        h('button', { class: 'btn sm', onclick: addProposal }, 'Add proposal'),
        h('button', { class: 'btn primary sm', disabled: !home.proposals.length || (m && m.status === 'running'), onclick: runModal }, m ? 'Re-run' : 'Run comparison')));
    const meta = h('div', { class: 'row small muted', style: { gap: '12px' } },
      h('span', {}, `${home.lines} lines · ${home.questions.length} questions · ${home.proposals.length} proposal${home.proposals.length === 1 ? '' : 's'}`),
      home.runs.length > 1 ? h('select', { class: 'inline', 'aria-label': 'Run', onchange: async (e) => { runId = e.target.value; run = await api(`/api/live/rfqs/${rid}/runs/${runId}`); drawLeft(); } },
        ...home.runs.slice().reverse().map(r => h('option', { value: r.run_id, selected: r.run_id === runId }, `${r.run_id} · ${istDateTime(r.created_at, { year: false })}`))) : null,
      changed ? h('span', { class: 'pill xs tone-warning' }, `${changed} proposal change${changed > 1 ? 's' : ''} since this run`) : null,
      ...bad.map(p => h('button', { class: 'chip', title: p.error || '', onclick: () => openProposal(p.n) }, p.org, h('span', { class: 'pill xs tone-error' }, 'FAILED'))),
      !home.proposals.length ? h('span', {}, 'No proposals yet. Vendors upload through the RFQ link, or add one here.') : null);

    const parts = [head, meta];
    if (!m) parts.push(h('div', { class: 'card empty' }, home.proposals.length ? 'Run the comparison to extract the proposals and build the tables.' : 'Waiting for proposals.'));
    else if (m.status === 'running') {
      parts.push(h('div', { class: 'card stack' }, h('div', { class: 'h5' }, 'Extracting proposals…'),
        ...m.proposals.map(p => h('div', { class: 'row small' }, h('span', { class: `pill xs ${p.status === 'done' ? 'tone-success' : p.status === 'failed' ? 'tone-error' : 'tone-warning'}` }, p.status.toUpperCase()), p.org,
          p.error ? h('span', { class: 'muted' }, p.error) : null)),
        h('div', { class: 'small muted' }, 'About 1–2 minutes. Unchanged proposals come from the cache.')));
    } else if (m.status === 'failed' && !Object.keys(run.tables).length) {
      parts.push(h('div', { class: 'card', style: { background: 'var(--err-bg)' } }, `This run failed: ${m.error || 'no proposal could be extracted'}`));
    } else {
      const s = m.summary || {};
      const stat = (label, value, tone, tip) => h('span', { class: 'stat-i', title: tip || '' }, h('span', { class: 'muted' }, label), h('b', { style: tone ? { color: tone } : null }, value));
      parts.push(h('div', { class: 'stats-strip' },
        stat(`Best single source (${s.best_single_vendor || 'none passed'})`, fmt('INR', s.best_single_source)),
        stat('Cherry-pick', fmt('INR', s.cherry_pick_total), null, `${s.cherry_pick_basis}, before discounts`),
        stat('Saving', fmt('INR', s.savings_vs_best_single), (s.savings_vs_best_single || 0) > 0 ? 'var(--ok-fg)' : null),
        stat('Compliant', `${s.passing ?? 0} / ${s.vendors ?? 0}`)));
      const derived = Object.keys(run.tables).filter(n => n.startsWith('derived/'));
      if (tab === 'derived' && derived.length && (!derivedSel || !derived.includes(derivedSel))) derivedSel = derived[derived.length - 1];
      parts.push(h('div', { class: 'cmp-tabrow' },
        tabs(TABS.map(([id, label]) => [id, label, id === 'derived' ? derived.length : id === 'parameters' ? new Set((run.tables.parameters || []).map(r => r.Parameter)).size : undefined]), tab, (t) => { tab = t; drawLeft(); }),
        tab === 'derived' && !derived.length ? null : tableActions(tab === 'derived' ? derivedSel : tab)));
      parts.push(tabBody(derived));
    }
    left.replaceChildren(...parts);
  }

  function tableActions(name) {
    return h('div', { class: 'row', style: { flexWrap: 'nowrap', gap: '6px' } },
      name === 'line_matrix' ? h('span', { class: 'small muted' }, 'Lowest price per line is green') : null,
      h('button', { class: 'btn sm', onclick: () => download(`/api/live/rfqs/${rid}/runs/${runId}/tables/${name}?format=csv`).catch(e => toast(e.message)) }, 'CSV'),
      h('button', { class: 'btn sm', onclick: () => download(`/api/live/rfqs/${rid}/runs/${runId}/tables/${name}?format=xlsx`).catch(e => toast(e.message)) }, 'Excel'));
  }

  // frozen: { column: width } — those columns stay put while the table scrolls sideways (left offsets are cumulative).
  function freezer(cols, frozen = {}) {
    let left = 0, lastCol = null;
    const at = {};
    for (const c of cols) if (frozen[c]) { at[c] = left; left += frozen[c]; lastCol = c; }
    return (el, c) => {
      if (!(c in at)) return el;
      el.classList.add('fz'); if (c === lastCol) el.classList.add('fz-last');
      Object.assign(el.style, { left: `${at[c]}px`, width: `${frozen[c]}px`, minWidth: `${frozen[c]}px`, maxWidth: `${frozen[c]}px` });
      return el;
    };
  }
  function grid(rows, { cols, cell, headCell, frozen } = {}) {
    if (!rows || !rows.length) return h('div', { class: 'empty muted' }, 'No rows.');
    cols = cols || Object.keys(rows[0]);
    const fz = freezer(cols, frozen);
    return h('div', { class: 'cmp-table' }, h('table', { class: 'grid' },
      h('thead', {}, h('tr', {}, ...cols.map(c => fz(headCell ? headCell(c) : h('th', {}, c), c)))),
      h('tbody', {}, ...rows.map(r => h('tr', {}, ...cols.map(c => fz(cell ? cell(c, r[c], r) : td(c, r[c]), c)))))));
  }

  const td = (c, v) => typeof v === 'number' ? h('td', { class: 'num' }, fmt(c, v))
    : String(v ?? '').length > 40 ? h('td', { class: 'wrap' }, h('div', { class: 'clamp3', title: String(v) }, fmt(c, v))) : h('td', {}, fmt(c, v));
  const vendorLink = (org) => h('a', { href: '#', onclick: (e) => { e.preventDefault(); viewVendor(org); } }, org);

  function tabBody(derived) {
    const t = run.tables;
    if (tab === 'vendor_summary') {
      return h('div', { class: 'fill' }, grid(t.vendor_summary, {
        frozen: { Rank: 56, Vendor: 190 },
        cols: ['Rank', 'Vendor', 'Compliance Status', 'Final Landed Cost (INR)', 'Fail Reasons', 'Flags', 'Discount %', 'Lines Quoted', 'Quantity Shortfall',
          'Delivers to All Locations', 'Q&A Style', 'Avg QA Confidence / 10', 'Lead Time (Days)', 'Payment Terms', 'Freight Included', 'Bundle Conditions', 'Special Notes'],
        cell: (c, v) => c === 'Vendor' ? h('td', { style: { whiteSpace: 'nowrap' } }, vendorLink(v))
          : c === 'Compliance Status' ? h('td', {}, h('span', { class: `pill xs ${v === 'PASS' ? 'tone-success' : 'tone-error'}` }, v))
            : td(c, v) }));
    }
    if (tab === 'line_matrix') {
      const fixed = ['RFQ Line', 'Item', 'Location', 'Qty'];
      const rows = t.line_matrix || [];
      const vendors = rows.length ? Object.keys(rows[0]).filter(c => !fixed.includes(c)) : [];
      return h('div', { class: 'fill' },
        grid(rows, { cols: [...fixed, ...vendors], frozen: { 'RFQ Line': 70, Item: 280 },
          headCell: (c) => h('th', {}, vendors.includes(c) ? vendorLink(c) : c),
          cell: (c, v, r) => {
            if (!vendors.includes(c)) return h('td', { class: c === 'Item' ? 'wrap' : (typeof v === 'number' ? 'num' : '') }, fmt(c, v));
            const vals = vendors.map(x => r[x]).filter(x => typeof x === 'number');
            return h('td', { class: `num${typeof v === 'number' && v === Math.min(...vals) ? ' best' : ''}` }, typeof v === 'number' ? inr(v, 0) : '—');
          } }));
    }
    if (tab === 'cherry_pick') {
      return h('div', { class: 'fill' }, grid(t.cherry_pick, {
        frozen: { 'RFQ Line': 70, Item: 280 },
        cell: (c, v) => (c === 'Vendor' || c === 'Runner-up') && v ? h('td', { style: { whiteSpace: 'nowrap' } }, vendorLink(v)) : td(c, v) }));
    }
    if (tab === 'questionnaire') {
      const qa = t.questionnaire || [];
      const vendors = (t.vendor_summary || []).map(r => r.Vendor);
      const style = Object.fromEntries((t.vendor_summary || []).map(r => [r.Vendor, r['Q&A Style']]));
      const qrows = home.questions.map((q, i) => ({ n: i + 1, q }));
      if (qa.some(r => r['Question #'] === null)) qrows.unshift({ n: null, q: 'Blanket answer covering all questions' });
      return h('div', { class: 'fill' },
        h('div', { class: 'cmp-table' }, h('table', { class: 'grid' },
          h('thead', {}, h('tr', {}, h('th', { class: 'fz fz-last', style: FZ(320) }, 'Question'), ...vendors.map(v => h('th', {}, vendorLink(v))))),
          h('tbody', {}, ...qrows.map(({ n, q }) => h('tr', {}, h('td', { class: 'fz fz-last wrap', style: FZ(320) }, n ? `${n}. ${q}` : h('i', {}, q)),
            ...vendors.map(v => {
              const a = qa.find(r => r.Vendor === v && r['Question #'] === n);
              if (!a) return h('td', { class: 'small muted' }, style[v] === 'none' ? 'No answers' : style[v] === 'blanket' && n ? 'See blanket answer' : '—');
              const sc = a['Confidence Score'];
              return h('td', { class: 'wrap', title: a['Confidence Reasoning'] || '' }, a.Answer,
                h('div', {}, h('span', { class: `pill xs ${sc >= 8 ? 'tone-success' : sc >= 6 ? 'tone-warning' : 'tone-error'}` }, `${sc}/10`)));
            })))))));
    }
    if (tab === 'parameters') {
      const rows = t.parameters || [];
      if (!rows.length) return h('div', { class: 'card empty' }, 'No custom parameters yet. Ask Ana, e.g. “Compare laptop warranty across vendors” or “What else should we compare on?”.');
      const params = [...new Set(rows.map(r => r.Parameter))];
      const vendors = [...new Set(rows.map(r => r.Vendor))];
      return h('div', { class: 'fill' },
        h('div', { class: 'cmp-table' }, h('table', { class: 'grid' },
          h('thead', {}, h('tr', {}, h('th', { class: 'fz fz-last', style: FZ(220) }, 'Parameter'), ...vendors.map(v => h('th', {}, vendorLink(v))))),
          h('tbody', {}, ...params.map(p => h('tr', {}, h('td', { class: 'fz fz-last wrap', style: FZ(220), title: (rows.find(r => r.Parameter === p) || {}).Description || '' }, h('b', {}, p)),
            ...vendors.map(v => {
              const r = rows.find(x => x.Parameter === p && x.Vendor === v);
              if (!r || r.Value === null) return h('td', { class: 'small muted', title: r && r.Evidence ? r.Evidence : '' }, 'Not stated');
              return h('td', { class: 'wrap', title: r.Evidence || '' }, r.Value,
                h('div', { class: 'small muted' }, r.Evidence ? `“${String(r.Evidence).slice(0, 90)}${String(r.Evidence).length > 90 ? '…' : ''}”` : ''),
                h('span', { class: `pill xs ${r.Confidence === 'High' ? 'tone-success' : r.Confidence === 'Medium' ? 'tone-warning' : 'tone-error'}` }, r.Confidence));
            })))))));
    }
    if (!derived.length) return h('div', { class: 'card empty' }, 'Tables Ana saves while answering appear here, e.g. scenarios and filtered views.');
    return h('div', { class: 'fill' },
      h('div', { class: 'row' }, ...derived.map(n => h('button', { class: `chip${n === derivedSel ? ' on' : ''}`, onclick: () => { derivedSel = n; drawLeft(); } }, n.replace('derived/', '')))),
      grid(t[derivedSel]));
  }

  // ---- actions ----
  function runModal() {
    const prev = (run && run.manifest.settings) || { fx_usd_inr: 84, qa_threshold: 6 };
    const fx = h('input', { type: 'number', step: '0.01', value: prev.fx_usd_inr });
    const qa = h('input', { type: 'number', step: '0.1', min: '0', max: '10', value: prev.qa_threshold });
    modal('Run comparison', h('div', { class: 'stack' },
      h('div', { class: 'body-text' }, `Extracts ${home.proposals.length} proposals against ${home.lines} RFQ lines and rebuilds every table. Custom parameters are carried over.`),
      h('div', { class: 'grid2' }, h('div', { class: 'field' }, h('label', {}, 'USD → INR rate'), fx),
        h('div', { class: 'field' }, h('label', {}, 'Minimum QA score (out of 10)'), qa,
          h('div', { class: 'hint' }, 'Vendors answering question by question fail below this. No answers fail; one blanket answer passes with a flag.')))), [
      (close) => h('button', { class: 'btn', onclick: close }, 'Cancel'),
      (close) => h('button', { class: 'btn primary', onclick: async () => {
        try {
          const m = await api(`/api/live/rfqs/${rid}/runs`, { method: 'POST', body: { fx_usd_inr: Number(fx.value), qa_threshold: Number(qa.value) } });
          close(); runId = m.run_id; run = { manifest: m, tables: {} }; home = await api(`/api/live/rfqs/${rid}/compare`); drawLeft(); pollRun();
        } catch (e) { toast(e.message); }
      } }, 'Run'),
    ]);
  }

  async function pollRun() {
    if (polling) return;
    polling = true;
    const id = runId, r0 = rid;
    while (rid === r0 && runId === id && run && run.manifest.status === 'running' && document.body.contains(left)) {
      await new Promise(r => setTimeout(r, 2500));
      try { run = await api(`/api/live/rfqs/${rid}/runs/${id}`); drawLeft(); } catch { break; }
    }
    polling = false;
    if (rid === r0 && runId === id && run && run.manifest.status !== 'running') { toast(run.manifest.status === 'done' ? `Run ${id} finished` : `Run ${id} failed`); load({ keepRun: true }); }
  }

  function addProposal() {
    const org = h('input', { placeholder: 'e.g. Acme Traders Pvt Ltd', maxlength: '120' });
    const file = h('input', { type: 'file', accept: '.pdf,.xlsx,.xls,.csv,.docx,.doc,.png,.jpg,.jpeg,.txt' });
    modal('Add proposal', h('div', { class: 'stack' },
      h('div', { class: 'field' }, h('label', {}, 'Organisation name *'), org),
      h('div', { class: 'field' }, h('label', {}, 'Proposal file *'), file,
        h('div', { class: 'hint' }, 'One file per organisation. Adding another for the same organisation replaces it.'))), [
      (close) => h('button', { class: 'btn', onclick: close }, 'Cancel'),
      (close) => h('button', { class: 'btn primary', onclick: async () => {
        if (!org.value.trim() || !file.files[0]) { toast('Enter the organisation and pick a file'); return; }
        const form = new FormData(); form.append('org', org.value.trim()); form.append('file', file.files[0]);
        try { const r = await api(`/api/live/rfqs/${rid}/proposals`, { method: 'POST', form }); close(); toast(r.replaced.length ? `Replaced ${r.replaced.join(', ')}` : 'Proposal added'); load({ keepRun: true }); }
        catch (e) { toast(e.message); }
      } }, 'Add'),
    ]);
  }

  // ---- right: analyst chat ----
  function drawChat() {
    const ready = run && run.manifest.status !== 'running' && Object.keys(run.tables).length;
    const intro = chat.length ? [] : [h('div', { class: 'card tight' }, ready ? 'Hi, I’m Ana. Ask me anything about these quotes. I answer from the comparison tables and can add new parameters to compare on.' : 'Run the comparison first, then ask me about the quotes.'),
      ready ? h('div', { class: 'row' }, ...SUGGEST.map(s => h('button', { class: 'chip', onclick: () => { input.value = s; ask(); } }, s))) : null];
    chatLog.replaceChildren(...intro, ...chat.map(m => m.role === 'user'
      ? h('div', { class: 'bubble-q', style: { alignSelf: 'flex-end', maxWidth: '88%' } }, m.text)
      : h('div', { class: 'card tight chat-md', style: { maxWidth: '96%', overflowX: 'auto', ...(m.error ? { background: 'var(--err-bg)' } : {}) }, html: md(m.text) })));
    chatLog.scrollTop = chatLog.scrollHeight;
    input.disabled = sendBtn.disabled = !ready || busy;
  }

  async function ask() {
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = ''; busy = true; typing.style.visibility = 'visible'; typing.textContent = 'Ana is working…';
    chat = [...chat, { role: 'user', text }]; drawChat();
    try {
      const res = await fetch(`/api/live/rfqs/${rid}/analyst/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, run_id: runId }) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const reader = res.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, i); buf = buf.slice(i + 2);
          const ev = /event: (\w+)/.exec(block)?.[1], raw = /data: (.*)/s.exec(block)?.[1];
          if (!ev || !raw) continue;
          const data = JSON.parse(raw);
          if (ev === 'chat') { chat = data; drawChat(); }
          else if (ev === 'tool') typing.textContent = `Ana is using ${data.name.replace(/_/g, ' ')}…`;
          else if (ev === 'tables') {
            run = await api(`/api/live/rfqs/${rid}/runs/${runId}`);
            const d = data.find(n => n.startsWith('derived/'));
            if (d) { tab = 'derived'; derivedSel = d; } else if (data.includes('parameters')) tab = 'parameters';
            drawLeft();
          } else if (ev === 'open') openProposal(data.n, data.page);
        }
      }
    } catch (e) { toast(e.message); }
    busy = false; typing.style.visibility = 'hidden'; drawChat(); input.focus();
  }
  sendBtn.addEventListener('click', ask);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ask(); });

  await load();
}
