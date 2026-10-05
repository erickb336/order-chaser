OC.chrome('new');
const $ = OC.$;
const params = new URLSearchParams(location.search);
// what: buy or sell (spot); long, short or close (margin). lev: the leverage of an open (the form starts at 2x).
let what = 'buy', lev = 2, posKey = null, sizeFor = null, snap = null, account = null, override = false, restOf = null, busy = false;
// Q1, Q2: the mode of this chase. The form opens in Dry run each time (a link from Setup can ask for Live).
let mode = params.get('mode') === 'live' ? 'live' : 'dry', check = null;
const MARGIN = ['long', 'short', 'close'];
const FEE_LO = 0.0001, FEE_HI = 0.0005;   // Kraken US margin fees: 0.01% to 0.05% of the cost (opening fee, and rollover per full 4 h)

// The server watches one pair for all tabs. A tab whose snapshots show another pair asks again for its own,
// at most every 3 s, while it has the focus and no chase runs. (Two tabs that both ask would swap the pair.)
let watchAt = 0;
function watch(pair) { watchAt = Date.now(); return OC.post('/api/watch', { pair }); }
function keepWatching() {
  if (snap && !busy && snap.watched !== $('pairsel').value && document.hasFocus() && Date.now() - watchAt > 3000)
    watch($('pairsel').value);
}
window.addEventListener('focus', keepWatching);
// The account and the positions (simulated in a dry run): read when the form opens and when you pick a margin choice.
async function loadAccount() {
  const r = await fetch('/api/account');
  if (r.ok) { account = await r.json(); draw(); }
}
// The tick accepts the cost shown for one limit, amount, side and pair. A change of any of them clears it.
const changed = () => { $('ovok').checked = false; draw(); };
// A limit belongs to one pair: a pair change clears it.
function setPair(pair) { if ($('pairsel').value !== pair) { $('pairsel').value = pair; watch(pair); $('ovp').value = ''; } }
$('pairsel').onchange = () => { watch($('pairsel').value); $('ovp').value = ''; if (what === 'close') posKey = null; changed(); };
$('what').onclick = e => {
  const b = e.target.closest('button');
  if (!b || b.dataset.w === what) return;
  what = b.dataset.w;
  if (MARGIN.includes(what)) loadAccount();
  if (what === 'close') sizeFor = null;     // a close starts at the whole position
  changed();
};
$('lev').onclick = e => { const b = e.target.closest('button'); if (b && !b.disabled) { lev = Number(b.dataset.v); draw(); } };
$('poslist').onclick = e => { const l = e.target.closest('[data-k]'); if (l && l.dataset.k !== posKey) { posKey = l.dataset.k; setPair(l.dataset.k.split('|')[0]); changed(); } };
// "Close the short first": the link switches the form to a close of that pair and direction.
$('oppose').onclick = e => {
  const a = e.target.closest('a[data-k]');
  if (!a) return;
  e.preventDefault(); what = 'close'; posKey = a.dataset.k || null; sizeFor = null; if (posKey) setPair(posKey.split('|')[0]); loadAccount(); changed();
  const row = [...$('poslist').querySelectorAll('[data-k]')].find(l => l.dataset.k === posKey);   // the link is gone now: the focus goes to its position
  if (row) row.querySelector('input').focus();
};
$('est').onclick = e => { if (e.target.id === 'whole') { wholeSize(); changed(); } };
$('ovbtn').onclick = () => { override = true; const s = snap; if (s && s.feed.ask) { const p = pairInfo(); $('ovp').value = (side() === 'buy' ? Number(s.feed.ask) : Number(s.feed.bid)).toFixed(p ? p.price_decimals : 2); } changed(); };
$('ovoff').onclick = () => { override = false; changed(); };
['amt', 'ovp'].forEach(id => $(id).oninput = changed);
$('ovok').onchange = draw;
$('to').onchange = draw;

function pairInfo() { return snap && snap.pairs[$('pairsel').value]; }
// The positions of the account, one row for each pair and direction.
const positions = () => account ? account.positions : [];
function position() {
  const all = positions();
  const p = all.find(x => x.pair + '|' + x.dir === posKey) || all.find(x => x.pair === $('pairsel').value) || all[0];
  if (p) { posKey = p.pair + '|' + p.dir; setPair(p.pair); }
  return p || null;
}
// "Close all": the whole position.
function wholeSize() { const p = position(); if (p) $('amt').value = OC.qty(p.qty); }
function side() {
  if (what === 'buy' || what === 'long') return 'buy';
  if (what === 'sell' || what === 'short') return 'sell';
  const p = position();
  return p && p.dir === 'short' ? 'buy' : 'sell';
}
const nf = (v, d) => Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const when = t => new Date(t * 1000).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const readAt = a => new Date(a.at * 1000).toLocaleTimeString('en-GB');
const level = v => v == null ? 'no position' : Math.round(v) + '%';

// Write HTML into el only when it changes: a focused control inside it keeps the focus.
function put(el, html) { if (el._h !== html) { el._h = html; el.innerHTML = html; } }
function text(el, t) { if (el.textContent !== t) el.textContent = t; }

// The close plan of a size, from the server (core.close_plan, the rule of the account): read again when the
// position, the size or the account read changes. null while it is being read.
let plan = null, planFor = null;
function closePlan(pos, size) {
  const key = [pos.pair, pos.dir, size, account.at].join('|');
  if (key !== planFor) {
    planFor = key;
    fetch(`/api/plan?pair=${encodeURIComponent(pos.pair)}&dir=${pos.dir}&qty=${encodeURIComponent(size)}`)
      .then(r => r.ok ? r.json() : null).then(p => { if (planFor === key) { plan = p && { ...p, key }; draw(); } });
  }
  return plan && plan.key === key ? plan : null;
}

