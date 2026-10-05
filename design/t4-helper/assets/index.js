/* The map of the prototype. */
'use strict';
OC.chrome('index', 'none');
OC.$('sw').remove();
const L = (h, t) => `<a href="${h}">${t}</a>`;
const ROWS = [
  ['setup.html', 'Setup', [['#none', 'helper not installed'], ['#nokey', 'key not set: the Terminal command'], ['#waiting', 'waiting for Terminal'], ['#withdraw', 'key refused (Withdraw on)'], ['#off', 'key set, live off, limits read-only'], ['#on', 'live on'], ['#expiring', 'profile expires in 2 days'], ['#expired', 'profile expired: fails closed'], ['#finger', 'fingerprints changed']]],
  ['new.html', 'Form (live)', [['#dry', 'dry run'], ['#nohelper', 'no helper'], ['#liveoff', 'live switch off'], ['#live', 'live ready: what Stage does'], ['#first', 'first live order'], ['#over', 'over a limit'], ['#margin', 'margin asked'], ['#staging', 'staging']]],
  ['chase.html', 'Touch and chase', [['#staged', 'staged: touch now'], ['#sheet', 'the macOS sheet'], ['#cancelled', 'touch cancelled'], ['#notouch', 'no touch in 60 s'], ['#r-value', 'refused: max value'], ['#r-loss', 'refused: max loss'], ['#r-day', 'refused: daily stop'], ['#r-hash', 'refused: hash'], ['#r-margin', 'refused: margin'], ['#r-profile', 'refused: profile expired'], ['#placing', 'placing'], ['#resting', 'resting'], ['#amended', 'amended'], ['#ioc', 'final IOC'], ['#pfeed', 'private feed lost'], ['#h-stopped', 'helper stopped'], ['#h-cancel', 'cancel with a touch'], ['#filled', 'filled'], ['#partial', 'part filled + IOC'], ['#stopped', 'stopped by you'], ['#expired', 'expired (GTD)']]],
  ['reconcile.html', 'Restart reconcile', [['#touch', 'needs your touch'], ['#reading', 'reading'], ['#declined', 'touch cancelled'], ['#watchdog', 'helper cancelled it'], ['#open', 'still open'], ['#expired', 'expired (GTD)'], ['#filled', 'filled'], ['#cantread', 'cannot read Kraken']]],
];
document.getElementById('view').innerHTML = `<div style="max-width:940px">
  <h1>Live trading with the Touch ID helper: clickable prototype</h1>
  <p class="muted">This prototype replaces the live flow of the approved T4 design. A signed helper (OCSigner.app) now holds the Kraken key. The page can only stage an order; your Touch ID on the Mac sends it. Every number, id, hash and date is <b>sample data</b>.</p>
  <p class="small"><b>Status: draft for the owner.</b> It follows money-sage routes.md section 14 (R1 to R10) and your answers to G25. The <a href="questions.html">Decisions</a> page lists which earlier answers this replaces, and why.</p>
  <h2 style="margin-top:22px">The flow</h2>
  <div class="flow">${L('setup.html#nokey', '1. Setup: key in Terminal')}<span class="arr">→</span>${L('new.html#live', '2. Form: Stage')}<span class="arr">→</span>${L('chase.html#staged', '3. Touch ID on your Mac')}<span class="arr">→</span>${L('chase.html#resting', '4. Live chase')}<span class="arr">→</span>${L('chase.html#partial', '5. Result with Kraken ids')}</div>
  <div class="flow" style="margin-top:8px"><span class="small muted">After a stop:</span>${L('reconcile.html#touch', '6. Restart reconcile (touch)')}<span class="arr">→</span>${L('reconcile.html#watchdog', 'Closed')}</div>
  <h2 style="margin-top:22px">Screens and states</h2>
  <div class="card" style="padding:8px 16px"><table class="t"><tr><th>Screen</th><th>States (state links)</th></tr>
  ${ROWS.map(([p, t, st]) => `<tr><td>${L(p, t)}</td><td class="small">${st.map(([h, x]) => L(p + h, x)).join(' · ')}</td></tr>`).join('')}
  <tr><td>${L('compare.html', 'Stage options')}</td><td class="small">Where the wait for the touch shows: A (built), B, C</td></tr></table></div>
  <h2 style="margin-top:22px">Rules the screens keep</h2>
  <ul class="small">
    <li>No page has a key field. You enter the key in Terminal with <span class="kbd">oc-signer setup</span>; it does not show as you type.</li>
    <li>One Touch ID per chase. The sheet shows pair, side, size, the start limit and the cap, both order ids (the order and the final IOC) and the copy number. Two chases need two touches.</li>
    <li>The page and the sheet show the same copy number: the first 8 characters of the staged copy's SHA-256.</li>
    <li>The helper checks the limits before it asks for a touch. A refused order costs no touch and sends nothing.</li>
    <li>Limits and the live switch show read-only. You change them in the helper, with Touch ID.</li>
    <li>Each order shows its Kraken expiry (GTD = chase end + 30 s). Chases end within 10 minutes.</li>
    <li>No screen warns about your other orders: nothing in this flow can cancel them (R8).</li>
    <li>Live is spot only. Margin stays in dry run.</li>
    <li>Dark by default. All text keeps 4.5:1 contrast (checked by script).</li>
  </ul>
  <p class="small muted">Not in this prototype: the history list and dry-run screens (unchanged), the Terminal commands beyond their sample output, and the first live test (it needs your go-ahead at that time).</p></div>`;
