OC.chrome('history'); OC.stream(() => {});
const $ = OC.$;
const HEAD = '<tr><th>When</th><th>Pair</th><th>Type</th><th class="r">Filled / asked</th><th class="r">Average price</th><th class="r">Saving USD</th><th>Mode</th><th>Outcome</th></tr>';
const OUT = { filled: ['ok', 'Filled (simulated)'], stopped: ['plain', 'Stopped by you'], ended: ['bad', 'Ended: tool restarted'], refused: ['bad', 'Order rejected'], cancelfail: ['bad', 'Cancel failed'] };
function when(t) {
  const d = new Date(t * 1000), today = new Date();
  const hm = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return 'Today ' + hm;
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) + ' ' + hm;
}
let filter = 'all';
document.getElementById('filt').onclick = e => {
  const b = e.target.closest('button');
  if (!b) return;
  filter = b.dataset.f;
  document.querySelectorAll('#filt button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  document.querySelectorAll('tr.click').forEach(tr => tr.classList.toggle('hidden', filter !== 'all' && tr.dataset.type !== filter));
};
// The Type cell and the outcome of a margin row.
function mtype(r) {
  const what = r.close ? `Close ${r.dir}` : `Open ${r.dir} · ${r.leverage}x`;
  return `<span class="badge mg">Margin</span> <span class="${r.close ? '' : r.dir === 'long' ? 'tone-fill' : 'tone-bad'}" style="white-space:nowrap">${what}</span>`;
}
function moutcome(r) {
  if (r.outcome === 'liquidated') return ['bad', 'Liquidated'];
  if (r.outcome === 'nopos') return ['warn', 'Ended: position closed'];
  if (r.outcome === 'refused') return ['bad', 'Order rejected'];
  if (r.close) return OC.closeBadge(r.outcome, r.filled, r.qty, r.pos_rest);
  if (Number(r.filled) <= 0) return ['bad', 'Nothing opened'];
  return Number(r.filled) >= Number(r.qty) ? ['ok', 'Position opened'] : ['warn', 'Part opened: position open'];
}
async function load() {
  $('out').innerHTML = '<table class="t">' + HEAD + [1, 2, 3].map(() => '<tr>' + Array(8).fill('<td><span class="skel" style="display:inline-block;width:80%">x</span></td>').join('') + '</tr>').join('') + '</table>';
  let rows;
  try { const r = await fetch('/api/history'); if (!r.ok) throw new Error(r.status); rows = await r.json(); }
  catch (e) {
    $('out').innerHTML = '<div class="note bad" style="margin:12px"><b>The tool cannot read the history file.</b> No order went to Kraken: this version runs dry runs only. <button class="btn" id="retry" style="margin-left:8px;padding:4px 10px;font-size:13px">Try again</button></div>';
    $('retry').onclick = load; return;
  }
  if (!rows.length) {
    $('out').innerHTML = '<div style="text-align:center;padding:48px 20px"><h2>No chases yet</h2><p class="muted">Each chase you run, dry or live, shows here with its fill and its saving.</p><a class="btn drybtn" href="/new">Start a dry run</a></div>';
    return;
  }
  $('out').innerHTML = '<table class="t">' + HEAD + rows.map(r => {
    const limitWord = r.side === 'buy' ? 'cap' : 'floor', d = r.price_decimals;
    const fw = OC.fillWord(r.filled, r.qty);
    const o = r.outcome === 'notfilled' ? ['bad', fw + ': ' + (r.nofeed ? 'no valid price, no IOC' : r.beyond ? (r.side === 'buy' ? 'above cap' : 'below floor') : `IOC at the ${limitWord}`)]
      : r.outcome === 'belowmin' ? ['bad', fw + ': rest below minimum']
      : r.outcome === 'cancelfail' && r.mode === 'live' ? ['bad', 'Cancel failed: check Kraken']
      : ['stopped', 'ended'].includes(r.outcome) ? [OUT[r.outcome][0], OUT[r.outcome][1] + ' · ' + fw]
      : ['timer', 'venuecancel', 'noanswer'].includes(r.outcome) ? ['bad', OC.LABEL[r.outcome] + ' · ' + fw]
      : OUT[r.outcome] || ['plain', r.outcome];
    const taker = 100 - OC.pct(r.maker_qty, r.qty);
    const outcome = r.outcome === 'filled' && taker > 0 ? ['ok', `Filled, ${taker}% ${r.exit === 'fillnow' ? 'by "Fill the rest now"' : 'after timeout'}${r.mode === 'live' ? '' : ' (simulated)'}`] : o;
    const sv = Number(r.saving), m = r.dir != null;
    const out = m ? moutcome(r) : outcome;
    const type = m ? mtype(r) : `<span class="badge plain">Spot</span> <span class="${r.side === 'buy' ? 'tone-fill' : 'tone-bad'}">${r.side === 'buy' ? 'Buy' : 'Sell'}</span>`;
    return `<tr class="click${filter !== 'all' && filter !== (m ? 'margin' : 'spot') ? ' hidden' : ''}" data-type="${m ? 'margin' : 'spot'}" data-id="${OC.esc(r.id)}" tabindex="0"><td>${when(r.started)}</td><td>${OC.esc(r.pair)}</td><td>${type}</td><td class="r num">${OC.qty(r.filled)} / ${OC.qty(r.qty)}</td><td class="r num">${Number(r.filled) ? OC.px(r.avg, d) : '—'}</td><td class="r num ${sv >= 0 ? 'tone-fill' : 'tone-bad'}">${sv < 0 ? '−' : ''}${OC.usd(Math.abs(sv))}</td><td>${r.mode === 'live' ? '<span class="badge live">Live</span>' : '<span class="badge dry">Dry run</span>'}</td><td><span class="badge ${out[0]}">${out[1]}</span></td></tr>`;
  }).join('') + '</table>';
  document.querySelectorAll('tr.click').forEach(tr => {
    const go = () => location.href = '/result?id=' + encodeURIComponent(tr.dataset.id);
    tr.onclick = go; tr.onkeydown = e => { if (e.key === 'Enter') go(); };
  });
  $('tot').innerHTML = `Live chases: 0 · Dry runs: ${rows.length} (dry-run savings are simulated and not counted)`;
}
load();
