/* T4 helper prototype: shared chrome and helpers. Static, no network. Every number, id and hash is SAMPLE data. */
'use strict';
const OC = (() => {
const $ = id => document.getElementById(id);
const PAGES = [
  ['index', 'Map'], ['setup', '1 Setup'], ['new', '2 Form (live)'], ['chase', '3 Touch and chase'],
  ['reconcile', '4 Restart reconcile'], ['compare', 'Stage options'], ['questions', 'Decisions']
];
// SAMPLE chase. One touch covers the order id, its amends and cancel, and the final IOC at the same cap (G25 2a).
const C = {
  chase: 'oc-0412', id1: 'oc-0412-a', id2: 'oc-0412-b', pair: 'BTC/USD', side: 'Buy', size: '0.0050 BTC',
  start: '62,410.20', cap: '62,418.50', exit: '60,500.00', value: '313.34', loss: '12.05',
  hash: '3f9a2c71', hashFull: '3f9a2c71 5e0d84b2 9c1a7f63 0be4c21e 77d2a9f0 4b8e13c5 a6f2d9e8 1c0be4c2',
  t0: '09:52:04', end: '09:54:04', gtd: '09:54:34', k1: 'OQX3T4-ABCDE-FGH7J2', k2: 'OB7K2M-QRSTU-VWX9Y4',
};
// The helper's limits (R5). Read-only on the page; a change needs Touch ID in the helper. SAMPLE values.
const LIM = { value: '500.00', loss: '20.00', day: '50.00', used: '18.40' };
function chrome(page, mode) {
  const app = document.querySelector('.app');
  const strip = document.createElement('div');
  strip.className = 'proto';
  strip.innerHTML = '<div class="in"><span class="tag">T4 helper prototype</span>' +
    PAGES.map(([p, t]) => `<a href="${p}.html" class="${p === page ? 'on' : ''}">${t}</a>`).join('') +
    '<span class="spacer"></span><span class="sample">SAMPLE DATA: no real key, no real order</span></div>';
  document.body.prepend(strip);
  if (mode === 'none') return;
  const top = document.createElement('div');
  top.className = 'top';
  const on = p => p.includes(page) ? 'on' : '';
  top.innerHTML = '<div class="brand"><span class="dot"></span>Order chaser</div>' +
    `<nav class="nav"><a href="new.html" class="${on(['new', 'chase'])}">Chase</a><a href="reconcile.html" class="${on(['reconcile'])}">History</a><a href="setup.html" class="${on(['setup'])}">Setup</a></nav>` +
    '<span class="spacer"></span><span id="modebadge"></span>' +
    '<span class="conn" id="helper" role="status"><i></i><span>Helper: ready</span></span>' +
    '<span class="conn" id="conn" role="status"><i></i><span>Kraken feed: connected</span></span>';
  app.prepend(top);
  setMode(mode);
}
function setMode(mode) {
  const b = $('modebadge'); if (!b) return;
  b.innerHTML = mode === 'live' ? '<span class="badge live">LIVE: real orders</span>' : '<span class="badge dry">DRY RUN: no real orders</span>';
}
function pill(id, state, text) {
  const c = $(id); if (!c) return;
  c.className = 'conn' + (state === 'bad' ? ' bad' : state === 'warn' ? ' warn' : '');
  c.lastChild.textContent = text;
}
// A state switcher. states: [[id, label, group?]]. onpick(id) draws.
function switcher(el, title, states, onpick, initial) {
  let groups = '', last = null;
  states.forEach(([id, label, g]) => {
    if (g && g !== last) { groups += `<span class="grp">${g}</span>`; last = g; }
    groups += `<button data-id="${id}">${label}</button>`;
  });
  el.innerHTML = `<div><span class="lbl">Prototype: ${title}</span><span class="small muted">Click a state. You can link one with #state.</span></div><div class="opts">${groups}</div>`;
  const pick = id => {
    el.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.id === id));
    history.replaceState(null, '', '#' + id);
    onpick(id);
  };
  el.onclick = e => { const b = e.target.closest('button[data-id]'); if (b) pick(b.dataset.id); };
  window.addEventListener('hashchange', () => pick(location.hash.slice(1)));
  document.addEventListener('click', e => { const g = e.target.closest('[data-go]'); if (g) pick(g.dataset.go); });
  const h = location.hash.slice(1);
  pick(states.some(s => s[0] === h) ? h : initial || states[0][0]);
  return pick;
}
// The text that the helper puts in the Touch ID sheet. It comes from the staged copy with this hash (R3, R6).
// macOS puts "<app> is trying to" before it, so the text starts with a verb.
function sheetText(kind) {
  if (kind === 'reconcile') return `check and close chase ${C.chase} on Kraken: read orders ${C.id1} and ${C.id2}, cancel them if open. Places nothing. Copy ${C.hash}.`;
  if (kind === 'cancel') return `cancel order ${C.id1} of chase ${C.chase} on Kraken. Places nothing. Copy ${C.hash}.`;
  return `place chase ${C.chase} on Kraken: ${C.side.toUpperCase()} ${C.size} on ${C.pair}, spot. Limit ${C.start} rising to cap ${C.cap} USD. Order ${C.id1}, then final IOC ${C.id2} at ${C.cap}. Up to 10 min. Copy ${C.hash}.`;
}
// A mock of the macOS Touch ID sheet. Touch ID only (G25 5a): there is no "Use Password" button.
function sheet(kind) {
  return '<div class="sheetwrap"><div class="sheetlbl"><b>Your Mac shows a sheet like this</b> (mock: macOS draws it; the layout can differ)</div>' +
    '<div class="sheet" role="dialog" aria-label="A macOS Touch ID sheet (mock)"><div class="ico">OC</div><div class="nm">OCSigner</div>' +
    `<div class="rs">“OCSigner” is trying to ${sheetText(kind)}</div>` +
    '<div class="fp" aria-hidden="true">Touch</div><div>Touch ID to allow this.</div><div class="bt"><span>Cancel</span></div></div></div>';
}
function touchNow(text) {
  return `<div class="touchnow" role="status"><div class="fp" aria-hidden="true">Touch</div><div><div class="big2">Touch ID on your Mac now</div><div class="small">${text}</div></div></div>`;
}
function limits(extra) {
  return '<div class="lim">' +
    `<span class="k">Max order value (size × limit + fees)</span><span class="v">${LIM.value} USD</span>` +
    `<span class="k">Max loss per trade, at the exit plan</span><span class="v">${LIM.loss} USD</span>` +
    `<span class="k">Daily loss stop (committed risk today)</span><span class="v">${LIM.day} USD</span>` +
    (extra || '') + '</div>';
}
const tl = rows => '<ul class="log">' + rows.map(([t, txt, cls]) => `<li><span class="ts">${t}</span><span class="${cls ? 'ev-' + cls : ''}">${txt}</span></li>`).join('') + '</ul>';
return { $, chrome, setMode, pill, switcher, tl, sheet, sheetText, touchNow, limits, C, LIM };
})();
