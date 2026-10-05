OC.chrome('chase');
const $ = OC.$;
let current = null, wasActive = null, snapNow = 0;

function side(c, snap) {
  const w = OC.words(c), s = c.summary, B = c.pair.base;
  $('order').textContent = OC.orderName(c);
  $('pair').textContent = c.pair.symbol;
  $('limitname').textContent = w.limitName[0].toUpperCase() + w.limitName.slice(1);
  $('limit').textContent = w.p(c.limit);
  $('yp').textContent = c.price ? w.p(c.price) : (c.pending ? 'sending…' : 'no order');
  $('avg').textContent = Number(s.filled) ? w.p(s.avg) : 'nothing filled';
  $('to').textContent = `${OC.mmss(c.timeout)}, then IOC at the ${w.limitWord}`;
  const isStart = c.limit === (w.buy ? c.start_ask : c.start_bid);
  $('worsthead').textContent = w.buy
    ? (isStart ? 'This chase never costs more than a market order at the start:' : 'This chase never costs more than:')
    : (isStart ? 'This chase never brings less than a market sell at the start:' : 'This chase never brings less than:');
  $('worst').textContent = OC.usd(c.worst) + ' ' + c.pair.quote + (w.buy ? '' : ' received');
  $('worstwhy').textContent = `${OC.qty(c.qty)} × ${w.p(c.limit)} ${w.buy ? '+' : '−'} 0.80% taker fee. The tool uses the highest fee rates, 0.40% maker and 0.80% taker, so this is a true ${w.buy ? 'maximum' : 'minimum'}.`;
  if (w.m) margin(c, w, snap);
  const sv = Number(s.saving);
  $('saving').innerHTML = Number(s.filled)
    ? `So far: <b class="${sv >= 0 ? 'tone-fill' : 'tone-bad'} num">${OC.usd(Math.abs(sv))} ${c.pair.quote}</b> ${sv >= 0 ? (w.buy ? 'less than' : 'more than') : (w.buy ? 'more than' : 'less than')} a market order at the start, for the ${OC.qty(s.filled)} ${B} that filled.`
    : '<span class="muted">So far: nothing filled.</span>';
  const done = c.phase === 'done';
  const rate = done ? c.rate : c.rate;
  $('ratehead').textContent = `Rate counter, estimated (${c.pair.symbol})`;
  $('ratetext').textContent = done ? 'No orders now' : c.replace ? 'Cancel and replace: a cancel adds up to 8' : (c.slow ? 'Near: amends every 15 s' : 'Normal: amends every 5 s');
  $('ratenum').textContent = Math.floor(rate) + ' / 60';
  $('ratebar').style.width = (rate / 60 * 100) + '%';
  $('ratebar').style.background = rate > 40 ? 'var(--warn)' : 'var(--fill)';
}