// The close list: one row for each pair and direction. The rows are built once for a set of positions; each
// redraw changes only their text, so the radio keeps the focus and the selection while prices update.
function drawPositions(s) {
  const list = $('poslist'), all = positions();
  const keys = !account ? '…' : all.map(x => x.pair + '|' + x.dir).join(' ');
  if (list.dataset.keys !== keys) {
    list.dataset.keys = keys;
    list.innerHTML = !account ? '<div class="tiny muted">Reading your positions (simulated account)…</div>'
      : !all.length ? '<div class="note info small">You have no open margin positions in the simulated account. To open one, pick Open long or Open short.</div>'
      : all.map((x, i) => `<label data-k="${OC.esc(x.pair + '|' + x.dir)}"><input type="radio" name="pos" aria-labelledby="pos${i}t" aria-describedby="pos${i}d pos${i}r pos${i}p"><div><b id="pos${i}t"></b> <span class="muted small" id="pos${i}d"></span><div class="tiny muted" id="pos${i}r"></div></div><div id="pos${i}p"></div></label>`).join('')
        + '<div class="tiny muted" id="posfoot"></div>';
  }
  all.forEach((x, i) => {
    const on = x.pair + '|' + x.dir === posKey, xb = x.pair.split('/')[0], pd = (s && s.pairs[x.pair] || {}).price_decimals || 2;
    // The watched pair: P/L at the mark of each new price (the mid of the best bid and ask, as the account marks it), with no new
    // account read. Another pair: P/L at its last mark in the account read.
    const live = s && x.pair === s.watched && s.feed.ok && s.feed.fresh && s.feed.bid && s.feed.ask;
    const upl = live ? ((Number(s.feed.bid) + Number(s.feed.ask)) / 2 - Number(x.entry)) * Number(x.qty) * (x.dir === 'long' ? 1 : -1) : Number(x.upl);
    const row = list.children[i], radio = row.querySelector('input');
    row.classList.toggle('on', on);
    if (radio.checked !== on) radio.checked = on;
    // Each open of a pair and direction is its own position with its own leverage (as Kraken).
    put($(`pos${i}t`), `${OC.esc(x.pair)} <span class="badge ${x.dir === 'long' ? 'long' : 'short'}">${x.dir === 'long' ? 'Long' : 'Short'}</span> ${OC.qty(x.qty)} ${OC.esc(xb)}`);
    text($(`pos${i}d`), x.count > 1 ? `· ${x.count} positions · average ${x.leverage}x · average entry ${OC.px(x.entry, pd)} · oldest opened ${when(x.opened)}`
      : `· ${x.leverage}x · opened ${when(x.opened)} at ${OC.px(x.entry, pd)}`);
    text($(`pos${i}r`), `Rollover so far: ${OC.usd(x.rollover)} USD (estimate)`);
    const pl = $(`pos${i}p`);
    pl.className = x.upl == null ? 'r small muted' : `r small num ${upl >= 0 ? 'tone-fill' : 'tone-bad'}`;
    put(pl, x.upl == null ? `no price yet<div class="tiny" style="font-family:var(--sans)">profit or loss shows when ${OC.esc(x.pair)} prices arrive</div>`
      : `${upl >= 0 ? '+' : '−'}${OC.usd(Math.abs(upl))} USD<div class="tiny muted" style="font-family:var(--sans)">${live ? 'profit or loss now, at the mark' : `at the last price seen, ${OC.clock(x.mark_at)}`} (estimate)</div>`);
  });
  if (account && all.length) text($('posfoot'), `Read from the simulated account at ${readAt(account)}. One row for each pair and direction. A close takes the oldest position first.`);
}

