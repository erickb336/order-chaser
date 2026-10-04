/* Order chaser prototype, task T5: margin open and close.
   All numbers are SAMPLE DATA. Nothing here calls a network. Load after proto.js. */
(function () {
'use strict';
const { S, f2, fq } = window.OC;

// ---------- Sample account and positions ----------
// Kraken margin level = equity / used margin x 100%. Kraken calls margin at 80% and liquidates at 40%.
const ACC = { equity: 4210.55, call: 80, stop: 40 };
const POS = [
  { id: 'btc-short', pair: 'BTC/USD', base: 'BTC', dir: 'short', qty: 0.03, lev: 3, entry: 62950.0, mark: 62418.5, opened: '2 Oct 14:05', roll: 1.13, minQty: 0.00005 },
  { id: 'eth-long', pair: 'ETH/USD', base: 'ETH', dir: 'long', qty: 0.8, lev: 2, entry: 2431.18, mark: 2402.6, opened: 'Yesterday 08:40', roll: 0.58, minQty: 0.002 },
];
POS.forEach(p => { p.cost = p.qty * p.entry; p.margin = p.cost / p.lev; p.pl = (p.dir === 'long' ? 1 : -1) * (p.mark - p.entry) * p.qty; });
const used = () => POS.reduce((a, p) => a + p.margin, 0);
const level = u => u > 0 ? ACC.equity / u * 100 : Infinity;
// Fee rates: Kraken US margin fees are 0.01% to 0.05% of the position cost. No API gives them, so the tool uses the stated maximum.
const MF = { lo: 0.0001, hi: 0.0005 };
// Leverage that each sample pair allows (from AssetPairs leverage_buy / leverage_sell).
const LEV = {
  'BTC/USD': { buy: [2, 3, 4, 5], sell: [2, 3, 4, 5], status: 'online' },
  'ETH/USD': { buy: [2, 3, 4, 5], sell: [2, 3, 4, 5], status: 'online' },
  'SOL/USD': { buy: [2, 3], sell: [2, 3], status: 'online' },
  'XRP/USD': { buy: [2, 3, 4, 5], sell: [], status: 'online' },
};

function openCalc(qty, price, lev) {
  const cost = qty * price, collat = cost / lev;
  const u0 = used(), u1 = u0 + collat;
  return { cost, collat, feeLo: cost * MF.lo, feeHi: cost * MF.hi, rollLo: cost * MF.lo, rollHi: cost * MF.hi,
    taker: cost * S.taker, now: level(u0), after: level(u1), free: ACC.equity - u0, enough: collat <= ACC.equity - u0 };
}

// ---------- Margin level gauge ----------
// Scale 0% to 300%. A level above 300% sits at the right end with a "+".
function gauge(now, after, opts) {
  opts = opts || {};
  const x = v => Math.min(100, Math.max(0, v / 300 * 100)).toFixed(2) + '%';
  const lab = v => v === Infinity ? 'no position' : (v > 300 ? Math.round(v) + '%' : Math.round(v) + '%');
  const pos = v => v === Infinity ? '100%' : x(v);
  const afterCls = after < ACC.stop ? 'bad' : '';
  return `<div class="gauge" role="img" aria-label="Margin level now ${lab(now)}, after ${lab(after)}. Margin call at 80%, liquidation at 40%.">
    <div class="track"></div>
    <div class="mk l" style="left:${x(ACC.stop)}"><span>40% liquidation</span></div>
    <div class="mk r" style="left:${x(ACC.call)}"><span>80% call</span></div>
    ${after == null ? '' : `<div class="pt now low ${parseFloat(pos(now)) > 85 ? 'rt' : ''}" style="left:${pos(now)}"><i></i>now ${lab(now)}</div>`}
    <div class="pt ${after == null ? 'after' : 'after ' + afterCls} ${parseFloat(pos(after == null ? now : after)) > 85 ? 'rt' : ''}" style="left:${pos(after == null ? now : after)}">${after == null ? 'now ' + lab(now) : (opts.afterWord || 'after') + ' ' + lab(after)}<i></i></div>
  </div>`;
}

// ---------- Margin chase states (sample) ----------
// Both samples chase on the buy side, so the price rail of the spot prototype applies:
//  open: open a 3x long, 0.0500 BTC;  close: close the 0.0300 BTC short (a reduce-only buy).
const F1 = { q: 0.018, p: 62418.1, liq: 'm' };
const F2 = { q: 0.032, p: 62418.3, liq: 'm' };
const C1 = { q: 0.012, p: 62418.1, liq: 'm' };
const C2 = { q: 0.018, p: 62418.3, liq: 'm' };
const ob = [
  ['0:00', 'Recorded the start ask, 62,418.50, as the cap.', ''],
  ['0:00', 'Safety timer on: 60 s, refreshed every 20 s.', ''],
  ['0:00', 'Placed a post-only buy, 0.0500 BTC at 62,417.90, leverage 3x.', 'ev-you'],
];
const oa = [['0:06', 'Best bid rose to 62,418.10. Amended the order to 62,418.10.', 'ev-you']];
const of1 = [['0:31', 'Filled 0.0180 BTC at 62,418.10 (maker). Position opened: 0.0180 BTC long, 3x.', 'ev-fill']];
const cb = [
  ['0:00', 'Read the position: 0.0300 BTC short, 3x.', ''],
  ['0:00', 'Recorded the start ask, 62,418.50, as the cap.', ''],
  ['0:00', 'Safety timer on: 60 s, refreshed every 20 s.', ''],
  ['0:00', 'Placed a post-only buy, reduce-only, 0.0300 BTC at 62,417.90.', 'ev-you'],
];
const cf1 = [['0:24', 'Filled 0.0120 BTC at 62,418.10 (maker). Position partly closed: 0.0180 BTC short stays open.', 'ev-fill']];
const ca2 = [['0:33', 'Best bid rose to 62,418.30. Amended the order to 62,418.30.', 'ev-you']];

const OPEN = { kind: 'open', dir: 'long', lev: 3, qty: 0.05, order: 'Open long 0.0500 BTC, 3x' };
const CLOSE = { kind: 'close', dir: 'short', lev: 3, qty: 0.03, order: 'Close short 0.0300 BTC' };
const STAYS_OPEN = 'If the tool stops, Kraken cancels the order (safety timer). The position does not close: it stays open, with rollover fees and liquidation risk.';

const MSTATES = [
  Object.assign({}, OPEN, { id: 'm-open-resting', label: 'Open: resting', tone: 'you', pulse: true, t: 4, fills: [], you: 62417.9, bid: 62417.9, ask: 62418.5, rate: 2, conn: 'ok', dms: 4,
    pos: ['muted', 'Not opened yet', 'Nothing filled, so no position is open.'],
    title: 'Resting at the best bid',
    sub: 'Your order to open a 3x long waits at 62,417.90. When the best bid rises, the tool moves the order up, at most once every 5 s. The order never goes above the cap, 62,418.50.',
    events: ob.slice(), actions: ['stop', 'fillnow'] }),
  Object.assign({}, OPEN, { id: 'm-open-partial', label: 'Open: part filled', tone: 'fill', pulse: true, t: 47, fills: [F1], you: 62418.3, bid: 62418.3, ask: 62418.5, rate: 6, conn: 'ok', dms: 7,
    pos: ['fill', 'Opened: 0.0180 of 0.0500 BTC', 'Long, 3x. This part is a position now.'],
    title: 'Partly filled: 36%',
    sub: '0.0180 BTC filled at 62,418.10 as maker. That part is an open 3x long position now. The rest, 0.0320 BTC, rests at the best bid, 62,418.30.',
    events: ob.concat(oa, of1, [['0:37', 'Best bid rose to 62,418.30. Amended the order to 62,418.30.', 'ev-you']]), actions: ['stop', 'fillnow'] }),
  Object.assign({}, OPEN, { id: 'm-replace', label: 'Amend refused: cancel and replace', tone: 'warn', pulse: true, t: 37, fills: [F1], you: null, pending: 62418.3, bid: 62418.3, ask: 62418.5, rate: 22, conn: 'ok', dms: 3,
    pos: ['fill', 'Opened: 0.0180 of 0.0500 BTC', 'Long, 3x. The position does not change during the replace.'],
    title: 'Amend refused: the tool cancels and replaces',
    sub: 'Kraken refused to amend this margin order (sample reason: "EOrder:Invalid arguments"). Kraken does not document amends for margin orders. So the tool moves the order in 4 steps:',
    steps: [['done', 'Cancel the order at 62,418.10.'], ['done', 'Wait for Kraken to confirm the cancel.'], ['done', 'Read the fills again: 0.0180 BTC filled.'], ['now', 'Place a new post-only buy for the rest, 0.0320 BTC at 62,418.30, leverage 3x.']],
    replace: true,
    events: ob.concat(oa, of1, [['0:37', 'Best bid rose to 62,418.30. Amend to 62,418.30 refused: "EOrder:Invalid arguments" (sample).', 'ev-warn'], ['0:37', 'Switched to cancel and replace for this chase. Cancelled the order.', 'ev-warn'], ['0:37', 'Kraken confirmed the cancel. Filled so far: 0.0180 BTC.', ''], ['0:37', 'Placing a post-only buy, 0.0320 BTC at 62,418.30, leverage 3x…', 'ev-you']]),
    actions: ['stop', 'fillnow'] }),
  Object.assign({}, OPEN, { id: 'm-open-filled', label: 'Open: filled', tone: 'fill', t: 78, fills: [F1, F2], you: null, bid: 62418.2, ask: 62418.5, rate: 3, conn: 'ok', dms: null, done: true,
    pos: ['fill', 'Opened: 0.0500 BTC long, 3x', 'Open until you close it, also when the tool stops.'],
    title: 'Position opened',
    sub: 'All 0.0500 BTC filled as maker, at an average of 62,418.23. Your 3x long position is open. The safety timer is off, because no order rests. The position stays open until you close it.',
    todo: ['Rollover: up to 1.56 USD for each started 4 h while the position is open (estimate at the 0.05% maximum).', 'To close it, use New chase, then Close a position.'],
    events: ob.concat(oa, of1, [['0:37', 'Best bid rose to 62,418.30. Amended the order to 62,418.30.', 'ev-you'], ['1:18', 'Filled 0.0320 BTC at 62,418.30 (maker). Order complete. Position opened: 0.0500 BTC long, 3x.', 'ev-fill'], ['1:18', 'Safety timer off.', '']]), actions: ['result', 'close'] }),
  Object.assign({}, OPEN, { id: 'm-open-stopped', label: 'Open: stopped by you', tone: 'muted', t: 55, fills: [F1], you: null, bid: 62418.3, ask: 62418.5, rate: 7, conn: 'ok', dms: null, done: true,
    pos: ['fill', 'Opened: 0.0180 BTC long, 3x', 'The part that filled stays open.'],
    title: 'Stopped by you',
    sub: 'You stopped the chase. The tool cancelled the order and Kraken confirmed the cancel. 0.0180 BTC filled before the stop. That part stays open as a 3x long position.',
    events: ob.concat(oa, of1, [['0:55', 'You pressed Stop. Cancelled the order.', ''], ['0:55', 'Kraken confirmed the cancel. Position stays open: 0.0180 BTC long, 3x. Safety timer off.', '']]), actions: ['result', 'close'] }),
  Object.assign({}, OPEN, { id: 'm-open-ended', label: 'Open: tool stopped or restarted', tone: 'bad', t: 70, fills: [F1], you: null, bid: 62418.4, ask: 62418.5, rate: 0, conn: 'ok', dms: null, done: true,
    pos: ['fill', 'Opened: 0.0180 BTC long, 3x', 'Stays open. A restart does not close it.'],
    title: 'Ended: the tool stopped or restarted',
    sub: 'The tool stopped at 1:02 and started again at 1:10 (sample cause: a crash). After a restart the tool does not continue a chase. It did these steps:',
    steps: [['done', 'Read the order from Kraken: open, 0.0180 BTC filled.'], ['done', 'Cancelled the resting order and waited for Kraken to confirm.'], ['done', 'Read the positions: 0.0180 BTC long, 3x, open.'], ['done', 'Did not continue the chase. Safety timer off.']],
    todo: ['Your position of 0.0180 BTC long, 3x, is still open. It pays rollover and can be liquidated.', 'To close it, start a close chase. To open the rest, start a new open chase.', 'The tool was off for 8 s, so the safety timer did not fire.'],
    note: 'The safety timer cancels orders only. It never closes a position. A position stays open while the tool is off, for any time.',
    events: ob.concat(oa, of1, [['1:02', 'Last event from the tool before it stopped.', ''], ['1:10', 'Tool started again. Read the order: open, 0.0180 BTC filled.', 'ev-bad'], ['1:10', 'Cancelled the resting order. Kraken confirmed the cancel.', ''], ['1:10', 'Position still open: 0.0180 BTC long, 3x. Chase ended, not continued.', '']]), actions: ['result', 'close'] }),
  Object.assign({}, CLOSE, { id: 'm-close-resting', label: 'Close: resting', tone: 'you', pulse: true, t: 5, fills: [], you: 62417.9, bid: 62417.9, ask: 62418.5, rate: 2, conn: 'ok', dms: 5,
    pos: ['you', 'Open: 0.0300 BTC short, 3x', 'Closing. Nothing closed yet.'],
    title: 'Resting at the best bid',
    sub: 'Your reduce-only buy waits at 62,417.90. Reduce-only means the order can only make the short smaller. It can never open a long. The order never goes above the cap, 62,418.50.',
    events: cb.slice(), actions: ['stop', 'fillnow'] }),
  Object.assign({}, CLOSE, { id: 'm-close-partial', label: 'Close: part filled', tone: 'fill', pulse: true, t: 40, fills: [C1], you: 62418.3, bid: 62418.3, ask: 62418.5, rate: 5, conn: 'ok', dms: 6,
    pos: ['fill', 'Partly closed: 0.0120 of 0.0300 BTC', 'Still open: 0.0180 BTC short, 3x.'],
    title: 'Partly closed: 40%',
    sub: '0.0120 BTC of the short closed at 62,418.10 as maker. The rest, 0.0180 BTC, rests at the best bid, 62,418.30, reduce-only.',
    events: cb.concat(cf1, ca2), actions: ['stop', 'fillnow'] }),
  Object.assign({}, CLOSE, { id: 'm-close-closed', label: 'Close: closed', tone: 'fill', t: 66, fills: [C1, C2], you: null, bid: 62418.2, ask: 62418.5, rate: 3, conn: 'ok', dms: null, done: true,
    pos: ['fill', 'Closed', 'The BTC/USD short is closed. No rollover from now on.'],
    title: 'Position closed',
    sub: 'All 0.0300 BTC of the short closed as maker, at an average of 62,418.22. The position is closed. The safety timer is off.',
    events: cb.concat(cf1, ca2, [['1:06', 'Filled 0.0180 BTC at 62,418.30 (maker). Position closed.', 'ev-fill'], ['1:06', 'Safety timer off.', '']]), actions: ['result', 'new'] }),
  Object.assign({}, CLOSE, { id: 'm-close-notfilled', label: 'Close: rest not filled (above cap)', tone: 'bad', t: 121, fills: [C1], you: null, bid: 62429.4, ask: 62431.0, rate: 10, conn: 'ok', dms: null, done: true,
    pos: ['warn', 'Partly closed: 0.0120 of 0.0300 BTC', 'Still open: 0.0180 BTC short, 3x.'],
    title: 'Stopped: the rest did not close',
    sub: 'The price rose above your cap. The ask is now 62,431.00, which is 12.50 above the cap of 62,418.50. The reduce-only IOC filled nothing. 0.0180 BTC of the short stays open, with rollover fees and liquidation risk.',
    events: cb.concat(cf1, ca2, [['2:00', 'Timeout. Cancelled the resting order.', 'ev-warn'], ['2:00', 'Kraken confirmed the cancel. Closed so far: 0.0120 BTC.', ''], ['2:01', 'Reduce-only IOC buy, 0.0180 BTC at 62,418.50: nothing filled. The ask is 62,431.00.', 'ev-bad'], ['2:01', 'Chase ended. Position still open: 0.0180 BTC short. Safety timer off.', '']]), actions: ['result', 'closeagain'] }),
  Object.assign({}, CLOSE, { id: 'm-close-stopped', label: 'Close: stopped by you', tone: 'muted', t: 30, fills: [C1], you: null, bid: 62418.3, ask: 62418.5, rate: 5, conn: 'ok', dms: null, done: true,
    pos: ['warn', 'Partly closed: 0.0120 of 0.0300 BTC', 'Still open: 0.0180 BTC short, 3x.'],
    title: 'Stopped by you',
    sub: 'You stopped the chase. The tool cancelled the order and Kraken confirmed the cancel. 0.0120 BTC of the short closed before the stop. 0.0180 BTC of the short stays open.',
    events: cb.concat(cf1, [['0:30', 'You pressed Stop. Cancelled the order.', ''], ['0:30', 'Kraken confirmed the cancel. Position still open: 0.0180 BTC short. Safety timer off.', '']]), actions: ['result', 'closeagain'] }),
  Object.assign({}, CLOSE, { id: 'm-liquidated', label: 'Liquidated', tone: 'bad', t: 44, fills: [C1], you: null, bid: 62418.3, ask: 62418.5, rate: 5, conn: 'ok', dms: null, done: true, norail: true,
    pos: ['bad', 'Liquidated', 'Kraken closed the rest of the short, 0.0180 BTC.'],
    title: 'Ended: Kraken liquidated the position',
    sub: 'The account margin level fell to 40%, so Kraken closed the rest of the short, 0.0180 BTC, at the market (sample event). Your reduce-only order had nothing left to close. The tool cancelled it and ended the chase.',
    todo: ['Check the liquidation price and any fee in Kraken Pro. The tool shows what the executions feed reported.', 'Check your other positions: the margin level is for the whole account.'],
    events: cb.concat(cf1, ca2, [['0:44', 'Executions feed: position liquidated, 0.0180 BTC short closed by Kraken.', 'ev-bad'], ['0:44', 'Cancelled the reduce-only order. Kraken confirmed the cancel.', ''], ['0:44', 'Chase ended. Safety timer off.', '']]), actions: ['result', 'new'] }),
];

// ---------- Margin decisions (owner) and open questions ----------
const MDECISIONS = [
  { id: 'M1', by: 'owner', on: ['new'], t: 'What margin can do', a: 'Open a long (buy) or a short (sell), and close a position. Spot buy and sell stay as they are.' },
  { id: 'M2', by: 'owner', on: ['new'], t: 'Leverage', a: 'You pick 2x, 3x, 4x or 5x for each open. Never above 5x, and only the levels that the pair allows.' },
  { id: 'M3', by: 'owner', on: ['new', 'chase'], t: 'Close', a: 'Always reduce-only: a close can never flip or grow a position. The size starts at the whole position. You can make it smaller.' },
  { id: 'M4', by: 'owner', on: ['new', 'chase'], t: 'Liquidation guard', a: 'No guard. The form shows the account margin level now and after the open, with the 80% call and 40% liquidation marks.' },
  { id: 'M5', by: 'owner', on: ['chase'], t: 'Amend refused', a: 'The tool cancels and replaces: cancel, wait for the confirmation, read the fills again, place a new post-only order. At most every 5 s.' },
  { id: 'M6', by: 'owner', on: ['new', 'setup'], t: 'Order of work', a: 'Margin comes after the spot dry run. Dry run first. A very small live test later, only with your go-ahead.' },
];
const MQUESTIONS = [
  { id: 'MQ1', on: ['new'], t: 'Which leverage does the form start at?', opts: ['2x, the lowest', 'No level: you must pick one', 'The level of your last open'], rec: 0,
    why: 'The lowest level is the safest start. A forced pick adds a click to every open, and "last used" can carry 5x into a chase by mistake.' },
  { id: 'MQ2', on: ['new'], t: 'Does the worst case include rollover?', opts: ['No: the worst case is the open (price, trading fee, opening fee). Rollover shows apart, per 4 h and per day', 'Yes, for one day of holding', 'Yes, for a hold time that you type'], rec: 0,
    why: 'The chase ends at the open. The hold time is your choice after it, so a fixed hold time puts a guess into a number that is otherwise a true maximum.' },
  { id: 'MQ3', on: ['new'], t: 'A part close leaves a rest below the Kraken minimum', opts: ['Warn, and offer "Close all" with one click; Start stays possible', 'Block Start until the size leaves 0 or at least the minimum', 'Allow without a warning'], rec: 0,
    why: 'Kraken refuses an order below the minimum, so a tiny rest can be hard to close later. A warning keeps your choice; a block can surprise you.' },
  { id: 'MQ4', on: ['new'], t: 'How does the close list show positions?', opts: ['One row per pair and direction (all opens of BTC/USD short together)', 'One row per open, as Kraken records them'], rec: 0,
    why: 'A reduce-only order closes against the pair, not against one open. One row per pair and direction matches what the order does.' },
  { id: 'MQ5', on: ['new', 'chase'], t: 'Which account does a margin dry run use?', opts: ['Your real balance when the key has Query Funds; otherwise a simulated 5,000 USD. Simulated liquidation at 40%', 'Always a simulated 5,000 USD', 'No margin level in a dry run'], rec: 0,
    why: 'Your real balance gives a true margin level for the test. Without the key, a labelled simulated balance still shows the margin parts.' },
  { id: 'MQ6', on: ['chase'], t: 'Kraken liquidates during an open chase', opts: ['End the chase: cancel the order and stop', 'Continue the chase for the rest'], rec: 0,
    why: 'A liquidation means the account is at 40%. More leverage at that time can make it worse, and you did not see the new margin level.' },
];
const mqHTML = q => `<div class="q" id="d-${q.id}"><span class="qid">${q.id}</span> <span class="badge warn">Open question</span><h3 style="margin-top:6px">${q.t}</h3>
  <ol class="small" style="margin:0 0 6px;padding-left:20px">${q.opts.map((o, i) => `<li>${o}${i === q.rec ? ' <span class="badge ok">recommended, default</span>' : ''}</li>`).join('')}</ol>
  <p class="tiny muted" style="margin:0">${q.why}</p></div>`;

// Margin decisions join the decision drawer on each screen.
window.OC.DECISIONS.push.apply(window.OC.DECISIONS, MDECISIONS);
window.OC.MQUESTIONS = MQUESTIONS;
Object.assign(window.OC, { ACC, POS, LEV, MF, MSTATES, MDECISIONS, MQUESTIONS, mqHTML, openCalc, gauge, used, level, STAYS_OPEN });
})();
