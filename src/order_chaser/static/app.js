/* Order chaser page: shared chrome, formatting, server calls, and the live-chase renderers. */
'use strict';
const OC = (() => {
const TOKEN = document.querySelector('meta[name="oc-token"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

// ---------- Formatting ----------
const n = v => v == null ? null : Number(v);
const px = (v, d) => v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: Math.max(2, d || 2), maximumFractionDigits: Math.max(2, d || 2) });
const usd = v => Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = v => { const s = Number(v).toFixed(8).replace(/0+$/, ''); const [w, f] = s.split('.'); return w + '.' + (f || '').padEnd(4, '0'); };
const mmss = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const timeoutWords = s => s % 60 === 0 ? (s / 60) + ' min' : s + ' s';
// Whole percent, rounded down, without the float error of 0.018 / 0.05 * 100 = 35.999…
const pct = (a, b) => Math.floor(Number(a) / Number(b) * 100 + 1e-9);
// One rule for the words of a fill: "Not filled" with 0, "Part filled" below the amount.
const fillWord = (filled, total) => Number(filled) <= 0 ? 'Not filled' : Number(filled) < Number(total) ? 'Part filled' : 'Filled';
// The button after a chase that did not fill all: the rest, or the whole amount again when nothing filled.
const againText = c => { const rest = Number(c.qty) - Number(c.filled); return `${Number(c.filled) > 0 ? 'Chase the rest' : 'Chase again'} (${qty(rest)} ${c.pair.base})`; };
const clock = t => new Date(t * 1000).toLocaleTimeString('en-GB');
// A rest below a Kraken minimum cannot be chased: the server sets c.rest_below_min with the rule of the core.
const restBelowMin = c => `The rest, ${qty(Number(c.qty) - Number(c.filled))} ${c.pair.base}, is below the Kraken minimum (${qty(c.pair.ordermin)} ${c.pair.base} or ${c.pair.costmin} ${c.pair.quote}). You cannot chase it.`;

// ---------- Margin ----------
// "Open long 0.0500 BTC, 3x" or "Close short 0.0300 BTC"; spot: "Buy 0.0500 BTC".
const orderName = c => !c.margin ? `${c.side === 'buy' ? 'Buy' : 'Sell'} ${qty(c.qty)} ${c.pair.base}`
  : c.margin.close ? `Close ${c.dir} ${qty(c.qty)} ${c.pair.base}` : `Open ${c.dir} ${qty(c.qty)} ${c.pair.base}, ${c.margin.leverage}x`;
const MARGIN_FEES = 'Kraken US margin fees are 0.01% to 0.05% of the position cost. Kraken can change them without notice, and no API gives them, so the tool counts the stated maximum, 0.05%.';
// The account margin level on a scale of 0% to 300%, with the call and liquidation marks of the pair (AssetPairs).
// now, after: percent or null (no position). after === undefined: only "now".
function gauge(now, after, call, stop, afterWord) {
  const x = v => Math.min(100, Math.max(0, v / 300 * 100)).toFixed(2) + '%';
  const lab = v => v == null ? 'no position' : Math.round(v) + '%';
  const pos = v => v == null ? '100%' : x(v);
  const rt = v => (v == null || v > 255) ? ' rt' : '';
  const one = after === undefined;
  const shown = one ? now : after;
  const aria = one ? `Margin level now ${lab(now)}.` : `Margin level now ${lab(now)}, ${afterWord || 'after'} ${lab(after)}.`;
  return `<div class="gauge" role="img" aria-label="${aria} Margin call at ${call}%, liquidation at ${stop}%.">
    <div class="track"></div>
    <div class="mk l" style="left:${x(stop)}"><span>${stop}% liquidation</span></div>
    <div class="mk r" style="left:${x(call)}"><span>${call}% call</span></div>
    ${one ? '' : `<div class="pt now${rt(now)}" style="left:${pos(now)}"><i></i>now ${lab(now)}</div>`}
    <div class="pt after${shown != null && shown <= stop ? ' bad' : ''}${rt(shown)}" style="left:${pos(shown)}">${one ? 'now' : (afterWord || 'after')} ${lab(shown)}<i></i></div>
  </div>`;
}

// The prices behind the account margin level. The tool watches one pair: another pair uses the last price seen
// of it, and after a restart a pair has no price until the tool watches it again. '' when every price is live.
function markNote(a, watched) {
  const rows = a ? a.positions.filter(p => p.pair !== watched) : [];
  return rows.map(p => p.mark == null ? `No price yet for ${esc(p.pair)}. The level counts its profit or loss as 0.`
    : `The level uses ${esc(p.pair)} at the last price seen, ${clock(p.mark_at)}.`).join(' ');
}

// After a margin chase: is a position of this chase still open, and the link to close it.
// A close: what stays comes from its close plan (c.margin_est, the core's FIFO plan of what filled).
const positionOpen = c => !['liquidated', 'nopos'].includes(c.outcome) &&
  (c.margin.close ? Number(c.margin_est.rest) > 0 : Number(c.filled) > 0);
const closeLink = c => `/new?what=close&pair=${encodeURIComponent(c.pair.symbol)}&dir=${c.dir}`;
const closeText = c => c.margin.close ? `Close the rest (${qty(c.margin_est.rest)} ${c.pair.base}), new ${c.side === 'buy' ? 'cap' : 'floor'}` : 'Close this position';
// A close in plain words, from its close plan: which positions it takes and what stays.
// Before a fill: the plan for the whole close. Then: what the fills took so far, or at the end.
function planLine(c) {
  const e = c.margin_est, f = Number(c.filled);
  if (!f) return c.phase === 'done' ? `Closed nothing. The plan was: ${e.plan}.` : `Closes, oldest first: ${e.plan}.`;
  return `${c.phase === 'done' ? 'Closed' : 'Closed so far'}: ${e.takes}. ${e.stays ? `Stays open: ${e.stays}.` : `Nothing stays open: the ${c.dir} is closed.`}`;
}

// ---------- Server calls ----------
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Token': TOKEN }, body: JSON.stringify(body || {}) });
  let data = {};
  try { data = await r.json(); } catch (e) { data = { errors: [await r.text().catch(() => 'Request refused.')] }; }
  return { ok: r.ok, data };
}

