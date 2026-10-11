// Live RFQ (C10): chat with Ana on the right, the RFQ redrawing in the centre as the agent calls its tools.
// Finalise locks the RFQ and prints the PDF; only then can it be emailed to typed addresses or downloaded.
import { h, api, toast, modal, download, istDateTime } from '../components.js';

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
let busy = false;

export async function render(root) {
  let v = await api('/api/live/current');
  let prev = new Set();
  const centre = h('div', { class: 'stack', style: { minWidth: 0 } });
  const chatLog = h('div', { class: 'stack', style: { flex: 1, overflow: 'auto', gap: '10px', paddingRight: '4px' } });
  const input = h('input', { class: 'full', placeholder: 'Describe what you need to buy…', style: { flex: 1 } });
  const sendBtn = h('button', { class: 'btn primary' }, 'Send');
  const typing = h('div', { class: 'small muted', style: { visibility: 'hidden' } }, 'Ana is working…');
  const chat = h('div', { class: 'card', style: { display: 'flex', flexDirection: 'column', gap: '12px', height: 'calc(100vh - 120px)', position: 'sticky', top: 0 } },
    h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('div', { class: 'h5' }, 'Ana'), h('span', { class: 'pill xs tone-indigo' }, 'CO-PILOT')),
    chatLog, typing, h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, input, sendBtn));
  root.replaceChildren(h('div', { style: { display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 380px', gap: '24px', alignItems: 'start' } }, centre, chat));

  function bubble(m) {
    return m.role === 'user'
      ? h('div', { class: 'bubble-q', style: { alignSelf: 'flex-end', maxWidth: '88%' } }, m.text)
      : h('div', { class: 'card tight chat-msg', style: { maxWidth: '100%', ...(m.error ? { background: 'var(--err-bg)' } : {}) } }, m.text);
  }
  function drawChat() {
    const welcome = v.chat.length ? [] : [h('div', { class: 'card tight' }, 'Hey there! I’m Ana. Tell me what you need to buy and I’ll build the RFQ as we talk.')];
    chatLog.replaceChildren(...welcome, ...v.chat.map(bubble));
    chatLog.scrollTop = chatLog.scrollHeight;
    const locked = v.status !== 'DRAFT';
    input.disabled = sendBtn.disabled = locked || busy;
    input.placeholder = locked ? 'RFQ finalised' : 'Describe what you need to buy…';
  }

  function draw() {
    const keys = new Set(), flash = (k) => { keys.add(k); return prev.has(k) ? null : 'flash'; };
    const final = v.status === 'FINAL';
    const lines = v.lines.length
      ? h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['#', 'Item', 'Location', 'Qty'].map((t, i) => h('th', { class: i === 3 ? 'num' : '' }, t)))),
        h('tbody', {}, ...v.lines.map(l => h('tr', { class: flash(`l${l.n}|${l.description}|${l.location}|${l.quantity}`) },
          h('td', { class: 'mono' }, l.n), h('td', {}, l.description, l.codes.length ? h('div', { class: 'meta mono' }, l.codes.join(', ')) : null),
          h('td', {}, l.location), h('td', { class: 'num' }, l.quantity)))))
      : h('div', { class: 'empty muted', style: { padding: '24px' } }, 'No line items yet.');
    const qs = v.questionnaire.length
      ? h('ol', { style: { margin: 0, paddingLeft: '20px' } }, ...v.questionnaire.map((q, i) => h('li', { class: flash(`q${i}|${q}`), style: { padding: '4px 0' } }, q)))
      : h('div', { class: 'muted' }, 'No questions yet.');
    prev = keys;
    const ready = !!v.scope && v.lines.length > 0;
    centre.replaceChildren(
      h('div', { class: 'row', style: { justifyContent: 'space-between' } },
        h('div', {}, h('div', { class: 'meta mono' }, v.id),
          h('div', { class: 'h4' }, v.scope || 'Untitled RFQ')),
        h('div', { class: 'row' }, h('span', { class: `pill ${final ? 'tone-success' : 'tone-warning'}` }, final ? 'FINAL' : 'DRAFT'),
          final ? h('a', { class: 'btn sm', href: `#/compare-live?id=${v.id}` }, 'Compare quotes') : null,
          h('button', { class: 'btn sm', onclick: history }, 'History'),
          h('button', { class: 'btn sm', onclick: newRfq }, 'New RFQ'))),
      h('div', { class: 'card pad0' }, lines),
      h('div', { class: 'card' }, h('div', { class: 'h5 mb8' }, 'Questions for suppliers'), qs),
      final ? sendPanel() : h('div', { class: 'row' },
        h('button', { class: 'btn primary', disabled: !ready || busy, onclick: finalise }, 'Finalise RFQ'),
        ready ? null : h('span', { class: 'small muted' }, 'Needs a scope and at least one line item.')));
    drawChat();
  }

  async function newRfq() {
    v = await api('/api/live/rfqs', { method: 'POST' }); prev = new Set(); send.emails = []; send.subject = send.message = null; draw();
  }

  async function history() {
    let rows;
    try { rows = await api('/api/live/rfqs'); } catch (e) { toast(e.message); return; }
    const body = rows.length
      ? h('div', { class: 'history' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['RFQ', 'Scope', 'Status', 'Lines', 'Sent', 'Replies', ''].map(t => h('th', {}, t)))),
        h('tbody', {}, ...rows.map(r => h('tr', {},
          h('td', { class: 'mono' }, r.id), h('td', { class: 'scope', title: r.scope || '' }, h('div', { class: 'clamp' }, r.scope || 'Untitled'), h('div', { class: 'meta' }, istDateTime(r.finalised_at || r.created_at, { year: false }))),
          h('td', {}, h('span', { class: `pill xs ${r.status === 'FINAL' ? 'tone-success' : 'tone-warning'}` }, r.status)),
          h('td', { class: 'num' }, r.lines), h('td', { class: 'num' }, r.sent), h('td', { class: 'num' }, r.replies),
          h('td', { class: 'row', style: { flexWrap: 'nowrap' } }, r.status === 'FINAL' ? h('a', { class: 'btn sm', href: `#/compare-live?id=${r.id}`, onclick: () => close() }, 'Compare') : null, h('button', { class: 'btn sm', onclick: async () => { close(); v = await api(`/api/live/rfqs/${r.id}/open`, { method: 'POST' }); prev = new Set(); send.emails = []; send.subject = send.message = null; draw(); } }, r.id === v.id ? 'Current' : 'Open')))))))
      : h('div', { class: 'empty muted' }, 'No RFQs yet.');
    const close = modal('RFQ history', body, [(c) => h('button', { class: 'btn', onclick: c }, 'Close')]);
  }

  function finalise() {
    modal('Finalise this RFQ?', h('div', { class: 'body-text' }, 'This locks the RFQ and prints the PDF. You won’t be able to chat or change it afterwards.'), [
      (close) => h('button', { class: 'btn', onclick: close }, 'Keep editing'),
      (close) => h('button', { class: 'btn primary', onclick: async () => {
        try { v = await api(`/api/live/rfqs/${v.id}/finalise`, { method: 'POST' }); close(); toast('RFQ finalised'); draw(); } catch (e) { toast(e.message); }
      } }, 'Finalise'),
    ]);
  }

  // ---- send panel (after finalising) ----
  const send = { emails: [], subject: null, message: null };
  function sendPanel() {
    send.subject ??= `Request for quotation: ${v.scope}`;
    send.message ??= `Hello,\n\nPlease find attached our request for quotation: ${v.scope}.\nKindly reply with your best prices, availability and answers to the supplier questions.\n\nThank you,\nProcurement`;
    const chips = h('div', { class: 'row' });
    const field = h('input', { placeholder: 'name@vendor.com, another@vendor.com', style: { flex: 1, minWidth: '240px' } });
    const err = h('div', { class: 'err small' });
    const subject = h('input', { value: send.subject, oninput: (e) => { send.subject = e.target.value; } });
    const message = h('textarea', { rows: 6, style: { height: 'auto', padding: '10px 12px' }, oninput: (e) => { send.message = e.target.value; } }, send.message);
    const go = h('button', { class: 'btn primary', disabled: !v.smtp_ready, onclick: submit }, 'Send emails');
    function drawChips() {
      chips.replaceChildren(...send.emails.map(e => h('span', { class: 'chip on' }, e, h('button', { 'aria-label': `Remove ${e}`, onclick: () => { send.emails = send.emails.filter(x => x !== e); drawChips(); } }, '✕'))));
    }
    function take() {
      const parts = field.value.split(/[\s,;]+/).filter(Boolean), bad = parts.filter(p => !EMAIL.test(p));
      for (const p of parts) if (EMAIL.test(p) && !send.emails.includes(p.toLowerCase())) send.emails.push(p.toLowerCase());
      field.value = bad.join(', '); err.textContent = bad.length ? `Not a valid email: ${bad.join(', ')}` : '';
      drawChips();
    }
    field.addEventListener('keydown', (e) => { if (['Enter', ',', ' '].includes(e.key) && field.value.trim()) { e.preventDefault(); take(); } });
    field.addEventListener('blur', take); field.addEventListener('paste', () => setTimeout(take, 0));
    async function submit() {
      take();
      if (!send.emails.length) { err.textContent = 'Add at least one email address.'; return; }
      go.disabled = true;
      try {
        v = await api(`/api/live/rfqs/${v.id}/send`, { method: 'POST', body: { emails: send.emails, subject: send.subject, message: send.message } });
        send.emails = []; toast('Sending in the background…'); draw(); poll();
      } catch (e) { toast(e.message); go.disabled = false; }
    }
    drawChips();
    return h('div', { class: 'card stack' },
      h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('div', { class: 'h5' }, 'Send RFQ'),
        h('button', { class: 'btn sm', onclick: () => download(`/api/live/rfqs/${v.id}/pdf`, { fallbackName: `${v.id}.pdf` }).catch(e => toast(e.message)) }, 'Download PDF')),
      h('div', { class: 'field' }, h('label', {}, 'Upload link to share yourself (WhatsApp, SMS, etc.)'),
        h('div', { class: 'row', style: { flexWrap: 'nowrap' } },
          h('input', { readonly: true, value: v.share_url || '', class: 'mono', style: { flex: 1, minWidth: 0 }, onclick: (e) => e.target.select() }),
          h('button', { class: 'btn sm', onclick: async () => { try { await navigator.clipboard.writeText(v.share_url); toast('Link copied'); } catch { toast('Select the link and copy it'); } } }, 'Copy link')),
        h('div', { class: 'hint' }, 'Download the PDF above and send it with this link. Vendors enter their organisation name when they upload.'),
        v.local_only ? h('div', { class: 'small', style: { color: 'var(--warn-fg)' } }, 'This link points to localhost, so it only opens on this computer. Set AERCHAIN_PUBLIC_URL to a public address first.') : null),
      h('div', { class: 'field' }, h('label', {}, 'Recipients'), chips, field, err,
        h('div', { class: 'hint' }, 'Type or paste addresses, separated by commas or spaces. The email includes the upload link.')),
      h('div', { class: 'field' }, h('label', {}, 'Subject'), subject),
      h('div', { class: 'field' }, h('label', {}, 'Message'), message),
      h('div', { class: 'row' }, go, v.smtp_ready ? null : h('span', { class: 'small muted' }, 'Email isn’t set up. Download the PDF and forward it yourself.')),
      v.sent.length ? h('div', {}, h('div', { class: 'small strong mb8' }, 'Sent so far'),
        ...v.sent.slice().reverse().map(s => h('div', { class: 'row small', style: { padding: '2px 0' } },
          h('span', { class: `pill xs ${s.status === 'sent' ? 'tone-success' : s.status === 'sending' ? 'tone-warning' : 'tone-error'}` }, s.status.toUpperCase()),
          s.email, h('span', { class: 'muted' }, istDateTime(s.at, { year: false })), s.error ? h('span', { class: 'muted' }, s.error) : null))) : null,
      h('div', {}, h('div', { class: 'small strong mb8' }, `Quotes received${v.uploads.length ? ` (${v.uploads.length})` : ''}`),
        ...(v.uploads.length ? v.uploads.slice().reverse().map(u => h('div', { class: 'row small', style: { padding: '2px 0' } },
          h('span', { class: 'pill xs tone-indigo' }, u.org),
          h('a', { href: `/api/live/rfqs/${v.id}/uploads/${u.n}`, download: u.filename }, u.filename),
          h('span', { class: 'muted' }, `${(u.size / 1024).toFixed(0)} KB · ${istDateTime(u.at, { year: false })}`)))
          : [h('div', { class: 'small muted' }, 'None yet. Reload the page to refresh.')])));

  }

  // While emails are going out, refresh the send log until none are still sending.
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    const id = v.id;
    while (v.id === id && v.sent.some(s => s.status === 'sending') && document.body.contains(centre)) {
      await new Promise(r => setTimeout(r, 1500));
      try { const n = await api(`/api/live/rfqs/${id}`); if (v.id === id) { v = n; draw(); } } catch { break; }
    }
    polling = false;
    if (v.id === id && v.sent.length && !v.sent.some(s => s.status === 'sending')) {
      const bad = v.sent.filter(s => s.status === 'failed').length;
      if (bad) toast(`${bad} email${bad > 1 ? 's' : ''} failed. See the list.`);
    }
  }

  // ---- chat stream ----
  async function say() {
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = ''; busy = true; typing.style.visibility = 'visible';
    v.chat = [...v.chat, { role: 'user', text }]; draw();
    try {
      const res = await fetch(`/api/live/rfqs/${v.id}/messages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
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
          const ev = /event: (\w+)/.exec(block)?.[1], data = /data: (.*)/s.exec(block)?.[1];
          if (ev === 'rfx' && data) { v = JSON.parse(data); draw(); }
        }
      }
    } catch (e) { toast(e.message); }
    busy = false; typing.style.visibility = 'hidden'; draw(); input.focus();
  }
  sendBtn.addEventListener('click', say);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') say(); });
  draw();
  poll();
}
