/* Stage, touch and chase (direction B: status card + event log). SAMPLE data. */
'use strict';
OC.chrome('chase', 'live');
const { $, C, LIM } = OC;
const STATES = [
  ['staged', 'Staged: touch now', 'Your touch'], ['sheet', 'The macOS sheet'], ['cancelled', 'You cancelled the touch'], ['notouch', 'No touch in 60 s'],
  ['r-value', 'Over max value', 'Refused by the helper'], ['r-loss', 'Over max loss'], ['r-day', 'Daily stop reached'], ['r-hash', 'Hash mismatch'], ['r-margin', 'Margin field'], ['r-profile', 'Profile expired'],
  ['placing', 'Placing (loading)', 'Live chase'], ['resting', 'Resting'], ['amended', 'Amended'], ['ioc', 'Final IOC (same touch)'], ['pfeed', 'Private feed lost'],
  ['h-stopped', 'Helper stopped', 'Failure'], ['h-cancel', 'Cancel it now (touch)'],
  ['filled', 'Filled', 'Result'], ['partial', 'Part filled + IOC'], ['stopped', 'Stopped by you'], ['expired', 'Expired at Kraken (GTD)'],
];
const E = {
  stage: ['09:51:58', `Staged chase ${C.chase}: copy ${C.hash}. The helper checked your limits: inside.`],
  ask: ['09:51:58', 'The helper asked for Touch ID.'],
  touch: [C.t0, 'You touched Touch ID. The helper read the key.'],
  place: [C.t0, `Placed post-only buy ${C.size} at ${C.start}, order ${C.id1}, expires at Kraken ${C.gtd} (GTD). Kraken id ${C.k1}.`],
  fill1: ['09:52:31', 'Filled 0.0018 BTC at 62,412.00 as maker. Fee 0.28 USD.', 'fill'],
  am1: ['09:52:36', `Amended ${C.id1} to 62,414.90: the bid rose. Signed by the helper, no new touch.`],
};
const refusal = (why, fix, ev) => ({ st: ['Refused by the helper', 'bad', 0], head: 'Nothing was sent to Kraken', sub: why, fix, ev: [ev, E.stage], q: null, refused: true, act: ['back'] });
const S = {
  staged: { st: ['Waiting for your touch', 'you', 1], touch: true, head: `Staged: ${C.side} ${C.size} on ${C.pair}`, sub: 'The helper checked your limits and asked macOS for Touch ID. Your order goes to Kraken only after your touch.', ev: [E.ask, E.stage], act: ['unstage'] },
  sheet: { st: ['Waiting for your touch', 'you', 1], bigsheet: true, head: 'Check the sheet, then touch', sub: 'The sheet text comes from the staged copy. The copy number on the sheet must match the number on this page.', ev: [E.ask, E.stage], act: ['unstage'] },
  cancelled: { st: ['Not placed', 'muted', 0], head: 'You cancelled the touch', sub: 'Nothing was sent to Kraken. The helper deleted the staged copy. Stage again to try again; the prices are new.', ev: [['09:52:09', 'You clicked Cancel on the Touch ID sheet. Nothing placed. Copy deleted.', 'warn'], E.ask, E.stage], act: ['back'] },
  notouch: { st: ['Not placed', 'muted', 0], head: 'No touch in 60 s', sub: 'The staged order waited 60 s. Prices move, so the helper closed the sheet and deleted the copy. Nothing was sent to Kraken. Stage again for new prices.', ev: [['09:52:58', 'No touch in 60 s. The helper closed the sheet. Nothing placed.', 'warn'], E.ask, E.stage], act: ['back'] },
  'r-value': refusal(`The order value is 564.01 USD (0.0090 BTC × cap ${C.cap} + fees). Your max order value is ${LIM.value} USD.`, 'Make the amount smaller, or raise the limit in the helper with <span class="kbd">oc-signer limits</span> (Touch ID).', ['09:51:58', 'Refused: order value 564.01 over max 500.00 USD.', 'bad']),
  'r-loss': refusal(`At your exit plan (58,900.00) the loss is 21.40 USD. Your max loss per trade is ${LIM.loss} USD. The order value is inside its limit, but both must pass.`, 'Move the exit plan closer, make the amount smaller, or raise the limit in the helper.', ['09:51:58', 'Refused: loss at the exit plan 21.40 over max 20.00 USD.', 'bad']),
  'r-day': refusal(`The daily loss stop is reached: committed risk today is 41.10 USD, and this order adds ${C.loss}. That is 53.15 of ${LIM.day} USD.`, 'Live opens again tomorrow at 00:00 UTC. Dry run works now. The helper counts the risk at each exit plan, not the realized loss.', ['09:51:58', `Refused: daily stop. 41.10 + ${C.loss} over 50.00 USD.`, 'bad']),
  'r-hash': refusal('The staged copy changed after the page wrote it: its SHA-256 no longer matches. Something other than this page changed the file.', 'Stage again. If it happens again, stop and tell the developer: the helper refused to sign a changed order.', ['09:51:58', `Refused: copy hash ${C.hash} does not match the file now (9be104d7).`, 'bad']),
  'r-margin': refusal('The staged order has a margin field (leverage 3). Live is spot only.', 'Use Spot on the form. Margin stays in dry run.', ['09:51:58', 'Refused: margin field in a live order. Spot only.', 'bad']),
  'r-profile': refusal('The helper did not start: its signing profile expired on 2026-10-04. macOS refuses to run it, so nothing can sign. This fails closed.', 'Sign the helper again in Xcode. <a href="setup.html#expired">How</a>', ['09:51:58', 'The helper did not start: profile expired. Live blocked.', 'bad']),
  placing: { st: ['Placing', 'you', 1], head: 'Sending the order to Kraken…', sub: 'You touched Touch ID. The helper signed the order. Kraken usually answers in under 1 s.', q: 0, you: 'sending…', ev: [E.touch, E.ask, E.stage], act: ['stop'] },
  resting: { st: ['Resting at the best bid', 'you', 1], head: `Your order rests on Kraken at ${C.start}`, sub: `Nothing filled yet. The tool moves the order up when the bid rises, never above the cap ${C.cap}.`, q: 0, you: C.start, ev: [E.place, E.touch, E.ask, E.stage], act: ['stop', 'fillnow'] },
  amended: { st: ['Amended', 'you', 1], head: 'Moved up to 62,414.90', sub: 'The bid rose, so the tool amended your order. The helper signs amends of this order with no new touch. 0.0018 of 0.0050 BTC filled (36%).', q: 0.0018, you: '62,414.90', ev: [E.am1, E.fill1, E.place, E.touch, E.ask, E.stage], act: ['stop', 'fillnow'] },
  ioc: { st: ['Final IOC', 'warn', 1], head: 'Time is up: buying the rest at the cap', sub: `The tool cancelled ${C.id1}; Kraken confirmed. It sent the final IOC ${C.id2} for 0.0032 BTC at ${C.cap}. Your touch covered it: same chase, same cap.`, q: 0.0018, you: `IOC ${C.cap}`, ev: [[C.end, `Timeout. Cancelled ${C.id1}; Kraken confirmed. Sent IOC ${C.id2} 0.0032 BTC at ${C.cap}.`, 'warn'], E.am1, E.fill1, E.place, E.touch], act: [] },
  pfeed: { st: ['Private feed lost', 'warn', 1], head: 'The order rests, but the fill feed is down', sub: 'The private feed stopped 6 s ago. The tool reads your order by REST every 5 s and pauses amends until the feed is back (Q8).', q: 0.0018, you: '62,414.90', ev: [['09:52:50', 'Private feed lost. Reading the order by REST every 5 s. Amends paused.', 'warn'], E.am1, E.fill1, E.place, E.touch], act: ['stop', 'fillnow'], priv: 'bad' },
  'h-stopped': { st: ['Helper stopped', 'bad', 0], head: 'The helper stopped: the tool cannot amend or cancel', sub: `Your order ${C.id1} still rests at 62,414.90. Kraken removes it at ${C.gtd} at the latest (its GTD expiry). Your other orders are not touched. To cancel it sooner, touch again or cancel it in Kraken Pro.`, q: 0.0018, you: '62,414.90 (last known)', ev: [['09:52:44', 'The helper stopped. Amends stopped. The order expires at Kraken at ' + C.gtd + '.', 'bad'], E.am1, E.fill1, E.place, E.touch], act: ['htouch'], helper: 'bad' },
  'h-cancel': { st: ['Waiting for your touch', 'you', 1], touch: 'cancel', head: `Cancel ${C.id1} now`, sub: 'The helper started again. It forgot the key when it stopped, so it needs a new touch to cancel. The sheet says cancel only.', q: 0.0018, you: '62,414.90 (last known)', ev: [['09:52:51', `Helper started again. Asked Touch ID to cancel ${C.id1}.`], ['09:52:44', 'The helper stopped.', 'bad'], E.am1, E.fill1], act: [] },
  filled: { st: ['Filled', 'fill', 0], done: true, head: `Filled ${C.size}`, sub: '100% filled as maker. Average 62,414.10. Fees 0.78 USD.', q: 0.005, you: 'filled', ids: [[C.id1, C.k1, 'Filled 0.0050 BTC']], ev: [['09:53:12', 'Filled 0.0032 BTC at 62,414.90. Fully filled. Chase ended.', 'fill'], E.am1, E.fill1, E.place, E.touch], act: ['again'] },
  partial: { st: ['Filled with the IOC', 'fill', 0], done: true, head: `Filled ${C.size}`, sub: 'The resting order filled 36%. The final IOC filled the rest at the cap. Average 62,416.30. Fees 1.03 USD.', q: 0.005, you: 'filled', ids: [[C.id1, C.k1, 'Filled 0.0018 BTC · cancelled'], [C.id2, C.k2, 'Filled 0.0032 BTC (IOC)']], ev: [['09:54:05', 'IOC filled 0.0032 BTC at 62,418.50. Chase ended.', 'fill'], [C.end, 'Timeout. Sent the final IOC.', 'warn'], E.fill1, E.place, E.touch], act: ['again'] },
  stopped: { st: ['Stopped by you', 'muted', 0], done: true, head: 'Stopped. Filled 0.0018 of 0.0050 BTC (36%)', sub: `You clicked Stop. The helper cancelled ${C.id1} with no new touch: the chase's touch covers its cancel. Kraken confirmed. No IOC sent.`, q: 0.0018, you: 'cancelled', ids: [[C.id1, C.k1, 'Filled 0.0018 BTC · cancelled']], ev: [['09:53:01', `Stop. Cancelled ${C.id1}; Kraken confirmed. Chase ended.`], E.fill1, E.place, E.touch], act: ['again'] },
  expired: { st: ['Expired at Kraken', 'warn', 0], done: true, head: `Kraken removed ${C.id1} at ${C.gtd}`, sub: 'The helper stopped and you did not cancel. Kraken removed the order at its GTD expiry, 30 s after the chase end. Filled 0.0018 of 0.0050 BTC (36%). No IOC: the helper was not there to sign it.', q: 0.0018, you: 'expired', ids: [[C.id1, C.k1, 'Filled 0.0018 BTC · expired (GTD)']], ev: [[C.gtd, `Kraken expired ${C.id1} (GTD). Other orders not touched.`, 'warn'], ['09:52:44', 'The helper stopped.', 'bad'], E.fill1, E.place], act: ['again'] },
};
const ACT = {
  stop: '<button class="btn danger">Stop and cancel</button>', fillnow: '<button class="btn">Fill the rest now</button>', unstage: '<a class="btn" href="#cancelled">Cancel this order</a>',
  back: '<a class="btn primary" href="new.html#live">Back to the form</a>', again: '<a class="btn" href="new.html#live">New chase</a>', htouch: '<a class="btn livebtn" href="#h-cancel">Cancel it now (Touch ID)</a>',
};
function view(id) {
  const s = S[id];
  const pct = s.q == null ? null : Math.floor(s.q / 0.005 * 100 + 1e-9);
  let card = `<div class="status"><div class="state tone-${s.st[1]} ${s.st[2] ? 'pulse' : ''}"><i></i>${s.st[0]}</div><div class="head">${s.head}</div><p class="sub">${s.sub}</p>`;
  if (s.touch) card += `<div style="margin-top:12px">${OC.touchNow(s.touch === 'cancel' ? `The sheet asks to cancel ${C.id1}. Copy <span class="hash">${C.hash}</span>.` : `Check that the sheet shows copy <span class="hash">${C.hash}</span>, then touch. The sheet closes after 60 s.`)}</div>`;
  if (s.fix) card += `<div class="note warn small" style="margin-top:12px"><b>What to do:</b> ${s.fix}</div>`;
  if (pct != null) card += `<div class="row small" style="margin-top:10px"><span>Filled <b class="num">${s.q.toFixed(4)}</b> of 0.0050 BTC</span><span class="spacer"></span><b class="num">${pct}%</b></div><div class="bar" style="margin-top:6px"><i class="m" style="width:${pct}%"></i></div>`;
  if (s.ids) card += '<h3 style="margin-top:14px">Orders of this chase</h3><table class="kv"><tr><th style="text-align:left">Order id</th><th style="text-align:left">Kraken id</th><th style="text-align:left">Result</th></tr>' + s.ids.map(r => `<tr><td class="mono" style="text-align:left">${r[0]}</td><td class="mono" style="text-align:left">${r[1]}</td><td style="text-align:left">${r[2]}</td></tr>`).join('') + '</table><p class="tiny muted" style="margin:6px 0 0">Search the Kraken id in Kraken Pro, Orders. Sample ids. Your other orders were not touched.</p>';
  card += '</div>';
  const staged = ['staged', 'sheet', 'cancelled', 'notouch'].includes(id) || s.refused;
  const facts = [['Order', `${C.side} ${C.size} · spot`], ['Start · cap', `<span class="num">${C.start} · ${C.cap}</span>`], ['Order ids', `<span class="mono">${C.id1}</span>, IOC <span class="mono">${C.id2}</span>`],
    ['Copy (SHA-256)', `<span class="hash" title="${C.hashFull}">${C.hash}</span>`], ['Exit plan', `<span class="num">${id === 'r-loss' ? '58,900.00' : C.exit}</span> (not placed)`], ['Worst value', `<span class="num">${C.value} USD</span>`]];
  if (!staged) facts.push(['Your price now', `<span class="num">${s.you}</span>`]);
  const gtd = staged ? '<p class="small" style="margin:0">Not placed yet. When placed: each order expires at Kraken 30 s after the chase ends. The chase ends within 10 min.</p>'
    : `<table class="kv"><tr><td class="muted">Chase ends</td><td class="num">${C.end} at the latest</td></tr><tr><td class="muted">Kraken removes the order</td><td class="num">${C.gtd} (GTD)</td></tr></table>
       <p class="tiny muted" style="margin:8px 0 0">If order-chaser stops, the helper cancels this chase's orders. If the helper stops too, Kraken removes them at ${C.gtd}. Your other orders are never touched.</p>`;
  const helper = s.helper === 'bad' ? '<span class="tone-bad">stopped</span>' : staged ? '<span class="tone-fill">connected</span>' : '<span class="tone-fill">connected · signs this chase only</span>';
  $('view').innerHTML = '<div class="livestrip" style="margin-bottom:16px">LIVE: this chase sends real orders to your Kraken account.</div>' +
    `<div class="split"><div>${card}${s.bigsheet ? `<div style="margin-top:16px">${OC.sheet('place')}</div>` : ''}${s.touch && !s.bigsheet ? `<div style="margin-top:16px">${OC.sheet(s.touch === 'cancel' ? 'cancel' : 'place')}</div>` : ''}
      <div class="row" style="margin:14px 0 18px">${s.act.map(a => ACT[a]).join('')}</div>
      <div class="card"><div class="row"><h2 style="margin:0">What the tool did</h2><span class="spacer"></span><span class="small muted">Newest first · sample events</span></div><div style="margin-top:8px">${OC.tl(s.ev)}</div></div></div>
    <aside class="side"><div class="card"><h3>This chase <span class="mono small">${C.chase}</span></h3><table class="kv">${facts.map(r => `<tr><td class="muted">${r[0]}</td><td>${r[1]}</td></tr>`).join('')}</table></div>
      <div class="card"><h3>Expiry at Kraken</h3>${gtd}</div>
      <div class="card"><h3>Connections</h3><table class="kv"><tr><td class="muted">Helper</td><td>${helper}</td></tr><tr><td class="muted">Public prices</td><td><span class="tone-fill">connected</span></td></tr><tr><td class="muted">Your fills</td><td>${s.priv === 'bad' ? '<span class="tone-warn">lost: REST every 5 s</span>' : '<span class="tone-fill">connected</span>'}</td></tr></table></div></aside></div>`;
  OC.pill('helper', ...(s.helper === 'bad' ? ['bad', 'Helper: stopped'] : id === 'r-profile' ? ['bad', 'Helper: profile expired'] : ['', 'Helper: ready · live on']));
}
OC.switcher($('sw'), 'Touch and chase', STATES, view, 'staged');