let lastSnap = null, lastAt = 0, lastNow = 0, offline = false;
function stream(onSnap, onOffline) {
  const es = new EventSource('/api/stream');
  es.onmessage = e => { lastSnap = JSON.parse(e.data); lastAt = Date.now(); lastNow = lastSnap.now; offline = false; conn(lastSnap); onSnap(lastSnap); };
  es.onerror = () => { offline = true; conn(null); if (onOffline) onOffline(lastSnap, (Date.now() - lastAt) / 1000); };
  setInterval(() => { if (offline && onOffline) onOffline(lastSnap, (Date.now() - lastAt) / 1000); }, 1000);
}

// ---------- Chrome: top bar and the dry-run marker on every page ----------
function chrome(page) {
  const app = document.querySelector('.app');
  const top = document.createElement('div');
  top.className = 'top';
  const on = p => p.includes(page) ? 'on' : '';
  top.innerHTML = '<div class="brand"><span class="dot"></span>Order chaser</div>' +
    `<nav class="nav"><a href="/" class="${on(['new', 'chase'])}">Chase</a><a href="/history" class="${on(['history', 'result'])}">History</a><a href="/setup" class="${on(['setup'])}">Setup</a></nav>` +
    '<span class="spacer"></span><span class="badge dry">DRY RUN: no real orders</span><span class="conn" id="conn" role="status"><i></i><span>Kraken feed: connecting…</span></span>' +
    `<span class="badge plain mono">${esc(location.host)}</span>`;
  app.prepend(top);
  const strip = document.createElement('div');
  strip.className = 'drystrip';
  strip.style.marginBottom = '16px';
  strip.id = 'drystrip';
  strip.textContent = 'DRY RUN: no real orders. No order goes to Kraken. Prices are live public prices; fills are simulated.';
  top.after(strip);
}

function conn(snap) {
  const el = $('conn');
  if (!el) return;
  let cls = 'conn', text = 'Kraken feed: connected';
  if (!snap) { cls += ' bad'; text = 'Tool: not reachable'; }
  else if (!snap.feed.up) { cls += ' bad'; text = 'Kraken feed: lost, reconnecting'; }
  else if (!snap.feed.ok) { cls += ' warn'; text = 'Kraken feed: reading the book'; }
  el.className = cls;
  if (el.lastChild.textContent !== text) el.lastChild.textContent = text;
}

// ---------- The live chase: which design state, and its copy ----------
function stateOf(c, now) {
  if (c.phase === 'done') return c.outcome;
  // A cancel and replace in flight (or waiting for a price for the new leg).
  if (c.replace && c.exit == null && (['cancelling', 'reread', 'placing'].includes(c.phase) || (c.phase === 'resting' && c.price == null))) return 'replacing';
  if (c.phase === 'placing') return 'placing';
  if (c.phase === 'amending') return 'amending';
  if (c.phase === 'feed_lost' || (c.phase === 'reconcile' && c.reconcile_for === 'feed')) return 'disconnected';
  if (c.phase === 'reconcile' || (c.reject && !c.replace && now - c.reject_at < 10)) return 'rejected';
  if (['cancelling', 'reread', 'ioc'].includes(c.phase)) return c.exit === 'stop' ? 'stopping' : 'fallback';
  if (c.slow) return 'ratenear';
  return Number(c.filled) > 0 ? 'partial' : 'resting';
}

