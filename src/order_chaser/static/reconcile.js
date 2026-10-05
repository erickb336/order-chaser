/* The restart reconcile (Q5): what the tool found on Kraken for the live chase that ran when the tool stopped.
   While it reads Kraken: the state from the stream. After it: the chase from /api/chase/<id>. */
OC.chrome('reconcile');
OC.setMode(true, 'LIVE: the tool checks a live chase that ran when the tool stopped.');
const $ = OC.$;
const id = new URLSearchParams(location.search).get('id');
const name = c => `${OC.esc(OC.orderName(c))} · ${new Date(c.started * 1000).toLocaleTimeString('en-GB')}`;

function render(c, reading) {
  const B = OC.esc(c.pair.base), f = Number(c.filled), q = Number(c.qty), pct = OC.pct(f, q);
  const filled = `${OC.qty(f)} of ${OC.qty(q)} ${B} (${pct}%)`;
  let st, head, sub, row, tone, act;
  if (reading && reading.error) {
    [st, tone] = ['Cannot read Kraken', 'bad'];
    head = 'The tool cannot see what happened yet';
    sub = `Attempt ${reading.attempt}${reading.next_in ? `, next try in ${reading.next_in} s` : ''}: ${OC.esc(reading.error)} Your order can still be open on Kraken. The last safety timer ran out within 60 s of the stop, so Kraken cancelled every order that rested then. An order can only be open if Kraken did not get that timer.`;
    row = ['Chase order', 'Unknown: not read yet', 'tone-bad'];
    act = '<button class="btn" disabled>New live chase</button><span class="small muted">Blocked until the tool reads Kraken.</span>';
  } else if (reading) {
    [st, tone] = ['Reading Kraken', 'you'];
    head = 'The tool stopped during a live chase';
    sub = 'It reads your orders and fills on Kraken with our order ids (cl_ord_id), to see what happened. New live chases wait until this ends.';
    row = null; act = '';
  } else {
    const all = f >= q;
    const F = {
      open: ['Found: still open', 'warn', 'Your order was still open on Kraken. The tool cancelled it.', `The tool started again before the safety timer fired, so your other orders are untouched. The order rested without control, so the tool cancelled it at once. ${filled} filled.`, 'Was open. Cancelled by the tool at the restart.'],
      timer: ['Found: cancelled by the timer', 'bad', 'The safety timer cancelled your order', `Kraken cancelled all your orders within 60 s of the stop. ${f > 0 ? filled + ' filled before that.' : 'Nothing filled.'} The tool did not send the IOC: it never sends an order after a restart without you.`, 'Cancelled by the safety timer.'],
      closed: [all ? 'Found: filled' : 'Found: closed', all ? 'fill' : 'warn', all ? 'Your order filled while the tool was down' : 'Your order was closed on Kraken', `${filled} filled. The tool recorded the fills from Kraken.${all ? ' Nothing more to do.' : ''}`, 'Closed on Kraken.'],
    }[c.found] || ['Ended', 'bad', 'The chase ended at a restart', `${filled} filled.`, ''];
    [st, tone, head, sub] = F;
    row = ['Chase order', `${F[4]} Filled ${filled}`, all ? 'tone-fill' : 'tone-warn'];
    act = `<a class="btn primary" href="/result?id=${encodeURIComponent(c.id)}">See the result</a>` + (all ? '' : `<a class="btn" href="/new?rest=${encodeURIComponent(c.id)}">${OC.againText(c)}, new ${OC.words(c).limitWord}</a>`);
  }
  const ids = Object.entries(c.txids || {}).map(([leg, tx]) => `<span class="mono">${OC.esc(leg)}</span> (Kraken <span class="mono">${OC.esc(tx)}</span>)`).join(', ');
  $('root').innerHTML = `<div class="status" id="statuscard" data-state="${reading ? (reading.error ? 'cantread' : 'checking') : c.found}">
      <div class="state tone-${tone} ${reading ? 'pulse' : ''}"><i></i>${st}</div><div class="head">${head}</div><p class="sub">${sub}</p>
      ${reading && reading.error ? `<div class="note bad small" style="margin-top:10px">To be sure now, open Kraken Pro, Orders, and cancel any open order on ${OC.esc(c.pair.symbol)}. The tool blocks new live chases until it reads Kraken.</div>` : ''}
    </div>
    ${OC.livePanel({ ...c, phase: 'done', txids: {} })}
    <div class="card" style="margin-top:16px"><h2>${OC.esc(name(c))}</h2><table class="kv">
      ${row ? `<tr><td class="muted">${row[0]}</td><td class="${row[2]}">${row[1]}</td></tr>` : '<tr><td class="muted">Chase order</td><td>reading the order…</td></tr>'}</table>
      <p class="tiny muted" style="margin:8px 0 0">Our order ids (cl_ord_id): ${ids || c.legs.map(l => `<span class="mono">${OC.esc(l)}</span>`).join(', ')}.</p></div>
    <div class="card" style="margin-top:16px"><h2>What the tool did after the restart</h2>${OC.eventLog(c)}</div>
    <div class="row" style="margin-top:16px">${act}</div>`;
  document.title = st + ' · Order chaser';
}

if (id) fetch('/api/chase/' + encodeURIComponent(id)).then(r => r.ok ? r.json() : null).then(c => {
  if (c) render(c, null); else $('root').innerHTML = '<div class="note bad">No such chase. <a href="/history">Open the history</a></div>';
});
OC.stream(s => {
  if (s.restart) render(s.restart, s.restart.reading);
  else if (!id && s.chase && s.chase.found) render(s.chase, null);
  else if (!id) $('root').innerHTML = '<div class="card"><h2>Nothing to check</h2><p class="muted">No live chase ran when the tool stopped. <a href="/new">New chase</a></p></div>';
});