// Margin: the order rows, the position of this chase, the account margin level (last read) and the worst case.
function margin(c, w, snap) {
  const B = c.pair.base, f = Number(c.filled), done = c.phase === 'done', m = c.margin;
  document.querySelectorAll('.mrow').forEach(r => r.classList.remove('hidden'));
  $('poscard').classList.remove('hidden');
  $('mgstrip').classList.remove('hidden');
  $('mgstrip').textContent = `MARGIN: this chase ${m.close ? `closes a ${c.dir} position, reduce-only` : `opens a ${m.leverage}x ${c.dir} position`}. A position stays open when the tool stops.`;
  // A close: each position keeps its own leverage (the close plan says which ones the close takes).
  $('lev').textContent = m.close ? [...new Set(m.positions.map(p => p.leverage + 'x'))].join(', ') + (m.positions.length > 1 ? ` (${m.positions.length} positions)` : '') : m.leverage + 'x';
  $('ro').textContent = m.close ? `Yes: can only reduce the ${c.dir}` : 'No: this order opens';
  let ps;
  if (c.outcome === 'liquidated') ps = ['bad', 'Liquidated', 'The simulated exchange closed the position at the mark.'];
  else if (!m.close) ps = f <= 0 ? ['muted', done ? 'Nothing opened' : 'Not opened yet', 'Nothing filled, so no position is open.']
    : !done ? ['fill', `Opened: ${OC.qty(f)} of ${OC.qty(c.qty)} ${B}`, `${c.dir === 'long' ? 'Long' : 'Short'}, ${m.leverage}x. This part is a position now.`]
    : ['fill', `Opened: ${OC.qty(f)} ${B} ${c.dir}, ${m.leverage}x`, 'Open until you close it, also when the tool stops.'];
  else {
    const e = c.margin_est, rest = Number(e.rest), start = Number(rest) + f, badge = OC.closeBadge(c.outcome, f, c.qty, rest);
    ps = rest <= 0 ? ['fill', 'Closed', `The ${c.pair.symbol} ${c.dir} is closed. No rollover from now on. ${OC.planLine(c)}`]
      : f <= 0 ? [done ? 'warn' : 'you', `Open: ${OC.qty(start)} ${B} ${c.dir}`, `${done ? 'Nothing closed.' : 'Closing. Nothing closed yet.'} At the start: ${e.start}. ${OC.planLine(c)}`]
      : !done ? ['fill', `Partly closed: ${OC.qty(f)} of ${OC.qty(start)} ${B}`, OC.planLine(c)]
      // At the end a close is judged by its order, as on the result and in the history (owner, G16).
      : [badge[0] === 'ok' ? 'fill' : 'bad', badge[1], OC.planLine(c)];
  }
  $('pstat').className = 'pstat tone-' + ps[0];
  $('pstat').lastChild.textContent = ps[1];
  $('pdet').textContent = ps[2] + ' (simulated)';
  const a = snap && snap.account;
  $('plevel').textContent = a ? `Account margin level (simulated account, read at ${new Date(a.at * 1000).toLocaleTimeString('en-GB')})` : 'Account margin level';
  $('pgauge').innerHTML = a ? OC.gauge(a.level == null ? null : Number(a.level), undefined, c.pair.margin_call, c.pair.margin_stop) : '<p class="tiny muted">Read at the start, after fills and at the end.</p>';
  $('pmarks').innerHTML = a ? OC.markNote(a, snap.watched) : '';
  $('pstays').innerHTML = ps[1] === 'Closed' || c.outcome === 'liquidated' ? '<span class="muted">No position of this chase is open now.</span>'
    : '<b>If the tool stops:</b> the simulated position stays in the simulated account. No order and no position is on Kraken.';
  const Q = c.pair.quote;
  if (!m.close) {
    // A margin short gives no cash: its worst case is the value of the short, after fees.
    $('worsthead').textContent = w.buy ? 'The open never costs more than this in price and fees:' : 'The short never sells for less than this, after fees:';
    $('worst').textContent = (w.buy ? '' : 'Short value: ') + OC.usd(c.worst) + ' ' + Q;
    $('worstwhy').textContent = `${OC.qty(c.qty)} × ${w.p(c.limit)} (${w.limitWord}) ${w.buy ? '+' : '−'} 0.80% taker fee ${w.buy ? '+' : '−'} 0.05% opening fee, the stated maximum (estimate). Not included: rollover, up to ${OC.usd(Number(c.qty) * Number(c.limit) * 0.0005)} ${Q} per 4 h. The position can also lose value as the price moves. That loss has no limit before liquidation.`;
  } else {
    $('worsthead').textContent = w.buy ? 'The close never pays more than this in price and fees:' : 'The close never brings less than this in price, less fees:';
    $('worstwhy').textContent = `${OC.qty(c.qty)} × ${w.p(c.limit)} (${w.limitWord}) ${w.buy ? '+' : '−'} 0.80% taker fee. A close has no opening fee. Rollover stops for the part that closes.`;
  }
}