const LABEL = {
  placing: 'Placing', resting: 'Resting', amending: 'Amending', partial: 'Partial fill', filled: 'Filled', fallback: 'Timeout fallback',
  notfilled: 'Rest not filled', belowmin: 'Rest below the minimum', rejected: 'Amend rejected', disconnected: 'Disconnected',
  ratenear: 'Rate limit near', ended: 'Ended: tool stopped or restarted', stopped: 'Stopped by you', stopping: 'Stopping', refused: 'Order rejected',
  pageoffline: 'Page lost the tool', cancelfail: 'Cancel failed',
  replacing: 'Amend refused: cancel and replace', liquidated: 'Liquidated', nopos: 'Ended: position closed',
};
const TONE = {
  placing: 'you', resting: 'you', amending: 'you', partial: 'fill', filled: 'fill', fallback: 'warn', notfilled: 'bad', belowmin: 'bad',
  rejected: 'warn', disconnected: 'bad', ratenear: 'warn', ended: 'bad', stopped: 'muted', stopping: 'muted', refused: 'bad', pageoffline: 'bad', cancelfail: 'bad',
  replacing: 'warn', liquidated: 'bad', nopos: 'warn',
};
const DONE = ['filled', 'notfilled', 'belowmin', 'stopped', 'ended', 'refused', 'cancelfail', 'liquidated', 'nopos'];
const SIM = ' (Simulated. No order goes to Kraken.)';
const DRY_TODO = 'Nothing to check in Kraken Pro: a dry run sends no orders.';
const LIVE_CANCEL_TODO = 'Check Kraken Pro for an open order and cancel it there.';

function words(c) {
  const buy = c.side === 'buy', pd = c.pair.price_decimals, B = c.pair.base;
  return {
    buy, B, pd, p: v => px(v, pd),
    best: buy ? 'best bid' : 'best ask', other: buy ? 'ask' : 'bid', limitWord: buy ? 'cap' : 'floor',
    up: buy ? 'up' : 'down', rises: buy ? 'rises' : 'falls', above: buy ? 'above' : 'below',
    bought: c.margin ? (c.margin.close ? 'closed' : 'opened') : buy ? 'bought' : 'sold', Bought: buy ? 'Bought' : 'Sold',
    limitName: c.limit === (buy ? c.start_ask : c.start_bid) ? (buy ? 'cap (start ask)' : 'floor (start bid)') : (buy ? 'cap (your limit)' : 'floor (your limit)'),
    m: c.margin, close: !!(c.margin && c.margin.close), dir: c.dir, lev: c.margin ? c.margin.leverage : null,
    // The words of the order in a sentence: "order to open a 3x long", "reduce-only buy", "order".
    order: !c.margin ? 'order' : c.margin.close ? `reduce-only ${c.side}` : `order to open a ${c.margin.leverage}x ${c.dir}`,
  };
}

// Margin: what the position of this chase is now, in one sentence. Spot: ''.
function positionLine(c) {
  if (!c.margin) return '';
  const B = c.pair.base, f = Number(c.filled);
  if (!c.margin.close) return f > 0 ? ` That part is an open ${c.margin.leverage}x ${c.dir} position now${c.mode === 'live' ? '' : ' (simulated)'}.` : ' Nothing filled, so no position opened.';
  return c.margin_est.stays ? ` Stays open: ${c.margin_est.stays}.` : ` The ${c.dir} position is closed.`;
}