function draw() {
  const s = snap, pair = $('pairsel').value, p = pairInfo();
  const [B, Q] = pair.split('/');
  const margin = MARGIN.includes(what), close = what === 'close', open = margin && !close;
  const pos = close ? position() : null;
  if (pos && sizeFor !== posKey) { $('amt').value = OC.qty(pos.qty); sizeFor = posKey; }   // a close starts at the whole position
  // The last liquidation of the simulated account: which positions, at what mark, when. A liquidation always reads the account.
  const liq = account && account.liquidation;
  $('liqnote').classList.toggle('hidden', !liq);
  if (liq) {
    const when = new Date(liq.at * 1000).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const row = x => {
      const [b, q] = x.pair.split('/'), pd = snap && snap.pairs[x.pair] ? snap.pairs[x.pair].price_decimals : 2, pl = Number(x.pl);
      return `<li>${OC.esc(x.pair)} ${OC.esc(x.dir)}, ${OC.qty(x.qty)} ${OC.esc(b)} at ${OC.esc(x.leverage)}x: closed at the mark ${OC.px(x.mark, pd)} (entry ${OC.px(x.entry, pd)}), ${pl >= 0 ? '+' : '−'}${OC.usd(Math.abs(pl))} ${OC.esc(q)}</li>`;
    };
    put($('liqnote'), `<b>The simulated exchange liquidated ${liq.positions.length > 1 ? `${liq.positions.length} positions` : 'a position'} on ${when}.</b> The account margin level fell to ${Math.round(Number(liq.level))}%, at or below the liquidation mark. Each position closed at the mark of its own pair (estimate, after rollover):` +
      `<ul id="liqlist" style="margin:4px 0 0;padding-left:18px">${liq.positions.map(row).join('')}</ul><span class="small">A dry run has no position on Kraken.</span>`);
  }
  const buy = side() === 'buy';
  const d = p ? p.price_decimals : 2;
  const P = v => OC.px(v, d);
  document.querySelectorAll('#what button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.w === what)));
  $('unit').textContent = B; $('ovunit').textContent = Q;
  $('priceshead').textContent = 'Prices now: ' + pair;
  $('intro').textContent = buy ? 'The tool places a post-only limit at the best bid and moves it up as the bid rises. It never goes above the cap.'
    : 'The tool places a post-only limit at the best ask and moves it down as the ask falls. It never goes below the floor.';
  $('whathelp').textContent = what === 'sell' ? 'Sell: rests at the best ask and moves down. Floor = the bid at the start.'
    : what === 'long' ? 'Open long: a post-only buy with leverage. It rests at the best bid and moves up. Each fill opens part of the position at once.'
    : what === 'short' ? 'Open short: a post-only sell with leverage. It rests at the best ask and moves down. Each fill opens part of the position at once.'
    : close && pos ? `Close ${pos.dir}: a reduce-only ${buy ? 'buy' : 'sell'} on ${pos.pair}.` : close ? 'Reduce-only. Closes all or part of an open position.' : '';
  const ready = !!(s && s.live && s.live.ready);
  if (s && mode === 'live' && (!ready || margin)) mode = 'dry';
  const live = mode === 'live';
  document.querySelectorAll('#mode button').forEach(b => b.setAttribute('aria-pressed', String((b.dataset.m === 'live') === live)));
  $('livebtn').disabled = !ready || margin;
  $('livebtn').style.textDecoration = !ready || margin ? 'line-through' : '';
  OC.setMode(live);
  put($('modehelp'), !ready ? 'Live is off: ' + (s && s.live ? s.live.why.map(OC.esc).join(' ') : 'setup is not complete.') + ' <a href="/setup">Finish setup</a> to turn it on.'
    : margin ? '<b style="color:var(--dry)">Dry run sends no orders.</b> It simulates the fills and the position on live public prices, on a simulated account. Margin live comes only after your go-ahead.'
    : live ? '<b style="color:var(--live)">Live stages the order and places nothing.</b> The signed helper (coming) places it on Kraken after a Touch ID. Spot only.'
    : '<b style="color:var(--dry)">Dry run sends no orders.</b> It runs the chase on live public prices and simulates the fills. The form opens in Dry run each time.');
  $('capword').textContent = buy ? 'Cap' : 'Floor';
  $('ovbtn').textContent = buy ? 'Set a higher limit' : 'Set a lower limit';
  $('ovoff').textContent = buy ? 'Use the start ask' : 'Use the start bid';
  $('tohelp').textContent = close && !pos ? 'The timeout counts when a close runs. You have no position to close.' : `After this time, the tool sends one ${close ? 'reduce-only ' : ''}IOC limit at the ${buy ? 'cap' : 'floor'} for the rest. If the price is ${buy ? 'above the cap' : 'below the floor'}, the rest does not fill.`;
  const ioc = '<li>After the timeout: cancels, confirms, re-reads the fill, then sends one IOC at the ' + (buy ? 'cap' : 'floor') + ' for the rest.</li><li>Before the IOC: checks the rest against both Kraken minimums. A rest below a minimum stops as "not filled".</li>';
  const replaceLine = `If Kraken refuses an amend, it cancels and places a new order instead.`;
  $('whatdoes').innerHTML = close && !pos ? '<li>Nothing to close.</li>' : !margin ? (buy
    ? '<li>Records the ask now as the cap.</li><li>Places a post-only buy at the best bid.</li><li>Moves it up with the best bid, at most every 5 s, never above the cap.</li>' + ioc
    : '<li>Records the bid now as the floor.</li><li>Places a post-only sell at the best ask.</li><li>Moves it down with the best ask, at most every 5 s, never below the floor.</li>' + ioc)
    : open ? `<li>Records the ${buy ? 'ask' : 'bid'} now as the ${buy ? 'cap' : 'floor'}.</li><li>Places a post-only ${buy ? 'buy' : 'sell'} at the best ${buy ? 'bid' : 'ask'}, with leverage ${lev}x.</li><li>Moves it ${buy ? 'up' : 'down'} with the best ${buy ? 'bid' : 'ask'}, at most every 5 s. ${replaceLine}</li><li>After the timeout: one IOC at the ${buy ? 'cap' : 'floor'} for the rest, with the same leverage.</li><li>When the chase ends, the position stays open until you close it.</li>`
    : `<li>Records the ${buy ? 'ask' : 'bid'} now as the ${buy ? 'cap' : 'floor'}.</li><li>Places a post-only ${buy ? 'buy' : 'sell'}, reduce-only, at the best ${buy ? 'bid' : 'ask'}.</li><li>Moves it with the price, at most every 5 s. ${replaceLine}</li><li>After the timeout: one reduce-only IOC at the ${buy ? 'cap' : 'floor'} for the rest.</li><li>What does not close stays open as a position.</li>`;

  let startOk = true, note = '', amtOk = false;
  const block = n => { startOk = false; note = n; };
  // prices
  const fresh = s && s.watched === pair && s.feed.fresh && s.feed.bid;
  const bid = fresh ? Number(s.feed.bid) : null, ask = fresh ? Number(s.feed.ask) : null;
  $('bid').textContent = bid ? P(bid) : '…'; $('ask').textContent = ask ? P(ask) : '…';
  if (fresh) {
    const spread = ask - bid;
    $('pnote').textContent = `Spread ${P(spread)} (${(spread / ask * 100).toFixed(4)}%). Updated ${Math.round(s.feed.age)} s ago from the public Kraken feed.`;
  } else if (s && !s.feed.up && s.feed.attempt) {
    $('pnote').innerHTML = `<b class="tone-bad">No prices: the feed is lost.</b> The tool retries (attempt ${s.feed.attempt}, next try in ${s.feed.next_in} s). Check your internet connection.`;
    block('Start needs live prices.');
  } else { $('pnote').textContent = 'Connecting to the public Kraken feed…'; block('Start is possible when prices arrive.'); }
  // pair status: a close also works when the pair is reduce_only
  if (s && s.pairs_error && !p) { $('pstat').innerHTML = `<span class="tone-bad">${OC.esc(s.pairs_error)}</span>`; block('Start needs the Kraken pair list.'); }
  else if (p) $('pstat').innerHTML = `Pair status: <span class="badge ${p.status === 'online' ? 'ok' : 'bad'} mono">${OC.esc(p.status)}</span>`;
  else $('pstat').textContent = 'Pair status: reading…';
  const off = p && p.status !== 'online' && !(close && p.status === 'reduce_only');
  $('pairoff').classList.toggle('hidden', !off);
  if (off && open && p.status === 'reduce_only') {
    $('pairoff').innerHTML = `<b>Kraken accepts only reduce-only orders on ${pair} now.</b> Pair status: <span class="mono">reduce_only</span>. You cannot open a position. You can close one: pick Close a position.`;
    block('Open comes back when the pair is online.');
  } else if (off) {
    $('pairoff').innerHTML = `<b>Kraken accepts no new chase for ${pair} now.</b> Pair status: <span class="mono">${OC.esc(p.status)}</span>. A chase needs the status <span class="mono">online</span>. The tool reads the status again every 30 s, and Start comes back when it is online.
      <table class="t small" style="margin-top:8px"><tr><th>Status</th><th>What Kraken allows</th><th>Chase</th></tr>
      <tr><td class="mono">online</td><td>All orders</td><td class="tone-fill">possible</td></tr>
      <tr><td class="mono">cancel_only</td><td>Cancels only</td><td class="tone-bad">blocked</td></tr>
      <tr><td class="mono">post_only</td><td>Post-only orders only: the IOC fallback cannot go out</td><td class="tone-bad">blocked</td></tr>
      <tr><td class="mono">limit_only</td><td>Limit orders only. The pair is not in normal trading.</td><td class="tone-bad">blocked</td></tr>
      <tr><td class="mono">reduce_only</td><td>Orders that reduce a position only</td><td class="tone-bad">blocked, except a margin close</td></tr>
      <tr><td class="mono">maintenance</td><td>No orders</td><td class="tone-bad">blocked</td></tr></table>`;
    block('Start comes back when the pair is online.');
  }
  // leverage (open), positions (close)
  $('levfield').classList.toggle('hidden', !open);
  $('posfield').classList.toggle('hidden', !close);
  let mnote = null;
  if (open && p) {
    const allowed = (buy ? p.leverage_buy : p.leverage_sell).filter(x => x >= 2 && x <= 5);
    if (allowed.length && !allowed.includes(lev)) lev = allowed.filter(x => x <= lev).pop() || allowed[0];
    document.querySelectorAll('#lev button').forEach(b => {
      const v = Number(b.dataset.v), ok = allowed.includes(v);
      b.disabled = !ok; b.setAttribute('aria-pressed', String(ok && v === lev));
      b.title = ok ? '' : `${pair} allows ${allowed.length ? allowed.map(x => x + 'x').join(', ') : 'no leverage on this side'}`;
    });
    $('levhelp').innerHTML = !allowed.length ? `<span class="tone-bad">Kraken allows no ${what} on ${pair}.</span>`
      : allowed.length < 4 ? `${pair} allows ${allowed.map(x => x + 'x').join(' and ')}. Kraken sets this for each pair.` : 'The form starts at 2x. Kraken allows up to 5x here; the tool never goes above 5x.';
    if (!allowed.length) {
      mnote = ['bad', `<b>Kraken allows no ${what} on ${pair}.</b> The pair has no ${buy ? 'buy' : 'sell'} leverage (AssetPairs <span class="mono">leverage_${buy ? 'buy' : 'sell'}</span> is empty). ${buy ? 'Open short' : 'Open long'} and Close may work.`];
      block(`Pick another pair, or ${buy ? 'Open short' : 'Open long'}.`);
    }
  }
  // An open against an open position of the other direction: blocked at the top of the form, with no figures.
  const other = open ? positions().find(x => x.pair === pair && x.dir === (what === 'long' ? 'short' : 'long')) : null;
  // No free margin for new orders: no amount can open, so the way on is a close.
  const noFree = open && !other && account && Number(account.free_orders) <= 0;
  $('oppose').classList.toggle('hidden', !other && !noFree);
  if (noFree) {
    put($('oppose'), `<b>No free margin for a new open. Close a position first.</b> Free margin for new orders: ${OC.usd(account.free_orders)} ${Q} (simulated account). <a href="/new?what=close" data-k="" id="toclose">Close a position</a>`);
    block('Close a position first.');
  }
  if (other) {
    put($('oppose'), `<b>Close the ${other.dir} first.</b> You have an open ${other.dir} position on ${OC.esc(pair)}: ${OC.qty(other.qty)} ${B}, ${other.leverage}x (simulated account). The tool does not open a position against it. <a href="/new?what=close" data-k="${OC.esc(pair + '|' + other.dir)}" id="toclose">Close the ${other.dir} on ${OC.esc(pair)}</a>`);
    block(`Close the ${other.dir} first.`);
  }
  if (close) {
    drawPositions(s);
    if (!pos) block(account ? 'Nothing to close.' : 'Start is possible when the positions arrive.');
  }
  $('amtfield').classList.toggle('hidden', close && !pos);
  $('capfield').classList.toggle('hidden', close && !pos);
  // amount and minimums
  $('amtlabel').textContent = close ? 'Size to close' : 'Amount';
  const raw = $('amt').value.trim();
  const q = Number(raw);
  const ref = buy ? ask : bid;
  let est = ref && q > 0 ? (open ? `≈ ${OC.usd(q * ref)} ${Q} position cost` : `≈ ${OC.usd(q * ref)} ${Q} at the ${buy ? 'ask' : 'bid'} now`) : `≈ … ${Q}`;
  const closeAll = close && pos ? `<button class="btn sm" id="whole" type="button">Close all: ${OC.qty(pos.qty)} ${B}</button>` : '';
  if (close && pos) est = q === Number(pos.qty) ? '<span class="badge ok">Whole position</span>' : closeAll;
  if (other) est = '';
  let amtErr = '';
  $('amthelp').innerHTML = !margin ? 'Dry run: no balance check. A live chase reads your balance from Kraken.'
    : open ? (account ? `Free margin: ${OC.usd(account.free)} ${Q} (simulated account, read at ${readAt(account)}).` : 'Reading the simulated account…')
    : pos ? `<b>Reduce-only.</b> The order can only make this ${pos.dir} smaller. It can never ${buy ? 'open a long' : 'open a short'} or make the position larger. The size starts at the whole position. You can make it smaller.` : '';
  if (p) {
    const word = close ? 'Size' : restOf && Number(restOf.filled) > 0 ? 'Rest' : 'Amount';
    const aOk = q >= Number(p.ordermin), cOk = ref ? q * ref >= Number(p.costmin) : true;
    $('mins').innerHTML = `<span class="tone-${aOk ? 'fill' : 'bad'}">${aOk ? '✓' : '✗'} ${word} at least ${OC.qty(p.ordermin)} ${B}</span> <span class="tone-${cOk ? 'fill' : 'bad'}">${cOk ? '✓' : '✗'} Value at least ${OC.usd(p.costmin)} ${Q}</span> <span class="tiny muted">Kraken minimums for ${pair} (ordermin, costmin)</span>`;
    const decOk = !raw.includes('.') || raw.split('.')[1].length <= p.qty_decimals;
    const errAmt = t => { amtErr = t; block(close ? 'Fix the size to start.' : 'Fix the amount to start.'); };
    if (!(q > 0) || !/^\d*\.?\d+$|^\d+\.$/.test(raw) || !decOk) errAmt(`Enter ${close ? 'a size' : 'an amount'} in ${B}, above 0, with at most ${p.qty_decimals} decimals.`);
    else if (!aOk || !cOk) errAmt(`The ${close ? 'size' : 'amount'} is below ${!aOk && !cOk ? 'both Kraken minimums' : 'a Kraken minimum'} for ${pair}. Enter ${OC.qty(Math.max(Number(p.ordermin), ref ? Number(p.costmin) / ref : 0))} ${B} or more.`);
    else if (close && pos && q > Number(pos.qty)) {
      errAmt(`A close cannot be larger than the position, ${OC.qty(pos.qty)} ${B}. Reduce-only orders never grow or flip a position. Enter ${OC.qty(pos.qty)} or less.`);
      est = closeAll;
    } else amtOk = true;
    if (amtOk && close && pos) {   // a part close that leaves a rest below the minimum: a warning, not a block
      const left = Number(pos.qty) - q;
      if (left > 0 && (left < Number(p.ordermin) || (ref && left * ref < Number(p.costmin)))) {
        $('mins').innerHTML += ` <span class="tone-bad">✗ The rest, ${OC.qty(left)} ${B}, is below the minimum</span>`;
        mnote = ['warn', `<b>This close leaves ${OC.qty(left)} ${B} open.</b> That is below the Kraken minimum of ${OC.qty(p.ordermin)} ${B} or ${OC.usd(p.costmin)} ${Q}, so a later order cannot close it. It can still be liquidated, and it pays rollover. To avoid this, press "Close all". You can also start as it is.`];
      }
    }
  }
  // The price stream redraws the form each second: a control is replaced only when it changes, so it keeps the focus.
  put($('est'), est);
  // The close plan of this size (the server's FIFO rule): which positions it takes and what stays.
  const plan = close && pos && amtOk ? closePlan(pos, raw.replace(/\.$/, '')) : null;
  $('plan').classList.toggle('hidden', !(close && pos && amtOk));
  put($('plan'), !plan ? 'Reading which positions this close takes…'
    : `<b>Closes, oldest first:</b> ${OC.esc(plan.takes)}. <b>Stays open:</b> ${plan.stays ? OC.esc(plan.stays) : `nothing, the ${pos.dir} is closed`}.`);
  // cap or floor, and the override
  $('ov').classList.toggle('hidden', !override);
  $('ovbtn').classList.toggle('hidden', override);
  // The same rule as the server: a plain number above 0, a multiple of the tick size.
  const limRaw = $('ovp').value.trim(), tick = p ? Number(p.tick) : 0;
  const lim = !override ? ref : /^\d+(\.\d+)?$/.test(limRaw) && Number(limRaw) > 0 ? Number(limRaw) : null;
  const onTick = lim != null && (!tick || Math.abs(lim / tick - Math.round(lim / tick)) < 1e-6);
  const ok = !override || (lim != null && onTick && ref && (buy ? lim >= ref : lim <= ref));
  let tickShown = false;
  if (!override) {
    $('capline').textContent = buy ? 'The ask at the start' : 'The bid at the start';
    $('capsub').textContent = !ref ? 'Waiting for prices.' : close ? `Now ${P(ref)}. The close never ${buy ? 'buys above' : 'sells below'} it.`
      : buy ? `Now ${P(ref)}. The tool records it when you press Start.` : `Now ${P(ref)}. The tool never sells below it.`;
  } else {
    $('capline').textContent = 'Your limit: ' + (lim != null ? P(lim) : '…');
    $('capsub').textContent = !ref || lim == null ? '' : `${lim > ref ? 'Higher than' : lim < ref ? 'Lower than' : 'The same as'} the ${buy ? 'ask' : 'bid'} now (${P(ref)}).`;
    $('ovwarn').className = 'note small ' + (ok && amtOk && lim === ref ? 'plain' : 'warn');
    if (!ok) {
      $('ovwarn').innerHTML = lim == null ? `<b>Enter the limit as a plain number above 0, such as ${ref ? ref.toFixed(d) : '62480.0'}.</b>`
        : !onTick ? `<b>Enter a limit that is a multiple of ${p.tick} ${Q}.</b>`
        : `<b>Enter a limit ${buy ? 'at or above the ask' : 'at or below the bid'} now${ref ? ' (' + P(ref) + ')' : ''}.</b>`;
      block('Fix the limit to start.');
    } else if (amtOk && lim === ref) {
      // A limit at the price now (the server's rule too): no extra cost and no loss, so no tick box.
      $('ovwarn').innerHTML = `<b>Your limit is the ${buy ? 'ask' : 'bid'} now.</b> ${buy ? 'Worst extra' : 'Worst loss'} against a market ${buy ? 'order' : 'sell'} now: <b class="num">0.00 ${Q}</b>.`;
    } else if (amtOk) {
      tickShown = true;
      const worst = buy ? q * lim * 1.008 : q * lim * 0.992, market = buy ? q * ref * 1.008 : q * ref * 0.992;
      const extra = Math.abs(worst - market), diff = Math.abs(lim - ref);
      $('ovwarn').innerHTML = buy
        ? `<b>Warning: this can cost more than a market order now.</b> Your limit is ${P(diff)} above the ask (${(diff / ref * 100).toFixed(2)}%). In the worst case the tool buys at ${P(lim)} plus the taker fee: <b class="num">${OC.usd(worst)} ${Q}</b>. A market order now costs about <b class="num">${OC.usd(market)} ${Q}</b>. Worst extra: <b class="num">${OC.usd(extra)} ${Q}</b>.`
        : `<b>Warning: this can bring less than a market sell now.</b> Your limit is ${P(diff)} below the bid (${(diff / ref * 100).toFixed(2)}%). In the worst case the tool sells at ${P(lim)} minus the taker fee: <b class="num">${OC.usd(worst)} ${Q}</b> received. A market sell now brings about <b class="num">${OC.usd(market)} ${Q}</b>. Worst loss: <b class="num">${OC.usd(extra)} ${Q}</b>.`;
      $('ovoktext').textContent = buy ? `I accept that this chase can cost up to ${OC.usd(extra)} ${Q} more than a market order now.` : `I accept that this chase can bring up to ${OC.usd(extra)} ${Q} less than a market sell now.`;
      if (!$('ovok').checked && startOk) block(buy ? 'Accept the extra cost to start.' : 'Accept the possible loss to start.');
    } else $('ovwarn').innerHTML = '<b>Enter a valid amount to see the extra cost.</b>';
  }
  // The tick shows only next to a cost it accepts.
  $('ovokrow').classList.toggle('hidden', !tickShown);
  if (!tickShown) $('ovok').checked = false;
  // margin: what an open uses and costs, and the account margin level now and after
  const L = lim || ref;
  const mg = margin && p && account && (open || pos) && !other;
  $('mgbox').classList.toggle('hidden', !mg);
  if (mg) {
    const lvlNow = account.level == null ? null : Number(account.level), eq = Number(account.equity), used = Number(account.used);
    const head = `<div class="small" style="margin-bottom:6px"><span class="badge plain">simulated account</span> Equity ${OC.usd(eq)} ${Q} · free margin ${OC.usd(account.free)} ${Q} · read at ${readAt(account)}</div>`;
    const marks = OC.markNote(account, s && s.watched);
    const gaugeHead = `<div class="small" style="margin-top:12px"><b>Account margin level</b> <span class="muted">(equity ÷ used margin). Kraken calls margin at ${p.margin_call}% and liquidates at ${p.margin_stop}% (AssetPairs).</span></div>` +
      (marks ? `<p class="tiny muted" id="marknote" style="margin:2px 0 0">${marks}</p>` : '');
    if (open && amtOk && L) {
      const cost = q * L, collat = cost / lev, freeOrders = Number(account.free_orders), lack = collat > freeOrders;
      const after = (eq - cost * FEE_HI) / (used + collat) * 100;
      $('mgbox').innerHTML = `<label class="f">Margin: what this open uses and costs <span class="badge plain">estimate</span></label>${head}
        <table class="t small" style="border:1px solid var(--line);border-radius:8px">
          <tr><td>Position cost</td><td class="r num">${OC.usd(cost)} ${Q}</td><td class="tiny muted">${OC.qty(q)} × ${P(L)} (the ${buy ? 'cap' : 'floor'})</td></tr>
          <tr><td>Collateral it uses</td><td class="r num ${lack ? 'tone-bad' : ''}"><b>${OC.usd(collat)} ${Q}</b></td><td class="tiny muted">position cost ÷ ${lev}. Free margin for new orders: ${OC.usd(freeOrders)} ${Q}</td></tr>
          <tr><td>Opening fee</td><td class="r num">${OC.usd(cost * FEE_LO)} to ${OC.usd(cost * FEE_HI)} ${Q}</td><td class="tiny muted">0.01% to 0.05% of the cost, charged at the open</td></tr>
          <tr><td>Rollover, every 4 h</td><td class="r num">${OC.usd(cost * FEE_LO)} to ${OC.usd(cost * FEE_HI)} ${Q}</td><td class="tiny muted">after each full 4 h open; for 1 day (6 × 4 h): up to ${OC.usd(cost * FEE_HI * 6)} ${Q}</td></tr>
        </table>
        <p class="tiny muted" style="margin:6px 0 0">${OC.MARGIN_FEES}</p>
        ${gaugeHead}${OC.gauge(lvlNow, after, p.margin_call, p.margin_stop)}
        <p class="small" style="margin:14px 0 0" id="lvlline">${after <= p.margin_stop ? `<b class="tone-bad">After this open: ${level(after)}, at or below the ${p.margin_stop}% liquidation mark.</b>` : `Now ${level(lvlNow)}. After this open: <b>${level(after)}</b>.`} The tool does not stop an open for its margin level. It shows the level so that you decide.</p>`;
      if (lack && !noFree) {
        amtErr = `This open needs ${OC.usd(collat)} ${Q} of collateral. Your free margin for new orders is ${OC.usd(freeOrders)} ${Q}. Kraken refuses an open without enough margin. Make the amount smaller.`;
        block('Fix the amount to start.');
      }
    } else if (open) {
      $('mgbox').innerHTML = `<label class="f">Margin: what this open uses and costs</label>${head}<p class="small muted" style="margin:0">Shown when prices arrive and the amount is valid.</p>`;
    } else if (plan) {
      // From the close plan: the close releases the collateral of each position it takes, at that position's leverage.
      const now = plan.level_now == null ? null : Number(plan.level_now), after = plan.level_after == null ? null : Number(plan.level_after);
      $('mgbox').innerHTML = head + gaugeHead + OC.gauge(now, after, p.margin_call, p.margin_stop, 'after the close') +
        `<p class="small" style="margin:14px 0 0" id="lvlline">Now ${level(now)}. After the close: <b>${after == null ? 'no open position' : level(after)}</b> (estimate: the close fills at the mark as maker, and releases the collateral of each position it takes). A close has no opening fee. Rollover stops for the part that closes.</p>`;
    } else {
      $('mgbox').innerHTML = head + gaugeHead + `<p class="small muted" style="margin:0" id="lvlline">The level after the close shows when the size is valid.</p>`;
    }
  }
  // The amount error: on the field (aria-invalid, aria-describedby) and in a live region, written only when it changes.
  $('amt').classList.toggle('err', !!amtErr);
  $('amt').setAttribute('aria-invalid', String(!!amtErr));
  if ($('amterr').textContent !== amtErr) $('amterr').textContent = amtErr;
  // worst case
  if (close && !pos) $('worst').textContent = 'Nothing to close.';
  else if (other) $('worst').textContent = `Close the ${other.dir} first.`;
  else if (ref && amtOk && ok) {
    if (open) {
      const cost = q * L, tf = cost * 0.008, of = cost * FEE_HI;
      $('worst').innerHTML = (buy
        ? `The open never costs more than <b class="num">${OC.usd(cost + tf + of)} ${Q}</b> in price and fees: ${OC.qty(q)} × ${P(L)} (cap) + ${OC.usd(tf)} taker fee (0.80%) + ${OC.usd(of)} opening fee (0.05%, the maximum; an estimate).`
        : `The short sells at no less than the floor, ${P(L)}, and pays at most <b class="num">${OC.usd(tf + of)} ${Q}</b> in fees at the open: ${OC.usd(tf)} taker (0.80%) + ${OC.usd(of)} opening fee (0.05%, the maximum; an estimate).`) +
        `<br><span class="tiny muted">Not included: rollover, up to ${OC.usd(of)} ${Q} per 4 h. A ${buy ? 'position can also lose value as the price moves' : 'short loses value when the price rises'}. That loss has no limit before liquidation.</span>`;
    } else if (close) {
      const fee = q * L * 0.008;
      $('worst').innerHTML = buy
        ? `The close never pays more than <b class="num">${OC.usd(q * L + fee)} ${Q}</b>: ${OC.qty(q)} × ${P(L)} (cap) + ${OC.usd(fee)} taker fee (0.80%). If the price passes the cap, the rest of the position stays open.`
        : `The close never brings less than <b class="num">${OC.usd(q * L - fee)} ${Q}</b>: ${OC.qty(q)} × ${P(L)} (floor) − ${OC.usd(fee)} taker fee (0.80%). If the price passes the floor, the rest of the position stays open.`;
    } else {
      $('worst').innerHTML = buy
        ? (override ? `Never more than <b class="num">${OC.usd(q * L * 1.008)} ${Q}</b> (${OC.qty(q)} × ${P(L)} + 0.80% taker fee).` : `Never more than a market order at the start: about <b class="num">${OC.usd(q * ref * 1.008)} ${Q}</b> (${OC.qty(q)} × ${P(ref)} + 0.80% taker fee).`) +
          ` Best case: about <b class="num">${OC.usd(q * bid * 1.004)} ${Q}</b> (all maker at ${P(bid)} + 0.40%). The fees use the highest rates, so the worst case is a true maximum.`
        : (override ? `Never less than <b class="num">${OC.usd(q * L * 0.992)} ${Q}</b> received (${OC.qty(q)} × ${P(L)} − 0.80% taker fee).` : `Never less than a market sell at the start: about <b class="num">${OC.usd(q * ref * 0.992)} ${Q}</b> received (${OC.qty(q)} × ${P(ref)} − 0.80% taker fee).`) +
          ' The fees use the highest rates, so this is a true minimum.';
    }
  } else $('worst').textContent = override && !ok ? 'Shown when the limit is valid.' : 'Shown when prices arrive and the amount is valid.';
  $('mnote').classList.toggle('hidden', !mnote);
  if (mnote) { $('mnote').className = `note ${mnote[0]} small`; $('mnote').innerHTML = mnote[1]; }
  // a chase runs
  $('busy').classList.toggle('hidden', !busy);
  if (busy) {
    const c = s.chase;
    $('busy').innerHTML = `<b>A chase runs now:</b> ${OC.esc(OC.orderName(c))} on ${OC.esc(c.pair.symbol)}, ${OC.pct(c.filled, c.qty)}% filled. You can start a new chase when it ends. <a href="/chase">Open it</a>`;
    startOk = false; note = '';
  }
  $('pairsel').disabled = busy;
  $('start').disabled = !startOk;
  $('start').textContent = live ? 'Stage live order…' : !margin ? 'Start dry run' : close ? 'Start dry run: close' : `Start dry run: open ${what}`;
  $('start').className = 'btn ' + (live ? 'livebtn' : 'drybtn');
  if (!$('startnote').dataset.err && $('startnote').textContent !== note) $('startnote').textContent = note;
}

function body() {
  const b = { pair: $('pairsel').value, qty: $('amt').value.trim(), timeout: Number($('to').value), mode };
  b.what = what === 'close' ? 'close-' + position().dir : what;
  if (what === 'long' || what === 'short') b.leverage = lev;
  if (override) { b.limit = $('ovp').value.trim(); b.accept_extra = $('ovok').checked; }
  return b;
}
function refused(errors) {
  $('startnote').dataset.err = '1';
  $('startnote').innerHTML = `<span class="tone-bad">${(errors || ['Refused.']).map(OC.esc).join(' ')}</span>`;
  setTimeout(() => { delete $('startnote').dataset.err; draw(); }, 6000);
}
$('start').onclick = async () => {
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission();
  $('start').disabled = true;
  if (mode === 'live') return liveCheck();
  const r = await OC.post('/api/chase', body());
  if (r.ok) { location.href = '/chase'; return; }
  refused(r.data.errors);
};

// ---------- Live: the confirm, then the order is staged (R2: nothing is placed) ----------
$('mode').onclick = e => {
  const b = e.target.closest('button');
  if (!b || b.disabled) return;
  mode = b.dataset.m;
  let seen = false;
  try { seen = localStorage.getItem('oc-live-seen') === '1'; localStorage.setItem('oc-live-seen', '1'); } catch (err) { /* private window: show it again */ }
  $('whatchanges').innerHTML = mode === 'live' && !seen ? `<div class="note info small" style="margin-top:10px"><b>You chose Live for the first time. What changes:</b>
    <ul style="margin:6px 0 4px;padding-left:18px"><li>The tool stages the order: it saves a copy and places nothing.</li><li>The signed helper (coming) holds your Kraken key and places the order after a Touch ID. This tool never receives the key.</li><li>Live is spot only: buy or sell.</li></ul>This note shows one time only.</div>` : '';
  draw();
};
function liveCheck() {
  const p = snap.pairs[$('pairsel').value];
  check = { first: snap.live.first, min_qty: p ? p.ordermin : null };
  if (check.first && check.min_qty && $('amt').value.trim() !== check.min_qty) { $('amt').value = check.min_qty; draw(); }   // Q6: the Kraken minimum
  openConfirm();
}
function openConfirm() {
  const c = check, B = $('pairsel').value.split('/')[0];
  $('firstbadge').innerHTML = c.first ? '<span class="badge warn">FIRST LIVE ORDER</span>' : '';
  $('ct').textContent = c.first ? 'Your first real order: stage it for the helper?' : 'Stage a real order for the helper?';
  $('cfirst').innerHTML = c.first ? `<div class="note info small" style="margin-bottom:12px">This is the first real order the tool sends. The form set the Kraken minimum, ${OC.qty(c.min_qty)} ${OC.esc(B)}. After it ends, check the order in Kraken Pro, then compare it with the result page.</div>` : '';
  const rows = [['Order', `<b>${what === 'buy' ? 'Buy' : 'Sell'} ${OC.qty($('amt').value)} ${OC.esc(B)}</b> on ${OC.esc($('pairsel').value)}`],
    [$('capword').textContent, OC.esc($('capline').textContent) + `<div class="tiny muted">${OC.esc($('capsub').textContent)}</div>`],
    ['Timeout', `${OC.esc($('to').selectedOptions[0].textContent)}, then one IOC at the ${what === 'buy' ? 'cap' : 'floor'} for the rest`],
    ['Worst case', $('worst').innerHTML]];
  $('ctab').innerHTML = rows.map(([k, v]) => `<tr><td class="muted" style="width:34%;text-align:left">${k}</td><td style="text-align:left">${v}</td></tr>`).join('');
  $('ccheck').innerHTML = (c.first ? '<label class="check" style="margin-bottom:12px"><input type="checkbox" id="okfirst"><div><b>I checked the amount and the worst case.</b><div class="tiny muted">Asked one time only, for the first live order.</div></div></label>' : '');
  const ticks = () => { $('cyes').disabled = $('okfirst') && !$('okfirst').checked; };
  document.querySelectorAll('#ccheck input').forEach(i => { i.onchange = ticks; });
  ticks();
  $('cmsg').textContent = '';
  $('cyes').classList.remove('hidden');
  $('confirm').classList.remove('hidden');
  document.querySelector('.app').inert = true;
  $('cno').focus();
}
function closeConfirm() { $('confirm').classList.add('hidden'); document.querySelector('.app').inert = false; draw(); $('start').focus(); }
$('cno').onclick = closeConfirm;
$('confirm').addEventListener('keydown', e => { if (e.key === 'Escape') closeConfirm(); });
$('cyes').onclick = async () => {
  $('cyes').disabled = true; $('cyes').textContent = 'Staging the order…';
  const r = await OC.post('/api/chase', { ...body(), first_ok: !!($('okfirst') && $('okfirst').checked) });
  $('cyes').textContent = 'Stage the order';
  if (r.ok) {
    $('cyes').classList.add('hidden');
    $('cmsg').innerHTML = `<div class="note ok small" id="stagednote"><b>Staged: waiting for the helper.</b> Nothing was placed on Kraken. Order <span class="mono">${OC.esc(r.data.staged.id)}</span>, SHA-256 <span class="mono">${OC.esc(r.data.sha256.slice(0, 16))}…</span></div>`;
    return;
  }
  $('cmsg').innerHTML = `<span class="tone-bad">${(r.data.errors || ['Refused.']).map(OC.esc).join(' ')}</span>`;
  $('cyes').disabled = false;
};

async function init() {
  if (params.get('rest')) {
    const r = await fetch('/api/chase/' + encodeURIComponent(params.get('rest')));
    if (r.ok) {
      restOf = await r.json();
      const rest = Number(restOf.qty) - Number(restOf.filled);
      $('pairsel').value = restOf.pair.symbol; what = restOf.side;
      $('amt').value = rest.toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
      $('restnote').classList.remove('hidden');
      $('restnote').textContent = (Number(restOf.filled) > 0 ? `You chase the rest of your last order: ${OC.qty(rest)} ${restOf.pair.base}.` : `You chase your last order again: ${OC.qty(rest)} ${restOf.pair.base}. Nothing filled.`) +
        ` The ${what === 'buy' ? 'cap is the ask' : 'floor is the bid'} at the new start, not the old ${what === 'buy' ? 'cap' : 'floor'}.`;
    }
  }
  if (MARGIN.includes(params.get('what'))) {      // from "Close this position" of a margin chase
    what = params.get('what');
    if (params.get('pair')) { $('pairsel').value = params.get('pair'); posKey = params.get('pair') + '|' + params.get('dir'); }
  }
  OC.stream(s => {
    const first = !snap;
    snap = s;
    busy = !!(s.chase && s.chase.phase !== 'done');
    if (first && busy) $('pairsel').value = s.watched;
    if (s.account && account && s.account.at >= account.at) account = s.account;   // also a new price of a pair
    keepWatching();
    draw();
  });
  await loadAccount();
  draw();
}
init();
