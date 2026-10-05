/* Restart reconcile: order-chaser stopped during a live chase. Reading and closing the chase's own orders needs a touch. SAMPLE data. */
'use strict';
OC.chrome('reconcile', 'dry');
const { $, C } = OC;
const STATES = [
  ['touch', 'Needs your touch', 'Before'], ['reading', 'Reading Kraken (loading)'], ['declined', 'You cancelled the touch'],
  ['watchdog', 'Helper cancelled it', 'Result'], ['open', 'Still open: cancelled now'], ['expired', 'Expired at Kraken (GTD)'], ['filled', 'Filled before the stop'], ['cantread', 'Cannot read Kraken', 'Failure'],
];
const row = (a, b, c) => `<tr><td class="mono" style="text-align:left">${a}</td><td class="mono" style="text-align:left">${b}</td><td style="text-align:left">${c}</td></tr>`;
const R = {
  watchdog: { tone: 'fill', head: 'Closed. The helper cancelled your order when order-chaser stopped.', sub: `order-chaser stopped at 09:52:40. The helper saw it 5 s later and cancelled ${C.id1} at 09:52:45. Kraken confirmed. Filled 0.0018 of 0.0050 BTC (36%) before that.`, rows: [row(C.id1, C.k1, 'Filled 0.0018 BTC · cancelled by the helper 09:52:45'), row(C.id2, '—', 'Never sent')] },
  open: { tone: 'warn', head: 'Closed now. The order was still open.', sub: `The helper and order-chaser both stopped (the Mac slept). ${C.id1} was still open on Kraken. The helper cancelled it now with your touch (Q5). Filled 0.0018 BTC.`, rows: [row(C.id1, C.k1, 'Filled 0.0018 BTC · cancelled now 09:53:20'), row(C.id2, '—', 'Never sent')] },
  expired: { tone: 'warn', head: `Closed. Kraken removed the order at ${C.gtd}.`, sub: 'The helper and order-chaser both stopped. Kraken removed the order at its GTD expiry, 30 s after the chase end. Filled 0.0018 BTC.', rows: [row(C.id1, C.k1, 'Filled 0.0018 BTC · expired (GTD) ' + C.gtd), row(C.id2, '—', 'Never sent')] },
  filled: { tone: 'fill', head: 'Closed. The order filled before the stop.', sub: `Kraken shows ${C.id1} filled 100% at 09:52:39, before order-chaser stopped. Nothing to cancel.`, rows: [row(C.id1, C.k1, 'Filled 0.0050 BTC'), row(C.id2, '—', 'Never sent')] },
};
function view(s) {
  const card = (st, tone, head, sub, extra) => `<div class="status"><div class="state tone-${tone}"><i></i>${st}</div><div class="head">${head}</div><p class="sub">${sub}</p>${extra || ''}</div>`;
  let main;
  if (s === 'touch') main = card('Waiting for your touch', 'you', `Chase ${C.chase} was live when order-chaser stopped`, 'The tool must read its orders on Kraken and cancel one that is still open. The helper forgot the key when it stopped, so it needs your touch. Live stays blocked until this is done. Dry run works.',
    `<div style="margin-top:12px">${OC.touchNow(`The sheet asks to check and close chase ${C.chase}. It places nothing. Copy <span class="hash">${C.hash}</span>.`)}</div>`) + `<div style="margin-top:16px">${OC.sheet('reconcile')}</div>`;
  else if (s === 'reading') main = card('Reading Kraken', 'you', `Reading ${C.id1} and ${C.id2}…`, 'You touched Touch ID. The helper reads only these two order ids. About 1 to 2 s.', '<div class="skel" style="height:60px;margin-top:12px"></div>');
  else if (s === 'declined') main = card('Not checked', 'warn', 'You cancelled the touch', `The tool did not read Kraken. Chase ${C.chase} may still have an open order. Live stays blocked until you check it. Dry run works.`, '<div class="row" style="margin-top:12px"><a class="btn primary" href="#touch">Check now (Touch ID)</a></div><p class="tiny muted" style="margin:8px 0 0">You can also check the order in Kraken Pro. Search ' + C.k1 + '.</p>');
  else if (s === 'cantread') main = card('Cannot read Kraken', 'bad', 'The helper could not reach Kraken', `No answer for 30 s. The tool tries again every 10 s. Live stays blocked. Your order ${C.id1} expires at Kraken at ${C.gtd} at the latest.`, '');
  else { const r = R[s]; main = card('Closed', r.tone, r.head, r.sub, '<h3 style="margin-top:14px">Orders of this chase</h3><table class="kv"><tr><th style="text-align:left">Order id</th><th style="text-align:left">Kraken id</th><th style="text-align:left">Result</th></tr>' + r.rows.join('') + '</table><div class="note ok small" style="margin-top:12px">Your other orders on Kraken were not touched. The helper cancels only the ids of its own chase.</div><div class="row" style="margin-top:12px"><a class="btn" href="new.html#live">New chase</a></div>'); }
  const log = { touch: [['09:53:18', 'order-chaser started. Found live chase ' + C.chase + ' with no end. Asked Touch ID to check it.', 'warn'], ['09:52:40', 'order-chaser stopped during a live chase (last line of the old log).', 'bad']] }[s] || [['09:53:19', 'You touched Touch ID. Reading ' + C.id1 + ' and ' + C.id2 + '.'], ['09:53:18', 'order-chaser started. Found live chase ' + C.chase + ' with no end.', 'warn']];
  $('view').innerHTML = '<h1>Restart reconcile</h1><p class="muted">order-chaser stopped during a live chase. At the next start it checks that chase on Kraken.</p>' +
    `<div class="split"><div>${main}<div class="card" style="margin-top:16px"><h2>What the tool did</h2>${OC.tl(log)}</div></div>
    <aside class="side"><div class="card"><h3>Chase <span class="mono small">${C.chase}</span></h3><table class="kv"><tr><td class="muted">Order</td><td>${C.side} ${C.size} · spot</td></tr><tr><td class="muted">Started</td><td class="num">${C.t0}</td></tr><tr><td class="muted">Kraken expiry</td><td class="num">${C.gtd} (GTD)</td></tr></table></div>
    <div class="card"><h3>Live</h3><p class="small" style="margin:0">${['watchdog', 'open', 'expired', 'filled'].includes(s) ? '<span class="tone-fill">Open again.</span> The chase is closed.' : '<span class="tone-warn">Blocked</span> until this chase is closed.'}</p></div></aside></div>`;
}
OC.switcher($('sw'), 'Restart reconcile', STATES, view, 'touch');