function copy(st, c, snap, ageOff) {
  const w = words(c), p = w.p, B = w.B;
  const filled = Number(c.filled), rest = Number(c.qty) - filled, s = c.summary;
  const bestNow = w.buy ? c.bid : c.ask;
  const t = { title: '', sub: '', steps: null, todo: null, note: null };
  const timeout = mmss(c.timeout);
  switch (st) {
    case 'placing':
      t.title = 'Placing your order';
      t.sub = `The tool simulates a post-only ${c.side} for ${qty(c.qty)} ${B} at the ${w.best}, ${p(c.pending)}. Post-only means the order never takes liquidity, so you pay the maker fee.` + SIM;
      break;
    case 'resting':
      if (n(c.price) === n(c.limit)) {
        t.title = `Resting at the ${w.limitWord}`;
        t.sub = `Your order waits at the ${w.limitWord}, ${p(c.limit)}. The order never goes ${w.above} the ${w.limitWord}, so the tool does not move it when the ${w.best} ${w.rises} further.` + SIM;
        break;
      }
      t.title = `Resting at the ${w.best}`;
      t.sub = (w.close
        ? `Your reduce-only ${c.side} waits at ${p(c.price)}. Reduce-only means the order can only make the ${w.dir} smaller. It can never open a ${w.dir === 'long' ? 'short' : 'long'}. The order never goes ${w.above} the ${w.limitWord}, ${p(c.limit)}.`
        : `Your ${w.order} waits at ${p(c.price)}. When the ${w.best} ${w.rises}, the tool moves the order ${w.up}, at most once every 5 s. The order never goes ${w.above} the ${w.limitWord}, ${p(c.limit)}.`) + replaceNote(c) + SIM;
      break;
    case 'amending':
      t.title = `Moving your order ${w.up}`;
      t.sub = `Another ${w.buy ? 'buyer raised the best bid' : 'seller lowered the best ask'} to ${p(c.pending)}. The tool amends your order to that price. The order keeps the same id and its fill history.` + SIM;
      break;
    case 'partial': {
      const where = c.price === bestNow ? `the ${w.best}, ${p(c.price)}` : p(c.price);
      if (w.close) {
        t.title = `Partly closed: ${pct(filled, c.qty)}%`;
        t.sub = `${qty(filled)} ${B} of the ${w.dir} closed at ${p(s.avg)} as maker. The rest, ${qty(rest)} ${B}, rests at ${where}, reduce-only.` + replaceNote(c) + SIM;
        break;
      }
      t.title = `Partly filled: ${pct(filled, c.qty)}%`;
      t.sub = `${qty(filled)} ${B} filled at ${p(s.avg)} as maker.${positionLine(c)} The rest, ${qty(rest)} ${B}, rests at ${where}.` + replaceNote(c) + SIM;
      break;
    }
    case 'replacing': {
      const read = c.phase === 'reread', waiting = c.phase === 'resting';
      const mark = i => { const at = c.phase === 'cancelling' ? 1 : read ? 2 : 3; return i < at ? 'done' : i === at ? 'now' : 'todo'; };
      t.title = 'Amend refused: the tool cancels and replaces';
      t.sub = `The simulated exchange refused to amend this margin order (reason: "${esc(c.reject || '')}"). Kraken does not document amends for margin orders. So the tool moves the order in 4 steps:`;
      t.steps = [['done', 'Cancel the order.'],
        [mark(1), 'Wait for the simulated exchange to confirm the cancel.'],
        [mark(2), 'Read the fills again' + (read ? '.' : `: ${qty(filled)} ${B} filled.`)],
        [mark(3), waiting ? `Place a new post-only ${c.side} for the rest, ${qty(rest)} ${B}, when the price is valid.`
          : `Place a new post-only ${c.side} for the rest, ${qty(rest)} ${B}${c.pending ? ' at ' + p(c.pending) : ''}${w.close ? ', reduce-only' : ', leverage ' + w.lev + 'x'}.`]];
      t.note = REPLACE_COST;
      break;
    }
    case 'filled': {
      const taker = Number(s.taker_qty);
      if (w.m) {
        const how = taker > 0 ? `${qty(s.maker_qty)} as maker and ${qty(taker)} as taker (IOC)` : 'as maker';
        if (w.close) {
          const left = Number(c.margin_est.rest);
          t.title = left > 0 ? `Closed ${qty(filled)} ${B} of the ${w.dir}` : 'Position closed';
          t.sub = `All ${qty(c.qty)} ${B} of the close filled ${how}, at an average of ${p(s.avg)}.` + positionLine(c) + ' (Simulated.)';
        } else {
          t.title = 'Position opened';
          t.sub = `All ${qty(c.qty)} ${B} filled ${how}, at an average of ${p(s.avg)}. Your ${w.lev}x ${w.dir} position is open (simulated). It stays open until you close it.`;
          t.todo = [`Rollover: up to ${usd(c.margin_est.rollover_4h)} ${c.pair.quote} for each 4 h that the position is open (estimate at the 0.05% maximum).`, 'To close it, use "Close this position".'];
        }
        break;
      }
      t.title = 'Filled';
      t.sub = taker > 0
        ? `All ${qty(c.qty)} ${B} filled, at an average of ${p(s.avg)}: ${qty(s.maker_qty)} as maker and ${qty(taker)} as taker (IOC). (Simulated.)`
        : `All ${qty(c.qty)} ${B} filled as maker, at an average of ${p(s.avg)}. (Simulated.)`;
      break;
    }
    case 'fallback': {
      const now = c.phase;
      const chaseFilled = c.fills.filter(f => f.order === 'chase').reduce((x, f) => x + Number(f.qty), 0);
      const mark = (i) => { const order = ['cancelling', 'reread', 'ioc']; const at = order.indexOf(now) + 1; return i < at ? 'done' : i === at ? 'now' : 'todo'; };
      if (!c.feed_ok) {
        t.title = 'Time is up: the price feed is lost';
        t.sub = `The ${timeout} timeout passed while the public Kraken price feed was lost. With no valid price, the tool sends no IOC. It cancels the order, and the rest counts as not filled.`;
        t.steps = [['done', 'Cancel the resting order.'], [mark(1), 'Wait for the simulated exchange to confirm the cancel.'],
          [mark(2), 'Read the filled quantity again.'], ['todo', 'No IOC: there is no valid price.']];
        break;
      }
      t.title = c.exit === 'fillnow' ? 'Filling the rest now' : 'Time is up: filling the rest';
      t.sub = (c.exit === 'fillnow' ? 'You pressed "Fill the rest now".' : `The ${timeout} timeout passed.`) +
        ` The tool now fills the rest with one IOC limit at the ${w.limitWord}. IOC means "fill now what you can, cancel the rest". It does these steps in this order:`;
      t.steps = [['done', 'Cancel the resting order.'],
        [mark(1), 'Wait for the simulated exchange to confirm the cancel.'],
        [mark(2), 'Read the filled quantity again' + (now === 'ioc' ? `: ${qty(chaseFilled)} ${B}.` : '.')],
        [mark(3), `Send an IOC ${c.side} for the rest${now === 'ioc' ? ', ' + qty(Number(c.qty) - chaseFilled) + ' ' + B : ''}, at the ${w.limitWord}, ${p(c.limit)}.`]];
      break;
    }
    case 'stopping':
      t.title = 'Stopping: cancelling the order';
      t.sub = `The tool cancels your order and waits for the simulated exchange to confirm. You keep what filled: ${qty(filled)} ${B}. The rest does not fill.` + positionLine(c);
      break;
    case 'notfilled': {
      const end = c.end_ask;
      const iocGot = c.fills.filter(f => f.order === 'ioc').reduce((a, f) => a + Number(f.qty), 0);
      const diff = end == null ? null : Math.abs(Number(end) - Number(c.limit));
      t.title = filled > 0 ? 'Stopped: the rest did not fill' : 'Stopped: nothing filled';
      const done = `You ${w.bought} ${qty(filled)} of ${qty(c.qty)} ${B}. No order of yours rests on the simulated exchange.`;
      if (end == null) {   // the chase ended with no valid price: the feed was lost or the book was not valid
        t.sub = `${c.feed_ok ? 'The order book was not valid at the end' : `The ${timeout} timeout passed while the public Kraken price feed was lost`}. With no valid price, the tool sent no IOC. It cancelled the order. ` + done;
        break;
      }
      const at = diff === 0 ? `at the ${w.limitWord}` : `${p(diff)} ${w.buy ? 'below' : 'above'} the ${w.limitWord} of ${p(c.limit)}`;
      t.sub = (beyond(c)
        ? `The price ${w.buy ? 'rose above your cap' : 'fell below your floor'}. The ${w.other} is now ${p(end)}, which is ${p(diff)} ${w.above} the ${w.limitWord} of ${p(c.limit)}. `
        : `The ${w.other} is now ${p(end)}, ${at}, but the book had too little at or ${w.buy ? 'below the cap' : 'above the floor'} for the rest. `) +
        `The ${w.close ? 'reduce-only ' : ''}IOC filled ${iocGot ? qty(iocGot) + ' ' + B : 'nothing'}. ` + done + positionLine(c);
      break;
    }
    case 'belowmin':
      t.title = 'Stopped: the rest is below the Kraken minimum';
      t.sub = `The rest, ${qty(rest)} ${B}, is below the Kraken minimum for ${c.pair.symbol} (${qty(c.pair.ordermin)} ${B} or ${c.pair.costmin} ${c.pair.quote}). An IOC that small is not possible, so the rest counts as "not filled". You ${w.bought} ${qty(filled)} of ${qty(c.qty)} ${B}. No order of yours rests on the simulated exchange.`;
      break;
    case 'rejected': {
      const reconciling = c.phase === 'reconcile';
      const why = c.reject === 'rate_limit' ? 'Reason: "EOrder:Rate limit exceeded".'
        : c.reject === 'would_cross' ? `Reason: the ${w.other} ${w.buy ? 'fell' : 'rose'} to ${p(w.buy ? c.ask : c.bid)}, so a post-only order at that price would take liquidity.`
        : `Reason: ${esc(c.reject)}.`;
      t.title = 'Amend rejected: checking the order';
      t.sub = `The simulated exchange rejected the amend to ${p(c.reject_price)}. ${why} The tool does not retry. It reads the order state again first.`;
      const next = Math.max(0, Math.ceil((c.slow ? 15 : 5) - ((snap ? snap.now : 0) - Math.max(c.last_amend_at || 0, c.placed_at || 0))));
      t.steps = [['done', c.reject === 'would_cross' ? `Amend rejected: post-only order would cross the ${w.other}.` : 'Amend rejected.'],
        [reconciling ? 'now' : 'done', reconciling ? 'Read the order state again.' : `Read the order state again: open, ${qty(filled)} ${B} filled, price ${p(c.price)}.`],
        [reconciling ? 'todo' : 'now', `Wait for the next change of the ${w.best}.` + (reconciling ? '' : ` Next amend possible in ${next} s.`)]];
      break;
    }
    case 'disconnected': {
      const f = snap ? snap.feed : null;
      const lost = f ? Math.max(0, Math.round(snap.now - f.since)) : 0;
      t.title = 'Connection to Kraken lost: reconnecting';
      t.sub = c.phase === 'reconcile'
        ? 'The public Kraken price feed is back. The tool reads the simulated order again before it continues.'
        : `The tool lost the public Kraken price feed ${lost} s ago. The simulated exchange pauses until the feed is back.` + (f && f.attempt ? ` Attempt ${f.attempt}: next try in ${f.next_in} s.` : '');
      t.steps = [[c.phase === 'reconcile' ? 'done' : 'now', 'Reconnect to the Kraken feed' + (f && f.attempt ? ` (attempt ${f.attempt}).` : '.')],
        [c.phase === 'reconcile' ? 'now' : 'todo', 'Read the order and the fills again.'], ['todo', 'Continue the chase from that state.']];
      break;
    }
    case 'ratenear':
      t.title = 'Slowing down: rate limit near';
      t.sub = `The estimated rate counter for ${c.pair.symbol} is at ${Math.floor(c.rate)} of 60. The tool now waits 15 s between amends, not 5 s. This keeps room for a cancel. Your order can trail the ${w.best} for a short time. (Simulated: the tool counts the simulated orders as Kraken would count real ones.)`;
      t.steps = [['done', 'Estimated counter above 40: amends every 15 s.'],
        ['todo', 'Kraken rejects an amend with "EOrder:Rate limit exceeded": the tool also switches to 15 s amends.'], ['todo', 'Counter below 40 again: amends every 5 s.']];
      break;
    case 'ended': {
      const off = c.off_from == null ? null : c.off_from - c.started, on = c.ended_at - c.started;
      t.title = 'Ended: the tool stopped or restarted';
      t.sub = (off == null ? 'The tool stopped and started again.' : `The tool stopped at ${mmss(off)} and started again at ${mmss(on)}.`) + ' After a restart the tool does not continue a dry run. It did these steps:';
      t.steps = [['done', 'Ended the simulated order. No order was on Kraken.'], ['done', `Recorded the simulated fills: ${qty(filled)} of ${qty(c.qty)} ${B}.`], ['done', 'Did not continue the dry run.']];
      t.todo = [DRY_TODO, 'To test again, start a new dry run.'];
      if (w.m) t.note = 'A restart does not close a position. The simulated position stays in the simulated account.' + positionLine(c);
      break;
    }
    case 'stopped':
      t.title = 'Stopped by you';
      t.sub = `You stopped the chase. The tool cancelled the order and the simulated exchange confirmed the cancel. ${qty(filled)} ${B} filled before the stop.` +
        (w.m ? positionLine(c) : ' No order of yours rests on the simulated exchange.');
      break;
    case 'liquidated':
      t.title = 'Ended: the simulated exchange liquidated the position';
      t.sub = `The account margin level fell to ${c.pair.margin_stop}%, so the simulated exchange closed every position at the mark. ` +
        (w.close ? 'Your reduce-only order had nothing left to close. ' : '') + (filled > 0 ? `${qty(filled)} ${B} ${w.close ? 'closed' : 'of the open filled'} before. ` : '') +
        'The tool cancelled its order and ended the chase.';
      t.todo = ['The margin level is for the whole account: the simulated account shows each position on the form, under "Close a position".', DRY_TODO];
      break;
    case 'nopos':
      t.title = 'Ended: the position is closed';
      t.sub = `The ${w.dir} position on ${c.pair.symbol} closed while the chase ran. The reduce-only order had nothing left to close. The tool cancelled it and ended the chase. This chase closed ${qty(filled)} ${B}.`;
      break;
    case 'refused': {
      const last = c.events.filter(e => e.kind === 'bad').pop();
      t.title = 'Stopped: the order was rejected';
      t.sub = (last ? last.text + ' ' : '') + 'Nothing filled. No order of yours rests on the simulated exchange.';
      break;
    }
    case 'cancelfail':
      t.title = 'Stopped: the cancel did not go through';
      t.sub = `The simulated exchange rejected the cancel 3 times and still showed the order as open. The tool stopped the chase and sent no IOC. You ${w.bought} ${qty(filled)} of ${qty(c.qty)} ${B}.`;
      t.todo = [c.mode === 'live' ? LIVE_CANCEL_TODO : DRY_TODO];
      break;
    case 'pageoffline':
      t.title = 'This page lost the tool';
      t.sub = `This page cannot reach the tool at ${location.host}. The values below are from ${Math.round(ageOff)} s ago. If the tool still runs, the chase continues without this page. Restart the tool with "uv run order-chaser" if it stopped.`;
      break;
  }
  return t;
}

