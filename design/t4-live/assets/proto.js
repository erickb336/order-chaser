/* T4 live-trading prototype: shared chrome and helpers. Static, no network. Every number is SAMPLE data. */
'use strict';
const OC = (() => {
const $ = id => document.getElementById(id);
const PAGES = [
  ['index', 'Map'], ['setup', '1 Setup'], ['mode', '2 Mode switch'], ['new', '3 Live confirm'],
  ['chase', '4 Live chase'], ['result', '5 Result'], ['reconcile', '6 Restart reconcile'], ['questions', 'Questions']
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
const tl = rows => '<ul class="log">' + rows.map(([t, txt, cls]) => `<li><span class="ts">${t}</span><span class="${cls ? 'ev-' + cls : ''}">${txt}</span></li>`).join('') + '</ul>';
return { $, chrome, setMode, conn, switcher, tl };
})();