function actions(st, c, snap) {
  const a = [], rest = Number(c.qty) - Number(c.filled);
  const canStop = ['placing', 'resting', 'amending', 'partial', 'rejected', 'ratenear', 'disconnected', 'replacing'].includes(st);
  const canFill = ['resting', 'amending', 'partial', 'rejected', 'ratenear', 'replacing'].includes(st);
  if (canStop) a.push('<button class="btn danger" id="stop">Stop</button>');
  if (canFill) a.push('<button class="btn" style="white-space:nowrap" id="fillnow">Fill the rest now</button>');
  if (OC.DONE.includes(st)) a.push(`<a class="btn primary" href="/result?id=${encodeURIComponent(c.id)}">See the result</a>`);
  if (c.margin && OC.DONE.includes(st)) a.push(c.open_now ? `<a class="btn" href="${OC.closeLink(c)}">${OC.closeText(c, snap && snap.account)}</a>` : '<a class="btn" href="/new">New chase</a>');
  else if (['filled', 'refused', 'belowmin', 'cancelfail'].includes(st) || (OC.DONE.includes(st) && c.rest_below_min)) a.push('<a class="btn" href="/new">New chase</a>');
  else if (OC.DONE.includes(st) && rest > 0) a.push(`<a class="btn" href="/new?rest=${encodeURIComponent(c.id)}">${OC.againText(c)}, new ${OC.words(c).limitWord}</a>`);
  if (st === 'fallback' || st === 'stopping') a.push('<span class="small muted">No action during the fallback. It ends in 1 to 2 s.</span>');
  if (st === 'pageoffline') a.push('<button class="btn" disabled>Stop</button><span class="small muted">Stop works again when the page reaches the tool.</span>');
  else if (OC.DONE.includes(st) && c.rest_below_min && st !== 'belowmin' && !c.margin) a.push(`<span class="small" id="restmin">${OC.restBelowMin(c)}</span>`);
  else if (OC.DONE.includes(st)) a.push('<span class="spacer"></span><span class="small muted">The chase ended. The form is open again.</span>');
  else if (st !== 'fallback' && st !== 'stopping') a.push('<span class="spacer"></span><span class="small muted">You can close this page. The browser asks "Leave this page?" first. The tool keeps the chase.</span>');
  $('actions').innerHTML = a.join(' ');
  if ($('stop')) $('stop').onclick = () => openStop(c);
  if ($('fillnow')) $('fillnow').onclick = async () => { $('fillnow').disabled = true; await OC.post('/api/chase/fillnow'); };
}

function draw(c, snap, st, ageOff) {
  $('none').classList.add('hidden'); $('main').classList.remove('hidden');
  $('card').innerHTML = OC.card(st, c, snap, ageOff);
  $('log').innerHTML = OC.eventLog(c);
  side(c, snap);
  // Screen readers hear the state when its title changes, not each rebuild of the card.
  const title = OC.copy(st, c, snap, ageOff).title;
  if ($('live').textContent !== title) $('live').textContent = title;
  // Keep the buttons while the state stays the same, so that a click is not lost.
  if ($('actions').dataset.st !== st) { actions(st, c, snap); $('actions').dataset.st = st; }
  const done = c.phase === 'done';
  document.title = (done ? '' : '● ') + OC.copy(st, c, snap, ageOff).title + ' · Order chaser';
}

OC.stream(snap => {
  const c = snap.chase;
  if (!c) { $('main').classList.add('hidden'); $('none').classList.remove('hidden'); document.title = 'No chase · Order chaser'; return; }
  current = c;
  const active = c.phase !== 'done';
  const st = OC.stateOf(c, snap.now);
  draw(c, snap, st, 0);
  if (wasActive === true && !active && 'Notification' in window && Notification.permission === 'granted') {
    new Notification('Order chaser: ' + OC.copy(st, c, snap, 0).title, { body: `${OC.qty(c.filled)} of ${OC.qty(c.qty)} ${c.pair.base} filled (dry run).` });
  }
  wasActive = active;
}, (snap, age) => {
  if (current && current.phase !== 'done') draw(current, null, 'pageoffline', age);
});

window.addEventListener('beforeunload', e => { if (current && current.phase !== 'done') { e.preventDefault(); e.returnValue = ''; } });
// The Stop dialog: modal, focus inside, Escape closes, the page behind is inert.
function openStop(c) {
  $('stopq').textContent = OC.qty(c.filled) + ' ' + c.pair.base;
  $('stopm').classList.toggle('hidden', !c.margin);
  if (c.margin) $('stopm').innerHTML = c.margin.close ? '<b>Margin:</b> the part that did not close stays open as a position.'
    : '<b>Margin:</b> the part that filled is an open position. It stays open after the stop, until you close it.';
  $('stopdlg').classList.remove('hidden');
  document.querySelector('.app').inert = true;
  $('stopno').focus();
}
function closeStop(focus) {
  $('stopdlg').classList.add('hidden');
  document.querySelector('.app').inert = false;
  focus.focus();
}
$('stopdlg').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); closeStop($('stop') || $('card')); }
  if (e.key === 'Tab') {   // keep Tab on the two buttons
    e.preventDefault();
    (document.activeElement === $('stopno') ? $('stopyes') : $('stopno')).focus();
  }
});
$('stopno').onclick = () => closeStop($('stop') || $('card'));
$('stopyes').onclick = async () => { closeStop($('card')); await OC.post('/api/chase/stop'); };