// The cost of a cancel and replace, in plain words (shown while it runs and in the result).
const REPLACE_COST = 'What a replace costs: each move is a cancel and a new order, not one amend. A cancel adds up to 8 to the rate counter and the new order adds 1, so moves can come less often. The new order also loses its place in the queue at its price, and for a moment no order rests.';
const replaceNote = c => c.replace ? ` The venue refuses amends of this order, so each move is a cancel and a new order (${c.legs.length - 1} so far).` : '';

// ---------- The price rail (from the approved prototype) ----------
function rail(c, bid, ask, stale) {
  const w = words(c);
  const sb = n(c.start_bid), sa = n(c.start_ask), lim = n(c.limit);
  const you = n(c.price), pend = n(c.pending);
  const yp = pend != null ? pend : you;
  const pts = [n(bid), n(ask), sb, sa, lim].filter(v => v != null);
  if (yp != null) pts.push(yp);
  let lo = Math.min(...pts), hi = Math.max(...pts);
  const tick = Number(c.pair.tick);
  const span = Math.max(hi - lo, 6 * tick), pad = span * 0.12 + tick / 2;
  lo -= pad; hi += pad;
  const x = v => ((v - lo) / (hi - lo) * 100).toFixed(2) + '%';
  const near = (a, b) => a != null && b != null && Math.abs((a - b) / (hi - lo)) < 0.24;
  const side = (cls) => {
    if (cls === 'bid' && near(n(bid), n(ask))) return 'l';
    if (cls === 'ask' && near(n(bid), n(ask))) return 'r';
    if (cls === 'you' && near(yp, lim)) return yp < lim ? 'l' : 'r';
    if (cls === 'cap' && near(yp, lim) && Math.abs(yp - lim) > tick / 2) return yp < lim ? 'r' : 'l';
    return '';
  };
  const mk = (cls, lab, v, below) => `<div class="mk ${cls} ${below ? 'below' : ''} ${side(cls)}" style="left:${x(v)}">${below ? '<div class="pin"></div>' : ''}<div class="val">${w.p(v)}</div><div class="lab">${lab}</div>${below ? '' : '<div class="pin"></div>'}</div>`;
  const z0 = Math.min(sb, lim), z1 = Math.max(sa, lim);
  let h = `<div class="rail${stale ? ' stale' : ''}"><div class="axis"></div><div class="zone" style="left:${x(z0)};width:calc(${x(z1)} - ${x(z0)})" title="Chase range"></div>`;
  if (bid != null) h += mk('bid', 'best bid', n(bid), true);
  if (ask != null) h += mk('ask', 'best ask', n(ask), true);
  let capLab = w.limitName;
  if (yp != null && Math.abs(yp - lim) > tick / 2) h += mk('you', pend != null ? (you != null ? 'your order (moving)' : 'your order (sending)') : 'your order', yp, false);
  else if (yp != null) capLab = `${w.limitWord} = your order`;
  h += mk('cap', capLab, lim, false);
  return h + '</div>';
}

