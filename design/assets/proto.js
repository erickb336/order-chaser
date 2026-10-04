/* Order chaser prototype: shared chrome, sample data, states and questions.
   All numbers are SAMPLE DATA. Nothing here calls a network. */
(function () {
'use strict';

// ---------- Sample data ----------
const S = {
  pair: 'BTC/USD', base: 'BTC', quote: 'USD', side: 'buy', qty: 0.05,
  startBid: 62417.9, startAsk: 62418.5, timeout: 120,
  maker: 0.004, taker: 0.008, tick: 0.1, minQty: 0.00005,
};
const f2 = n => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fq = n => n.toFixed(4);
const mmss = s => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');

function summarize(fills, startAsk) {
  startAsk = startAsk || S.startAsk;
  let q = 0, gross = 0, fee = 0, mq = 0;
  fills.forEach(x => { q += x.q; gross += x.q * x.p; fee += x.q * x.p * (x.liq === 't' ? S.taker : S.maker); if (x.liq === 'm') mq += x.q; });
  const avg = q ? gross / q : 0;
  const market = q * startAsk * (1 + S.taker);
  const ours = gross + fee;
  return { q, avg, gross, fee, ours, market, saving: market - ours, makerShare: q ? mq / q : 0 };
}

// ---------- Live chase states (one object per state) ----------
const F1 = { q: 0.018, p: 62418.1, liq: 'm' };
const F2 = { q: 0.032, p: 62418.3, liq: 'm' };
const base = [
  ['0:00', 'Recorded the start ask, 62,418.50, as the cap.', ''],
  ['0:00', 'Safety timer on: 60 s, refreshed every 20 s.', ''],
  ['0:00', 'Placed a post-only buy, 0.0500 BTC at 62,417.90.', 'ev-you'],
];
const amended = [['0:06', 'Best bid rose to 62,418.10. Amended the order to 62,418.10.', 'ev-you']];
const fill1 = [['0:31', 'Filled 0.0180 BTC at 62,418.10 (maker).', 'ev-fill']];
const amended2 = [['0:37', 'Best bid rose to 62,418.30. Amended the order to 62,418.30.', 'ev-you']];

const STATES = [
  { id: 'placing', label: 'Placing', tone: 'you', pulse: true, t: 0, fills: [], you: null, pending: 62417.9, bid: 62417.9, ask: 62418.5, rate: 1, conn: 'ok', dms: 0,
    title: 'Placing your order',
    sub: 'The tool sends a post-only buy for 0.0500 BTC at the best bid, 62,417.90. Post-only means the order never takes liquidity, so you pay the maker fee.',
    events: base.slice(0, 2).concat([['0:00', 'Sending a post-only buy, 0.0500 BTC at 62,417.90…', 'ev-you']]), actions: ['stop'] },
  { id: 'resting', label: 'Resting', tone: 'you', pulse: true, t: 4, fills: [], you: 62417.9, bid: 62417.9, ask: 62418.5, rate: 2, conn: 'ok', dms: 4,
    title: 'Resting at the best bid',
    sub: 'Your order waits at 62,417.90. When the best bid rises, the tool moves the order up, at most once every 5 s. The order never goes above the cap, 62,418.50.',
    events: base.slice(), actions: ['stop', 'fillnow'] },
  { id: 'amending', label: 'Amending', tone: 'you', pulse: true, t: 6, fills: [], you: 62417.9, pending: 62418.1, bid: 62418.1, ask: 62418.5, rate: 5, conn: 'ok', dms: 6,
    title: 'Moving your order up',
    sub: 'Another buyer raised the best bid to 62,418.10. The tool amends your order to that price. The order keeps the same id and its fill history.',
    events: base.concat([['0:06', 'Best bid rose to 62,418.10. Amending the order to 62,418.10…', 'ev-you']]), actions: ['stop', 'fillnow'] },
  { id: 'partial', label: 'Partial fill', tone: 'fill', pulse: true, t: 47, fills: [F1], you: 62418.3, bid: 62418.3, ask: 62418.5, rate: 6, conn: 'ok', dms: 7,
    title: 'Partly filled: 36%',
    sub: '0.0180 BTC filled at 62,418.10 as maker. The rest, 0.0320 BTC, rests at the best bid, 62,418.30.',
    events: base.concat(amended, fill1, amended2), actions: ['stop', 'fillnow'] },
  { id: 'filled', label: 'Filled', tone: 'fill', t: 78, fills: [F1, F2], you: null, bid: 62418.2, ask: 62418.5, rate: 3, conn: 'ok', dms: null, done: true,
    title: 'Filled',
    sub: 'All 0.0500 BTC filled as maker, at an average of 62,418.23. The tool turned off the safety timer.',
    events: base.concat(amended, fill1, amended2, [['1:18', 'Filled 0.0320 BTC at 62,418.30 (maker). Order complete.', 'ev-fill'], ['1:18', 'Safety timer off.', '']]), actions: ['result', 'new'] },
  { id: 'fallback', label: 'Timeout fallback', tone: 'warn', pulse: true, t: 120, fills: [F1], you: null, bid: 62418.4, ask: 62418.5, rate: 9, conn: 'ok', dms: 2,
    title: 'Time is up: filling the rest',
    sub: 'The 2:00 timeout passed. The tool now fills the rest with one IOC limit at the cap. IOC means "fill now what you can, cancel the rest". It does these steps in this order:',
    steps: [['done', 'Cancel the resting order.'], ['done', 'Wait for Kraken to confirm the cancel.'], ['done', 'Read the filled quantity again: 0.0180 BTC.'], ['now', 'Send an IOC buy for the rest, 0.0320 BTC, at the cap, 62,418.50.']],
    events: base.concat(amended, fill1, amended2, [['2:00', 'Timeout. Cancelled the resting order.', 'ev-warn'], ['2:00', 'Kraken confirmed the cancel. Filled so far: 0.0180 BTC.', ''], ['2:00', 'Sending an IOC buy, 0.0320 BTC at 62,418.50…', 'ev-warn']]), actions: [] },
  { id: 'notfilled', label: 'Rest not filled (above cap)', tone: 'bad', t: 121, fills: [F1], you: null, bid: 62429.4, ask: 62431.0, rate: 10, conn: 'ok', dms: null, done: true,
    title: 'Stopped: the rest did not fill',
    sub: 'The price rose above your cap. The ask is now 62,431.00, which is 12.50 above the cap of 62,418.50. The IOC filled nothing. You bought 0.0180 of 0.0500 BTC. No order of yours rests on Kraken.',
    events: base.concat(amended, fill1, amended2, [['2:00', 'Timeout. Cancelled the resting order.', 'ev-warn'], ['2:00', 'Kraken confirmed the cancel. Filled so far: 0.0180 BTC.', ''], ['2:01', 'IOC buy, 0.0320 BTC at 62,418.50: nothing filled. The ask is 62,431.00.', 'ev-bad'], ['2:01', 'Chase ended. Safety timer off.', '']]), actions: ['result', 'again'] },
  { id: 'rejected', label: 'Amend rejected', tone: 'warn', pulse: true, t: 52, fills: [F1], you: 62418.3, bid: 62418.3, ask: 62418.4, rate: 8, conn: 'ok', dms: 12,
    title: 'Amend rejected: checking the order',
    sub: 'Kraken rejected the amend to 62,418.40. Reason: the ask fell to 62,418.40, so a post-only order at that price would take liquidity. The tool does not retry. It reads the order state again first.',
    steps: [['done', 'Amend rejected: post-only order would cross the ask.'], ['done', 'Read the order state again: open, 0.0180 BTC filled, price 62,418.30.'], ['now', 'Wait for the next change of the best bid. Next amend possible in 3 s.']],
    events: base.concat(amended, fill1, amended2, [['0:52', 'Amend to 62,418.40 rejected: would cross the ask (post-only).', 'ev-warn'], ['0:52', 'Read the order again: open, 0.0180 BTC filled, at 62,418.30.', '']]), actions: ['stop', 'fillnow'] },
  { id: 'disconnected', label: 'Disconnected', tone: 'bad', pulse: true, t: 63, fills: [F1], you: 62418.3, bid: 62418.3, ask: 62418.5, rate: 6, conn: 'bad', dms: 26, stale: true,
    title: 'Connection to Kraken lost: reconnecting',
    sub: 'The tool lost the Kraken feed 8 s ago. Your order still rests on Kraken at 62,418.30. The tool does not move the order while it cannot see prices. Attempt 3: next try in 4 s.',
    steps: [['now', 'Reconnect to the Kraken feed (attempt 3).'], ['todo', 'Read the order and the fills again.'], ['todo', 'Continue the chase from that state.']],
    note: 'If the tool cannot refresh the safety timer, Kraken cancels ALL your orders in 34 s, on every pair.',
    events: base.concat(amended, fill1, amended2, [['0:55', 'Lost the Kraken feed. Order frozen at 62,418.30.', 'ev-bad'], ['0:58', 'Reconnect attempt 1 failed.', ''], ['1:01', 'Reconnect attempt 2 failed.', '']]), actions: ['stop'] },
  { id: 'pageoffline', label: 'Page lost the tool', tone: 'bad', t: 70, fills: [F1], you: 62418.3, bid: 62418.3, ask: 62418.5, rate: 6, conn: 'page', dms: 12, stale: true,
    title: 'This page lost the tool',
    sub: 'This page cannot reach the tool at localhost:5180. The values below are from 12 s ago. If the tool still runs, the chase continues without this page. Restart the tool with "order-chaser start" if it stopped.',
    note: 'If the tool stopped, the safety timer makes Kraken cancel ALL your orders within 60 s.',
    events: base.concat(amended, fill1, amended2), actions: [] },
  { id: 'ratenear', label: 'Rate limit near', tone: 'warn', pulse: true, t: 70, fills: [F1], you: 62418.3, bid: 62418.4, ask: 62418.5, rate: 47, conn: 'ok', dms: 10,
    title: 'Slowing down: rate limit near',
    sub: 'Your Kraken rate counter for BTC/USD is at 47 of 60. The tool now waits 15 s between amends, not 5 s. This keeps room for a cancel. Your order can trail the best bid for a short time.',
    events: base.concat(amended, fill1, amended2, [['1:04', 'Rate counter at 47 of 60. Next amend in 15 s, not 5 s.', 'ev-warn']]), actions: ['stop', 'fillnow'] },
  { id: 'stopped', label: 'Stopped by you', tone: 'muted', t: 55, fills: [F1], you: null, bid: 62418.3, ask: 62418.5, rate: 7, conn: 'ok', dms: null, done: true,
    title: 'Stopped by you',
    sub: 'You stopped the chase. The tool cancelled the order and Kraken confirmed the cancel. 0.0180 BTC filled before the stop. No order of yours rests on Kraken.',
    events: base.concat(amended, fill1, amended2, [['0:55', 'You pressed Stop. Cancelled the order.', ''], ['0:55', 'Kraken confirmed the cancel. Filled: 0.0180 BTC. Safety timer off.', '']]), actions: ['result', 'again'] },
];

// ---------- Questions for the owner ----------
const QUESTIONS = [
  { id: 'Q1', on: ['new', 'chase'], t: 'More than one chase at a time?',
    why: 'The safety timer and the rate counter are per account and per pair. Two chases share them.',
    o: [['a', 'One chase at a time. The form is locked while a chase runs.'], ['b', 'One chase per pair.'], ['c', 'Any number of chases.']], rec: 'a', def: 'a' },
  { id: 'Q2', on: ['new'], t: 'Unit of the amount: base or quote currency?',
    why: 'Kraken sizes a limit order in the base currency (BTC). A quote amount (USD) must be turned into BTC at the start.',
    o: [['a', 'Base currency (0.0500 BTC). The form shows the USD estimate.'], ['b', 'Quote currency (3,000 USD). The tool converts at the start ask.'], ['c', 'A switch on the form to pick each time.']], rec: 'a', def: 'a' },
  { id: 'Q3', on: ['chase'], t: 'What happens when the page closes while a chase runs?',
    why: 'The local tool, not the page, runs the chase. The page is only a view.',
    o: [['a', 'The tool keeps chasing. The page shows the chase again when you open it.'], ['b', 'The tool stops the chase and cancels the order.'], ['c', 'The browser asks "Leave this page?" first, then (a).']], rec: 'c', def: 'c' },
  { id: 'Q4', on: ['new', 'chase'], t: 'Sell side: is an exact mirror correct?',
    why: 'A sell rests at the best ask and moves down. Its limit is a floor: the bid seen at the start.',
    o: [['a', 'Exact mirror: rest at the best ask, floor = start bid, fallback IOC at the start bid. Show the word "floor" for a sell.'], ['b', 'Buy only in the first version.']], rec: 'a', def: 'a' },
  { id: 'Q5', on: ['setup'], t: 'How often must you accept the safety timer risk?',
    why: 'The safety timer (Kraken CancelAllOrdersAfter) cancels ALL your orders on ALL pairs if the tool stops, also stop-losses you placed by hand.',
    o: [['a', 'Once in setup. Each live chase shows a one-line reminder.'], ['b', 'On every live chase.'], ['c', 'Only allow live chases in a Kraken sub-account.']], rec: 'a', def: 'a' },
  { id: 'Q6', on: ['chase'], t: 'What can you do during a chase: Stop only, or also "Fill the rest now"?',
    why: '"Fill the rest now" runs the timeout fallback at once: cancel, confirm, re-read, then IOC at the cap.',
    o: [['a', 'Stop only: cancel the order and keep what filled.'], ['b', 'Stop, and also a "Fill the rest now" button.']], rec: 'b', def: 'a' },
  { id: 'Q7', on: ['new'], t: 'Can you change the timeout for each chase?',
    why: 'A short timeout gives faster fills but more taker fees. A long one gives more maker fills but more price risk.',
    o: [['a', 'No. Always 2 minutes.'], ['b', 'Yes, from 30 s to 15 min. The form starts at 2 min.']], rec: 'b', def: 'b' },
  { id: 'Q8', on: ['new'], t: 'Limits on the cap override?',
    why: 'An override above the start ask can cost more than a market order at the start.',
    o: [['a', 'Any higher limit. The form shows the extra worst-case cost, and you confirm it.'], ['b', 'At most 1% above the start ask.'], ['c', 'No override in the first version.']], rec: 'a', def: 'a' },
  { id: 'Q9', on: ['chase', 'result'], t: 'What if the rest is below the Kraken minimum order size?',
    why: 'Kraken rejects an order below its minimum (sample: 0.00005 BTC). Then the fallback IOC cannot go out.',
    o: [['a', 'Stop, and report the small rest as "not filled: below the minimum".'], ['b', 'Round the IOC up to the minimum (you buy a little more than you asked).']], rec: 'a', def: 'a' },
  { id: 'Q10', on: ['setup', 'new'], t: 'Add the "Query Funds" permission to check your balance?',
    why: 'Without it, the form cannot show your balance. Kraken then rejects an order that is too large, after you start.',
    o: [['a', 'Add Query Funds (read only). The form blocks an amount above your balance.'], ['b', 'No balance check. Kraken rejects the order, and the tool shows why.']], rec: 'a', def: 'b' },
  { id: 'Q11', on: ['setup'], t: 'How does the API key get into the macOS Keychain?',
    why: 'The key must never be in a file. Both options keep it in the Keychain only.',
    o: [['a', 'Paste it in the setup page. The local tool writes it to the Keychain and never shows it again.'], ['b', 'Run one Terminal command that the page shows (security add-generic-password).']], rec: 'a', def: 'a' },
  { id: 'Q12', on: ['chase', 'result'], t: 'Dry run: when does a simulated order fill?',
    why: 'A dry run does not know your place in the queue. The rule sets how optimistic its result is.',
    o: [['a', 'When a public trade prints at your price or better.'], ['b', 'Only when a public trade prints through your price (below it, for a buy).']], rec: 'b', def: 'b' },
  { id: 'Q13', on: ['chase'], t: 'How do you learn that a chase ended, when the tab is in the background?',
    why: 'A chase can run for minutes.',
    o: [['a', 'A macOS notification from the browser, and the tab title.'], ['b', 'The tab title only.']], rec: 'a', def: 'b' },
  { id: 'Q14', on: ['new'], t: 'Must a dry run come before the first live chase?',
    why: 'A dry run shows the behaviour on live prices with no risk.',
    o: [['a', 'Yes, one dry run before the first live chase.'], ['b', 'Yes, one dry run for each new pair.'], ['c', 'No rule. Dry run is only the default mode.']], rec: 'a', def: 'a' },
];

// ---------- Chrome: prototype strip, sample-data ribbon, product top bar ----------
const PAGES = [
  ['index', 'index.html', 'Start'], ['compare', 'compare.html', 'Compare directions'],
  ['setup', 'setup.html', '1 Setup'], ['new', 'new.html', '2 New chase'], ['chase', 'chase.html', '3 Live chase'],
  ['result', 'result.html', '4 Result'], ['history', 'history.html', '5 History'], ['questions', 'questions.html', 'Questions'],
];
function chrome(page, opts) {
  opts = opts || {};
  const n = QUESTIONS.filter(q => q.on.includes(page)).length;
  const strip = document.createElement('div');
  strip.className = 'proto';
  strip.innerHTML = '<div class="in"><span class="tag">Prototype</span>' +
    PAGES.map(p => `<a href="${p[1]}" class="${p[0] === page ? 'on' : ''}">${p[2]}</a>`).join('') +
    '<span class="spacer"></span>' + (n ? `<button id="qbtn">Questions for this screen (${n})</button>` : '') + '</div>';
  const ribbon = document.createElement('div');
  ribbon.className = 'sample';
  ribbon.innerHTML = '<b>SAMPLE DATA</b>: all prices, fills and fees on this page are invented for the prototype. No page calls Kraken.';
  document.body.prepend(ribbon);
  document.body.prepend(strip);
  if (opts.top) {
    const app = document.querySelector('.app');
    const top = document.createElement('div');
    top.className = 'top';
    top.innerHTML = '<div class="brand"><span class="dot"></span>Order chaser</div>' +
      '<nav class="nav"><a href="new.html" class="' + (page === 'new' || page === 'chase' ? 'on' : '') + '">Chase</a><a href="history.html" class="' + (page === 'history' || page === 'result' ? 'on' : '') + '">History</a><a href="setup.html" class="' + (page === 'setup' ? 'on' : '') + '">Setup</a></nav>' +
      '<span class="spacer"></span><span class="conn" id="conn"><i></i>Kraken feed: connected</span><span class="badge plain mono">localhost:5180</span>';
    app.prepend(top);
  }
  if (n) {
    const d = document.createElement('aside');
    d.className = 'qdrawer'; d.id = 'qdrawer';
    d.innerHTML = '<div class="row"><h2>Questions for the owner</h2><span class="spacer"></span><button class="btn" id="qclose">Close</button></div>' +
      '<p class="muted small">These cases are not decided. The prototype shows the default. Answer with the letter, for example "Q3: a".</p>' +
      QUESTIONS.filter(q => q.on.includes(page)).map(qHTML).join('') +
      '<p class="small"><a href="questions.html">See all 14 questions</a></p>';
    document.body.appendChild(d);
    document.getElementById('qbtn').onclick = () => d.classList.add('open');
    document.getElementById('qclose').onclick = () => d.classList.remove('open');
    document.addEventListener('click', e => {
      const a = e.target.closest('[data-q]');
      if (a) { e.preventDefault(); d.classList.add('open'); const el = d.querySelector('#d-' + a.dataset.q); if (el) el.scrollIntoView(); }
    });
  }
}
function qHTML(q) {
  const lab = k => q.o.find(x => x[0] === k)[0];
  return `<div class="q" id="d-${q.id}"><span class="qid">${q.id}</span><h3>${q.t}</h3><p class="small muted">${q.why}</p>
    <ul class="opts">${q.o.map(o => `<li data-k="(${o[0]})" class="${o[0] === q.rec ? 'rec' : ''}">${o[1]}</li>`).join('')}</ul>
    <div class="def">Recommendation: (${lab(q.rec)}). Default if you do not answer: (${lab(q.def)}).</div></div>`;
}
const qref = id => `<a class="qlink" data-q="${id}" href="questions.html#${id}" title="Open question ${id}">${id}</a>`;

// ---------- Renderers for the three live-chase directions ----------
const toneVar = { you: 'var(--you)', fill: 'var(--fill)', warn: 'var(--warn)', bad: 'var(--bad)', muted: 'var(--muted)' };

function rail(st) {
  const pts = [st.bid, st.ask, S.startAsk, S.startBid];
  if (st.you) pts.push(st.you);
  if (st.pending) pts.push(st.pending);
  let lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts);
  const span = Math.max(hi - lo, 0.6), pad = span * 0.12 + 0.05;
  lo -= pad; hi += pad;
  const x = v => ((v - lo) / (hi - lo) * 100).toFixed(2) + '%';
  const near = (a, b) => a != null && b != null && Math.abs((a - b) / (hi - lo)) < 0.24;
  const yp0 = st.pending || st.you;
  const side = (cls, v) => {
    if (cls === 'bid' && near(st.bid, st.ask)) return 'l';
    if (cls === 'ask' && near(st.bid, st.ask)) return 'r';
    if (cls === 'you' && near(yp0, S.startAsk)) return yp0 < S.startAsk ? 'l' : 'r';
    if (cls === 'cap' && near(yp0, S.startAsk) && Math.abs(yp0 - S.startAsk) > 0.05) return yp0 < S.startAsk ? 'r' : 'l';
    return '';
  };
  const mk = (cls, lab, v, below) => `<div class="mk ${cls} ${below ? 'below' : ''} ${side(cls, v)}" style="left:${x(v)}">${below ? '<div class="pin"></div>' : ''}<div class="val">${f2(v)}</div><div class="lab">${lab}</div>${below ? '' : '<div class="pin"></div>'}</div>`;
  let h = `<div class="rail"><div class="axis"></div><div class="zone" style="left:${x(S.startBid)};width:calc(${x(S.startAsk)} - ${x(S.startBid)})" title="Chase range: from the start bid up to the cap"></div>`;
  h += mk('bid', 'best bid', st.bid, true) + mk('ask', 'best ask', st.ask, true);
  h += mk('cap', 'cap (start ask)', S.startAsk, false);
  const yp = st.pending || st.you;
  if (yp && Math.abs(yp - S.startAsk) > 0.05) h += mk('you', st.pending ? (st.you ? 'your order (moving)' : 'your order (sending)') : 'your order', yp, false);
  else if (yp) h = h.replace('cap (start ask)', 'cap = your order');
  return h + '</div>';
}

function card(st, compact) {
  const sm = summarize(st.fills);
  const pct = Math.round(sm.q / S.qty * 100);
  const tone = st.tone;
  const steps = st.steps ? `<ol class="small" style="margin:10px 0 0;padding-left:20px">${st.steps.map(s => `<li style="padding:2px 0" class="${s[0] === 'now' ? 'tone-' + tone : s[0] === 'todo' ? 'muted' : ''}">${s[0] === 'done' ? '✓ ' : s[0] === 'now' ? '→ ' : ''}${s[1]}</li>`).join('')}</ol>` : '';
  const notfilled = st.id === 'notfilled' ? `<i class="nf" style="width:${100 - pct}%"></i>` : '';
  return `<div class="status" style="${st.stale ? 'opacity:.92' : ''}">
    <div class="state tone-${tone} ${st.pulse ? 'pulse' : ''}"><i></i>${st.label}${st.stale ? ' · values from 8–12 s ago' : ''}</div>
    <div class="head" style="${compact ? 'font-size:21px' : ''}">${st.title}</div>
    <div class="sub ${compact ? 'small' : ''}">${st.sub}</div>${steps}
    ${st.note ? `<div class="note bad small" style="margin-top:12px"><b>Warning:</b> ${st.note}</div>` : ''}
    <div class="row" style="margin-top:18px;align-items:flex-end;flex-wrap:wrap">
      <div><div class="small muted">Filled</div><div class="big" style="white-space:nowrap;${compact ? 'font-size:24px' : ''}">${fq(sm.q)} <span class="muted" style="font-size:.55em">of ${fq(S.qty)} BTC</span></div></div>
      <span class="spacer"></span>
      ${st.done ? '' : `<div class="timer" style="white-space:nowrap"><span class="num">${mmss(st.t)} / 2:00</span><div class="bar"><i style="width:${Math.min(100, st.t / S.timeout * 100)}%"></i></div></div>`}
    </div>
    <div class="bar" style="margin-top:8px"><i class="m" style="width:${pct}%"></i>${notfilled}</div>
    ${rail(st)}
  </div>`;
}

function ladder(st) {
  const lvl = [];
  const top = Math.max(S.startAsk + 0.4, Math.min(st.ask, S.startAsk + 0.4) + 0.2);
  const bot = S.startBid - 0.5;
  const above = st.ask > S.startAsk + 0.4;
  for (let p = Math.round(top * 10); p >= Math.round(bot * 10); p--) lvl.push(p / 10);
  const seed = p => (Math.sin(p * 12.9898) * 43758.5453) % 1;
  const qty = p => Math.abs(seed(p)) * 0.9 + 0.05;
  let h = '<div class="ladder"><div class="hdr"><span>Bids (BTC)</span><span>Price</span><span>Asks (BTC)</span></div>';
  if (above) h += `<div class="lr" style="background:var(--bad-bg)"><span></span><span class="p tone-bad">${f2(st.ask)}</span><span class="qa">best ask: 12.50 above the cap</span></div><div class="lr"><span></span><span class="p muted">⋮</span><span></span></div>`;
  lvl.forEach(p => {
    const isAsk = !above && p >= st.ask - 1e-9;
    const isBid = p <= st.bid + 1e-9;
    const you = st.you && Math.abs(p - st.you) < 1e-9;
    const pend = st.pending && Math.abs(p - st.pending) < 1e-9;
    const cap = Math.abs(p - S.startAsk) < 1e-9;
    const q = qty(p);
    h += `<div class="lr ${you || pend ? 'you' : ''} ${cap ? 'cap' : ''}">
      <span class="qb">${isBid ? (you ? '<b class="tone-you">+0.0' + (st.fills.length ? '320' : '500') + '</b> ' : '') + q.toFixed(3) : ''}${isBid ? `<span class="depth" style="right:0;width:${q * 60}%;background:var(--bid)"></span>` : ''}${(you || pend) ? `<span class="tagy">${pend && !you ? 'moving here' : pend ? 'you → ' + f2(st.pending) : 'you'}</span>` : ''}</span>
      <span class="p">${f2(p)}</span>
      <span class="qa">${isAsk ? q.toFixed(3) + `<span class="depth" style="left:0;width:${q * 60}%;background:var(--ask)"></span>` : ''}${cap ? '<span class="tagc">cap</span>' : ''}</span></div>`;
  });
  h += '</div>';
  const sm = summarize(st.fills);
  return `<div class="row small" style="margin-bottom:8px"><span class="badge" style="background:${toneVar[st.tone]};color:#fff">${st.label}</span><span class="spacer"></span><span class="num">${fq(sm.q)} / ${fq(S.qty)} BTC</span><span class="num muted">${mmss(st.t)}</span></div>` + h +
    `<div class="tiny muted" style="margin-top:6px">Blue row: your order. Black line: the cap. Depth bars: sample sizes.</div>`;
}

function timeline(st) {
  const sm = summarize(st.fills);
  return `<div class="row small" style="margin-bottom:10px;border-bottom:1px solid var(--line);padding-bottom:8px"><b>Buy 0.0500 BTC</b><span class="spacer"></span><span class="num">${fq(sm.q)} filled</span><span class="num muted">${mmss(st.t)}</span></div>
    <ul class="tl">${st.events.map(e => `<li class="${e[2]}"><div class="ts">${e[0]}</div>${e[1]}</li>`).join('')}
    <li class="now"><div class="ts">now</div><b class="tone-${st.tone}">${st.title}.</b></li></ul>`;
}

function eventLog(st) {
  return `<ul class="log">${st.events.slice().reverse().map(e => `<li><span class="ts">${e[0]}</span><span class="${e[2]}">${e[1]}</span></li>`).join('')}</ul>`;
}

window.OC = { S, STATES, QUESTIONS, summarize, f2, fq, mmss, chrome, qHTML, qref, rail, card, ladder, timeline, eventLog };
})();
