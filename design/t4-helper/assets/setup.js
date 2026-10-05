/* Setup: the helper (OCSigner.app) holds the key. You enter the key in Terminal, never in a page (R1). SAMPLE data. */
'use strict';
OC.chrome('setup', 'dry');
const { $, C, LIM } = OC;
const STATES = [
  ['none', 'Helper not installed', 'Helper'], ['nokey', 'Installed, key not set'], ['waiting', 'Waiting for Terminal (loading)'],
  ['withdraw', 'Key refused: Withdraw on', 'Key'], ['off', 'Key set, live off'], ['on', 'Key set, live on'],
  ['expiring', 'Profile expires in 2 days', 'Helper profile'], ['expired', 'Profile expired: fails closed'], ['finger', 'Fingerprints changed: key cleared'],
];
const CMD = 'oc-signer setup';
const copy = cmd => `<div class="copybox"><code>${cmd}</code><button class="btn sm" type="button">Copy</button></div>`;
const step = (no, cls, title, body) => `<div class="step ${cls}"><div class="no">${no}</div><div><h3 style="margin:4px 0 6px">${title}</h3>${body}</div></div>`;
const helperRow = s => {
  const rows = {
    none: ['bad', 'Not found'], nokey: ['fill', 'Installed · signed · profile valid to 2026-10-11'], waiting: ['fill', 'Installed · signed · profile valid to 2026-10-11'],
    withdraw: ['fill', 'Installed · signed · profile valid to 2026-10-11'], off: ['fill', 'Installed · signed · profile valid to 2026-10-11'], on: ['fill', 'Installed · signed · profile valid to 2026-10-11'],
    expiring: ['warn', 'Installed · signed · profile expires 2026-10-07 (in 2 days)'], expired: ['bad', 'Installed · profile expired 2026-10-04 · macOS will not start it'], finger: ['fill', 'Installed · signed · profile valid to 2026-10-11'],
  };
  const [t, x] = rows[s];
  return `<tr><td class="muted">Helper (OCSigner.app)</td><td class="tone-${t}">${x}</td></tr>`;
};
const keyRow = s => {
  const x = { none: ['muted', 'No helper, no key'], nokey: ['warn', 'Not set'], waiting: ['you', 'Waiting for Terminal…'], withdraw: ['bad', 'Refused, not saved'],
    off: ['fill', 'Set on 2026-10-05 09:31 (the page never sees it)'], on: ['fill', 'Set on 2026-10-05 09:31 (the page never sees it)'], expiring: ['fill', 'Set on 2026-10-05 09:31'],
    expired: ['muted', 'Set, but the helper cannot start'], finger: ['bad', 'Cleared by macOS: your fingerprints changed'] }[s];
  return `<tr><td class="muted">Kraken key</td><td class="tone-${x[0]}">${x[1]}</td></tr>`;
};
const liveRow = s => {
  const on = s === 'on' || s === 'expiring';
  return `<tr><td class="muted">Live switch (in the helper)</td><td>${on ? '<span class="badge live">ON</span>' : '<span class="badge plain">OFF</span>'}${['expired', 'finger'].includes(s) ? ' <span class="small tone-bad">· live blocked</span>' : ''}</td></tr>`;
};
const term = s => {
  const ok = ['off', 'on', 'expiring'].includes(s);
  let t = `<span class="p">$</span> ${CMD}\nKraken API key (hidden as you type): \nKraken private key (hidden as you type): \n<span class="c">Touch ID to save the key in your Keychain… done</span>\nTesting the key with Kraken (6 checks)…\n`;
  t += s === 'withdraw' ? '  Withdraw Funds ............ ON\n<span class="w">Refused: this key can withdraw money. The helper did not save it.\nMake a key with Withdraw Funds off on Kraken, then run oc-signer setup again.</span>'
    : '  Query Funds ............... on\n  Modify Orders ............. on\n  Cancel/Close Orders ....... on\n  Query Open Orders & Trades  on\n  Access WebSockets API ..... on\n  Withdraw Funds ............ off  (required)\n<span class="p">Saved. Live is OFF. Turn it on with: oc-signer live on</span>';
  return `<div class="term" aria-label="Terminal (sample)">${t}</div><div class="tiny muted" style="margin-top:4px">Sample Terminal output. You type the key in Terminal; it does not show as you type.</div>` + (ok ? '' : '');
};
function view(s) {
  const top = `<h1>Setup</h1><p class="muted" style="max-width:760px">A small signed program, the <b>helper</b> (OCSigner.app), keeps your Kraken key. Only the helper can read the key, and only after your Touch ID. This page never asks for the key and never shows it.</p>`;
  const status = `<div class="card"><h2>Status</h2><table class="kv">${helperRow(s)}${keyRow(s)}${liveRow(s)}</table></div>`;
  let main = '';
  if (s === 'none') {
    main = `<div class="card"><h2>Install the helper</h2><div class="steps">` +
      step(1, 'cur', 'Build and install OCSigner.app', '<p class="small">Follow <b>docs/helper.md</b> (one time, about 10 minutes). Xcode signs it with your free Apple Personal Team. This Mac only.</p>') +
      step(2, '', 'Enter your Kraken key in Terminal', '<p class="small muted">After step 1.</p>') + step(3, '', 'Turn on live in the helper', '<p class="small muted">After step 2.</p>') +
      '</div><div class="note info small" style="margin-top:12px">Dry run works without the helper. Live needs it.</div></div>';
  } else if (s === 'nokey' || s === 'waiting' || s === 'finger') {
    const head = s === 'finger' ? '<div class="note bad small" style="margin-bottom:12px" role="alert"><b>Your fingerprints changed, so macOS cleared the key.</b> This is the Touch ID only policy: a new fingerprint cannot use an old key. Enter the key again. Live is blocked until you do.</div>' : '';
    main = `<div class="card">${head}<h2>Enter your Kraken key in Terminal</h2><div class="steps">` +
      step('✓', 'done', 'Helper installed', '') +
      step(2, 'cur', 'Open Terminal and run this command', copy(CMD) +
        '<ol class="how small"><li>Paste the API key, then the private key. They do not show as you type.</li><li>Touch ID when your Mac asks.</li><li>The helper tests the key with Kraken and shows the result.</li></ol>' +
        '<div class="note warn small"><b>Never paste the key in a page or a chat.</b> This page has no key field on purpose.</div>' +
        (s === 'waiting' ? '<div class="row small" style="margin-top:12px" role="status"><span class="state tone-you pulse"><i></i></span><span>Waiting for the helper. This page updates when the key is set. You can leave it open.</span></div>' : '')) +
      step(3, '', 'Turn on live in the helper', '<p class="small muted">After step 2.</p>') + '</div></div>';
  } else if (s === 'withdraw') {
    main = `<div class="card"><div class="note bad small" role="alert"><b>The helper refused the key: it can withdraw money (Withdraw Funds is on).</b> It did not save it. Make a new key on Kraken with Withdraw Funds off, then run the command again.</div>${copy(CMD)}<h3 style="margin-top:14px">What Terminal showed</h3>${term(s)}</div>`;
  } else if (s === 'expired') {
    main = `<div class="card"><div class="note bad small" role="alert"><b>The helper's signing profile expired on 2026-10-04, so macOS will not start it.</b> Live is blocked: nothing can sign an order. Dry run still works. Your key stays in the Keychain.</div>
      <h2>Sign the helper again</h2><ol class="how small"><li>Open the OCSigner project in Xcode.</li><li>Click Run (Product, Run). Xcode makes a new 7-day profile.</li><li>Come back here. The page checks again.</li></ol>
      <p class="tiny muted">A free Personal Team profile lasts 7 days (G25 1a). Sample dates.</p></div>`;
  } else {
    const live = s === 'on' || s === 'expiring';
    main = (s === 'expiring' ? '<div class="note warn small" style="margin-bottom:14px" role="status"><b>The helper profile expires in 2 days (2026-10-07).</b> Then live stops until you sign the helper again in Xcode (about 2 minutes). Do it before a chase.</div>' : '') +
      `<div class="card"><h2>The key</h2><p class="small">The key is set and passed the helper's test on 2026-10-05 09:31. The page never sees it. To replace it, run <span class="kbd">${CMD}</span> again.</p>
      <details><summary class="small">What Terminal showed (sample)</summary>${term(s)}</details></div>
      <div class="card"><div class="row"><h2 style="margin:0">Limits in the helper</h2><span class="spacer"></span><span class="badge plain">read-only here</span></div>
      <p class="small muted" style="margin:4px 0 10px">The helper checks every live order against these. The page cannot change them.</p>${OC.limits(`<span class="k">Committed today</span><span class="v">${LIM.used} of ${LIM.day} USD</span>`)}
      <p class="small" style="margin:12px 0 4px">Change them in the helper (Touch ID):</p>${copy('oc-signer limits')}</div>
      <div class="card"><div class="row"><h2 style="margin:0">Live switch</h2><span class="spacer"></span>${live ? '<span class="badge live">ON</span>' : '<span class="badge plain">OFF</span>'}</div>
      <p class="small">${live ? 'Live is on. The form can stage live orders. Each one still needs your Touch ID.' : 'Live is off. The form shows live orders but cannot stage them.'} The switch is in the helper, behind Touch ID. Spot only: margin, leverage and shorts stay in dry run.</p>
      ${copy(live ? 'oc-signer live off' : 'oc-signer live on')}</div>`;
  }
  $('view').innerHTML = top + `<div class="split"><div>${main}</div><aside class="side">${status}
    <div class="card"><h3>What the helper does</h3><ul class="small" style="margin:0;padding-left:18px"><li>Keeps the key. Python never gets it.</li><li>Asks Touch ID once per chase and shows the exact order.</li><li>Signs only that chase's order ids.</li><li>Checks your limits before it asks.</li><li>Never touches your other orders.</li></ul></div></aside></div>`;
  OC.pill('helper', ...({ none: ['bad', 'Helper: not installed'], nokey: ['warn', 'Helper: no key'], waiting: ['warn', 'Helper: no key'], withdraw: ['warn', 'Helper: no key'], off: ['', 'Helper: ready · live off'], on: ['', 'Helper: ready · live on'], expiring: ['warn', 'Helper: profile expires in 2 days'], expired: ['bad', 'Helper: profile expired'], finger: ['bad', 'Helper: key cleared'] }[s]));
}
OC.switcher($('sw'), 'Setup', STATES, view, 'nokey');
