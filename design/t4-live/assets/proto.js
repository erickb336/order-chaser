/* T4 live-trading prototype: shared chrome and helpers. Static, no network. Every number is SAMPLE data. */
'use strict';
const OC = (() => {
const $ = id => document.getElementById(id);
const PAGES = [
  ['index', 'Map'], ['setup', '1 Setup'], ['mode', '2 Mode switch'], ['new', '3 Live confirm'],
  ['chase', '4 Live chase'], ['result', '5 Result'], ['reconcile', '6 Restart reconcile'], ['questions', 'Decisions']
];
// mode: 'dry' | 'live' | 'none' (no product chrome)
function chrome(page, mode) {
  const app = document.querySelector('.app');
  const strip = document.createElement('div');
  strip.className = 'proto';
  strip.innerHTML = '<div class="in"><span class="tag">T4 live prototype</span>' +
    PAGES.map(([p, t]) => `<a href="${p}.html" class="${p === page ? 'on' : ''}">${t}</a>`).join('') +
    '<span class="spacer"></span><span class="sample">SAMPLE DATA: no real key, no real order</span></div>';
  document.body.prepend(strip);
  if (mode === 'none') return;
  const top = document.createElement('div');
  top.className = 'top';
  const on = p => p.includes(page) ? 'on' : '';
  top.innerHTML = '<div class="brand"><span class="dot"></span>Order chaser</div>' +
    `<nav class="nav"><a href="new.html" class="${on(['new', 'chase', 'mode'])}">Chase</a><a href="result.html" class="${on(['result', 'reconcile'])}">History</a><a href="setup.html" class="${on(['setup'])}">Setup</a></nav>` +
    '<span class="spacer"></span><span id="modebadge"></span>' +
    '<span class="conn" id="conn" role="status"><i></i><span>Kraken feed: connected</span></span>' +
    '<span class="badge plain mono" title="The real host and port, read from the page address">127.0.0.1:5180</span>';
  app.prepend(top);
  setMode(mode);
}
function setMode(mode) {
  const b = $('modebadge');
  if (!b) return;
  b.innerHTML = mode === 'live' ? '<span class="badge live">LIVE: real orders</span>' : '<span class="badge dry">DRY RUN: no real orders</span>';
}
function conn(state, text) {
  const c = $('conn'); if (!c) return;
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
  const h = location.hash.slice(1);
  pick(states.some(s => s[0] === h) ? h : initial || states[0][0]);
  return pick;
}
// C7, Q9: a mock of the macOS Keychain prompt. The program name is neutral: macOS can name the Python program that runs the tool.
// ctx: 'setup' (the key test) or 'start' (the first live chase after a start of the tool). go: [[label, data-go]] sample buttons.
function macPrompt(ctx, go) {
  return '<div class="mac" role="dialog" aria-label="A macOS Keychain prompt (mock)">' +
    '<div class="tiny" style="color:#d4d7dd;margin-bottom:8px"><b>Your Mac shows a prompt like this</b> (mock: the words and the program name can differ)</div>' +
    '<div class="t">“python3” wants to use your confidential information stored in “Kraken API key (order-chaser)” in your keychain.</div>' +
    '<div style="color:#d4d7dd">To allow this, enter the “login” keychain password.</div>' +
    '<div class="b"><span>Always Allow</span><span>Deny</span><span class="p">Allow</span></div>' +
    '<p class="tiny" style="color:#d4d7dd;margin:12px 0 0">The prompt can name <b>python3</b>, the program that runs the tool, not “Order chaser”. Click <b>Allow</b>, not Always Allow. ' +
    (ctx === 'start' ? 'macOS asks once at each start of the tool. This is the first live chase since the tool started at 09:40 (sample).' : 'macOS asks again once at each start of the tool.') +
    ' The tool keeps the key only in memory until it stops.</p>' +
    '<div class="row" style="margin-top:12px;flex-wrap:wrap;gap:8px">' + go.map(([t, g]) => `<button class="btn sm" data-go="${g}">${t}</button>`).join('') + '</div></div>';
}
// C8: the other open orders that the safety timer cancelled. Stop-loss and take-profit orders come first. SAMPLE data.
const OTHER = [
  ['Stop-loss', 'ETH/USD', 'Sell 0.4000 ETH, trigger 2,310.00'],
  ['Take-profit', 'ETH/USD', 'Sell 0.4000 ETH, trigger 2,780.00'],
  ['Limit', 'SOL/USD', 'Buy 12.00 SOL at 138.50'],
];
function cancelled(when) {
  const prot = OTHER.filter(o => o[0] !== 'Limit').length;
  return `<div class="note bad small" style="margin-top:14px" role="status"><b>Kraken also cancelled ${OTHER.length} of your other open orders at ${when}</b>, ` +
    `${prot} of them stop-loss or take-profit orders. Your positions on ${[...new Set(OTHER.filter(o => o[0] !== 'Limit').map(o => o[1]))].join(', ')} have no stop-loss or take-profit now. Place them again in Kraken Pro. The tool does not place them for you.` +
    '<table class="kv" style="margin-top:8px"><tr><th style="text-align:left">Type</th><th style="text-align:left">Pair</th><th style="text-align:left">Order (sample)</th></tr>' +
    OTHER.map(([t, p, o]) => `<tr><td style="text-align:left">${t === 'Limit' ? t : '<b>' + t + '</b>'}</td><td style="text-align:left">${p}</td><td style="text-align:left">${o}</td></tr>`).join('') +
    '</table><div class="tiny" style="margin-top:6px">The tool read this list from Kraken: the orders that were open when the chase started and that Kraken cancelled with the timer.</div></div>';
}
const tl = rows => '<ul class="log">' + rows.map(([t, txt, cls]) => `<li><span class="ts">${t}</span><span class="${cls ? 'ev-' + cls : ''}">${txt}</span></li>`).join('') + '</ul>';
return { $, chrome, setMode, conn, switcher, tl, macPrompt, cancelled, OTHER };
})();