// Did the price end beyond the cap (buy: above) or the floor (sell: below)?
const beyond = c => c.end_ask != null && (c.side === 'buy' ? Number(c.end_ask) > Number(c.limit) : Number(c.end_ask) < Number(c.limit));

function card(st, c, snap, ageOff) {
  const t = copy(st, c, snap, ageOff);
  const tone = TONE[st];
  const filled = Number(c.filled), total = Number(c.qty);
  const pct = filled / total * 100;
  const done = DONE.includes(st);
  const makerPct = filled ? Number(c.summary.maker_qty) / total * 100 : 0;
  const elapsed = (done ? c.ended_at : (snap ? snap.now : Date.now() / 1000)) - c.started;
  const steps = t.steps ? `<ol class="small" style="margin:10px 0 0;padding-left:20px">${t.steps.map(s => `<li style="padding:2px 0" class="${s[0] === 'now' ? 'tone-' + tone : s[0] === 'todo' ? 'muted' : ''}">${s[0] === 'done' ? '✓ ' : s[0] === 'now' ? '→ ' : ''}${s[1]}</li>`).join('')}</ol>` : '';
  const nf = ['notfilled', 'belowmin', 'ended', 'stopped', 'liquidated', 'nopos'].includes(st) ? `<i class="nf" style="width:${100 - pct}%"></i>` : '';
  // The chase's own prices, never the feed of another pair. Their age shows when they are not current.
  const now = done ? c.ended_at : st === 'pageoffline' ? lastNow + ageOff : snap ? snap.now : c.book_at;
  const age = c.book_at == null ? null : Math.max(0, Math.round(now - c.book_at));
  const stale = st === 'pageoffline' || st === 'disconnected' || (done && c.end_ask == null);
  const ageText = age == null ? 'no valid values' : done ? `values from ${age} s before the end` : `values from ${age} s ago`;
  let label = LABEL[st];
  if (c.margin && st === 'filled') label = !c.margin.close ? 'Position opened' : Number(c.margin_est.rest) > 0 ? 'Closed' : 'Position closed';
  if (c.margin && c.margin.close && st === 'partial') label = 'Partly closed';
  if (st === 'notfilled') label = (filled > 0 ? 'Rest not filled' : 'Nothing filled') + (!beyond(c) ? '' : c.side === 'buy' ? ' (above cap)' : ' (below floor)');
  if (stale) label += ' · ' + ageText;   // the age in the badge, not faded text
  const railNote = !done ? '' : stale ? `Prices: ${ageText}. There was no valid price at the end.`
    : c.book_at != null ? `Prices at the end of the chase, ${clock(c.book_at)}.` : '';
  return `<div class="status" id="statuscard" data-state="${st}" >
    <div class="state tone-${tone} ${done ? '' : 'pulse'}"><i></i>${label}</div>
    <div class="head">${t.title}</div>
    <div class="sub">${t.sub}</div>${steps}
    ${t.note ? `<p class="small" id="cardnote" style="margin:12px 0 0">${t.note}</p>` : ''}
    ${t.todo ? `<div class="note info small" style="margin-top:12px"><b>What to do now</b><ul style="margin:4px 0 0;padding-left:18px">${t.todo.map(x => `<li>${x}</li>`).join('')}</ul></div>` : ''}
    <div class="row" style="margin-top:18px;align-items:flex-end;flex-wrap:wrap">
      <div><div class="small muted">${!c.margin ? 'Filled' : c.margin.close ? 'Closed' : 'Opened'}</div><div class="big" style="white-space:nowrap">${qty(filled)} <span class="muted" style="font-size:.55em">of ${qty(total)} ${c.pair.base}</span></div></div>
      <span class="spacer"></span>
      ${done ? '' : `<div class="timer" style="white-space:nowrap"><span class="num">${mmss(elapsed)} / ${mmss(c.timeout)}</span><div class="bar"><i style="width:${Math.min(100, elapsed / c.timeout * 100)}%"></i></div></div>`}
    </div>
    <div class="bar" style="margin-top:8px"><i class="m" style="width:${makerPct}%"></i><i class="tk" style="width:${pct - makerPct}%"></i>${nf}</div>
    ${rail(c, c.bid, c.ask, stale)}${railNote ? `<div class="tiny muted" id="railnote">${railNote}</div>` : ''}
  </div>`;
}

function eventLog(c) {
  const kind = { you: 'ev-you', fill: 'ev-fill', warn: 'ev-warn', bad: 'ev-bad' };
  return `<ul class="log">${c.events.slice().reverse().map(e => `<li><span class="ts">${mmss(e.t)}</span><span class="${kind[e.kind] || ''}">${esc(e.text)}</span></li>`).join('')}</ul>`;
}

return { planLine, markNote, clock, positionOpen, closeLink, closeText, gauge, orderName, positionLine, MARGIN_FEES, REPLACE_COST, TOKEN, $, esc, beyond, pct, fillWord, againText, restBelowMin, DRY_TODO, LIVE_CANCEL_TODO, px, usd, qty, mmss, timeoutWords, post, stream, chrome, stateOf, card, eventLog, words, copy, LABEL, DONE };
})();
