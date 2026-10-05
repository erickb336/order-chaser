/* The form in live mode. The page can only stage a live order (R2); the helper places it after your Touch ID. SAMPLE data. */
'use strict';
OC.chrome('new', 'dry');
const { $, C, LIM } = OC;
const STATES = [
  ['dry', 'Dry run (unchanged)', 'Mode'], ['nohelper', 'Live: no helper'], ['liveoff', 'Live: switch off in the helper'],
  ['live', 'Live ready', 'Live'], ['first', 'First live order'], ['over', 'Over a limit (page warns)'], ['margin', 'Margin asked: dry run only'], ['staging', 'Staging (loading)'],
];
const field = (lab, val, help, cls) => `<div class="field"><label class="f">${lab}</label><div class="input ${cls || 'num'}">${val}</div>${help ? `<div class="help">${help}</div>` : ''}</div>`;
function view(s) {
  const live = s !== 'dry';
  OC.setMode(live ? 'live' : 'dry');
  const ready = ['live', 'first', 'over', 'staging'].includes(s);
  const size = s === 'first' ? '0.0001 BTC' : s === 'over' ? '0.0090 BTC' : C.size;
  const value = s === 'first' ? '6.27' : s === 'over' ? '564.01' : C.value;
  const loss = s === 'first' ? '0.24' : s === 'over' ? '21.69' : C.loss;
  const what = `<div class="seg"><button aria-pressed="true" class="buy">Buy</button><button>Sell</button></div>` +
    (s === 'margin' ? '<div class="seg" style="margin-top:8px"><button>Spot</button><button aria-pressed="true" class="long">Margin long 3x</button></div>' : (live ? '<div class="help">Spot only in live. Margin, leverage and shorts stay in dry run.</div>' : '<div class="seg" style="margin-top:8px"><button aria-pressed="true">Spot</button><button>Margin</button></div>'));
  let block = '', btn = '', side = '';
  if (s === 'dry') { btn = '<button class="btn drybtn">Start dry run</button>'; }
  else if (s === 'nohelper') { block = '<div class="note info small">Live needs the helper and your key. <a href="setup.html#none">Set up the helper</a>. Dry run works now.</div>'; btn = '<button class="btn livebtn" disabled>Stage for Touch ID</button>'; }
  else if (s === 'liveoff') { block = '<div class="note info small">Live is off in the helper. Turn it on in Terminal with <span class="kbd">oc-signer live on</span> (Touch ID). <a href="setup.html#off">Setup</a></div>'; btn = '<button class="btn livebtn" disabled>Stage for Touch ID</button>'; }
  else if (s === 'margin') { block = '<div class="note warn small" role="alert"><b>Margin is dry run only.</b> Live is spot: no margin, leverage or shorts (R7). Switch to Spot, or switch the mode to Dry run.</div>'; btn = '<button class="btn livebtn" disabled>Stage for Touch ID</button>'; }
  else if (s === 'over') { block = `<div class="note bad small" role="alert"><b>Over your max order value: ${value} of ${LIM.value} USD.</b> The helper would refuse it, so the page does not stage it. Make the amount smaller (0.0079 BTC fits).</div>`; btn = '<button class="btn livebtn" disabled>Stage for Touch ID</button>'; }
  else {
    block = `<div class="note plain small"><b>What Stage does:</b> the page writes this order to a fixed copy and keeps its SHA-256 hash. The helper checks the copy against your limits, then your Mac asks for Touch ID and shows this exact order. Nothing goes to Kraken before your touch.</div>` +
      (s === 'first' ? '<div class="note warn small" style="margin-top:10px"><label class="check on"><input type="checkbox" checked> This is my first live order. I use the Kraken minimum, 0.0001 BTC.</label></div>' : '');
    btn = s === 'staging' ? '<button class="btn livebtn" disabled>Staging…</button><span class="small muted" role="status">Writing the copy and asking the helper. About 1 s.</span>' : '<a class="btn livebtn" href="chase.html#staged">Stage for Touch ID</a>';
  }
  if (ready) {
    const over = s === 'over';
    side = `<div class="card"><h3>Against your limits</h3><div class="lim">
      <span class="k">Order value (size × cap + fees)</span><span class="v ${over ? 'tone-bad' : ''}">${value} / ${LIM.value}</span>
      <span class="k">Loss at the exit plan</span><span class="v ${over ? 'tone-bad' : ''}">${loss} / ${LIM.loss}</span>
      <span class="k">Committed today after this</span><span class="v">${(18.40 + +loss).toFixed(2)} / ${LIM.day}</span></div>
      <p class="tiny muted" style="margin:8px 0 0">USD. A preview: the helper checks again before it asks for Touch ID. Change limits in the helper.</p></div>`;
  }
  $('view').innerHTML = (live ? '<div class="livestrip" style="margin-bottom:16px">LIVE: a staged order goes to your Kraken account after your Touch ID.</div>' : '') + `<div class="split"><div class="card">
    <h1 style="font-size:20px">New chase</h1>
    ${field('Pair', 'BTC/USD', '', '')}
    <div class="field"><label class="f">What to do</label>${what}</div>
    <div class="grid2">${field('Amount', size, 'Kraken minimum: 0.0001 BTC.')}${field('Timeout', '2 min', live ? 'Then one IOC at the cap. Live chases end within 10 min.' : 'Then one IOC at the cap for the rest.')}</div>
    ${live ? field('Exit plan: sell if the price falls to', C.exit + ' USD', 'The helper counts your loss at this price. The tool does not place this sell on Kraken.') : ''}
    <div class="field"><label class="f">Mode</label><div class="seg lg"><button class="dry" aria-pressed="${!live}">Dry run</button><button class="live" aria-pressed="${live}">Live</button></div></div>
    ${block}<div class="row" style="margin-top:14px">${btn}</div></div>
    <aside class="side"><div class="card"><h3>BTC/USD now <span class="sample" style="margin-left:6px">sample</span></h3>
      <table class="kv"><tr><td class="muted">Best bid = start</td><td class="num tone-fill">${C.start}</td></tr><tr><td class="muted">Best ask = your cap</td><td class="num tone-bad">${C.cap}</td></tr></table></div>${side}</aside></div>`;
  OC.pill('helper', ...({ nohelper: ['bad', 'Helper: not installed'], liveoff: ['', 'Helper: ready · live off'] }[s] || ['', 'Helper: ready · live on']));
}
OC.switcher($('sw'), 'New chase', STATES, view, 'live');
