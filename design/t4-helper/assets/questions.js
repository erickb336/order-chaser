/* Decisions: what R1-R10 and G25 replace in the approved T4 design, and why. */
'use strict';
OC.chrome('questions', 'none');
OC.$('sw').remove();
// [earlier answer, what it said, what replaces it, reason, [[label, href]]]
const REPLACED = [
  ['Q3, C6', 'Paste the key one time in Setup. The page sends it and clears the fields.', 'R1: you enter the key in Terminal with oc-signer setup (no echo). No page has a key field.', 'A page key field sends the key through the browser and Python. With R1, only the signed helper ever has it.', [['Setup: key not set', 'setup.html#nokey']]],
  ['Q9, C7', 'Click Allow in a Keychain prompt one time at each start. The tool keeps the key in memory.', 'R3 and G25 2a, 5a: one Touch ID per chase, on a sheet that shows the exact order. Touch ID only.', 'After one Allow, any code in the Python process could use the key for hours. A touch now covers one chase only.', [['Staged: touch now', 'chase.html#staged'], ['The macOS sheet', 'chase.html#sheet']]],
  ['Live start', 'Start a live chase on the form, with a confirm dialog (POST /api/chase with mode live).', 'R2: the page can only stage an order. The helper places it after your touch.', 'A scraped page token must not place an order. Stage plus touch puts a person in each order.', [['Form: live ready', 'new.html#live'], ['Stage options', 'compare.html']]],
  ['Safety timer', 'Renew a 60 s cancel-all on Kraken (CancelAllOrdersAfter) every 20 s. If the tool stops, Kraken cancels ALL orders.', 'R8 and G25 3a: each order expires at Kraken 30 s after the chase ends (GTD). Chases end within 10 min. The helper cancels only its own ids if order-chaser stops.', 'The cancel-all also removed your stop-loss and take-profit orders on other pairs.', [['Live chase: expiry card', 'chase.html#resting'], ['Reconcile: helper cancelled it', 'reconcile.html#watchdog']]],
  ['Q7 (A+)', 'The confirm warns about your other open orders; with a stop-loss or take-profit you tick a box.', 'Removed. No screen warns about other orders.', 'R8 makes it unnecessary: nothing in this flow can cancel another order, so there is nothing to accept.', [['Form: live ready', 'new.html#live']]],
  ['Q10, G19 P1, P2', 'Setup asks if another tool uses the account. If yes, live stays off until you use a Kraken sub-account; the tool trusts your answer.', 'Removed.', 'The sub-account was needed only because the account-wide timer replaced other tools\' timers. R8 removes that timer.', [['Setup: key set', 'setup.html#off']]],
  ['G19 P3, C8', 'After the timer fires, list the other orders Kraken cancelled, stop-loss and take-profit first. Never place them again.', 'Removed. Other orders are never cancelled, so there is no list.', 'R8. The rule behind P3 stays in a stronger form: the helper places only orders that you touched for.', [['Live chase: helper stopped', 'chase.html#h-stopped']]],
  ['Live margin (T5 in live)', 'Margin open and close orders in live, with a margin confirm.', 'R7: live is spot only. The helper refuses margin, leverage and shorts. Margin stays in dry run.', 'R9: no leverage before a protected stop on Kraken. That is not in this task.', [['Form: margin asked', 'new.html#margin'], ['Refused: margin field', 'chase.html#r-margin']]],
];
const CHANGED = [
  ['Q5', 'After a restart, cancel an open order of the chase at once.', 'Kept, but it needs a touch now: the helper forgot the key when it stopped. Live stays blocked until it is done.', [['Reconcile: needs your touch', 'reconcile.html#touch']]],
  ['C1, C2, C9', 'Setup tests the key permissions (Query Funds, Withdraw off, Cancel/Close) and uses the Kraken labels.', 'Kept. The helper runs the test in Terminal and the page shows the result.', [['Setup: key set', 'setup.html#off']]],
  ['Q4', 'Refuse a key with Withdraw Funds and remove it.', 'Kept. The helper refuses it in Terminal and does not save it.', [['Setup: key refused', 'setup.html#withdraw']]],
  ['Q1, Q2, Q6, Q8', 'Live per chase on the form; the form opens in dry run; first live order at the minimum with one tick; private feed lost: REST every 5 s.', 'Kept as they were.', [['Form: first live order', 'new.html#first'], ['Chase: private feed lost', 'chase.html#pfeed']]],
];
const NEW = [
  ['R1', 'A signed helper holds the key; you enter it in Terminal.', [['Setup', 'setup.html#nokey']]],
  ['R2', 'The page can only show or stage a live order.', [['Form: live ready', 'new.html#live']]],
  ['R3, G25 2a', 'One Touch ID per chase; the sheet shows pair, side, size, limit, the order id and the final IOC id at the same cap.', [['The macOS sheet', 'chase.html#sheet']]],
  ['R4', 'The helper signs only that chase\'s ids.', [['Amended (no new touch)', 'chase.html#amended'], ['Final IOC', 'chase.html#ioc']]],
  ['R5, G25 4a', 'Limits and the live switch in the helper, behind a touch. The daily stop counts committed risk at the exit plan.', [['Setup: limits', 'setup.html#off'], ['Refused: daily stop', 'chase.html#r-day']]],
  ['R6', 'The staged copy and its SHA-256; the helper signs only when it matches.', [['Refused: hash mismatch', 'chase.html#r-hash']]],
  ['R7', 'Spot only in live.', [['Form: margin asked', 'new.html#margin']]],
  ['R8, G25 3a', 'GTD = chase end + 30 s; 10-min cap; the helper cancels only its own ids.', [['Helper stopped', 'chase.html#h-stopped'], ['Expired (GTD)', 'chase.html#expired']]],
  ['R9, R10', 'A protected stop before any leverage (not built); the spike first. No screen.', []],
  ['G25 1a', 'A free Personal Team: the helper profile lasts 7 days, then live fails closed.', [['Profile expires in 2 days', 'setup.html#expiring'], ['Profile expired', 'setup.html#expired']]],
  ['G25 5a', 'Touch ID only: a new fingerprint clears the key.', [['Fingerprints changed', 'setup.html#finger']]],
];
const links = l => l.length ? l.map(([t, h]) => `<a href="${h}">${t}</a>`).join('<br>') : '<span class="muted">No screen</span>';
document.getElementById('view').innerHTML = `<div style="max-width:1040px"><h1>Decisions: what the helper changes</h1>
  <p class="muted">The approved T4 design assumed a pasted key, one Keychain Allow per start, a live start on the form and an account-wide safety timer. The owner replaced these with money-sage routes.md section 14 (R1 to R10) and the G25 answers. This page lists each earlier answer that changes, and why. The open product questions are in the designer's report.</p>
  <h2 style="margin-top:22px">Earlier answers that R1 to R10 and G25 replace</h2>
  <div class="card" style="padding:8px 16px"><table class="t dec"><tr><th style="width:110px">Earlier</th><th>It said</th><th>Now</th><th>Why</th><th style="width:190px">See it</th></tr>
  ${REPLACED.map(([a, b, c, d, l]) => `<tr><td><b>${a}</b></td><td class="small">${b}</td><td class="ans">${c}</td><td class="small">${d}</td><td class="small">${links(l)}</td></tr>`).join('')}</table></div>
  <h2 style="margin-top:22px">Earlier answers that stay</h2>
  <div class="card" style="padding:8px 16px"><table class="t dec"><tr><th style="width:110px">Earlier</th><th>It said</th><th>Now</th><th style="width:190px">See it</th></tr>
  ${CHANGED.map(([a, b, c, l]) => `<tr><td><b>${a}</b></td><td class="small">${b}</td><td class="ans">${c}</td><td class="small">${links(l)}</td></tr>`).join('')}</table></div>
  <h2 style="margin-top:22px">The owner's rules and where they show</h2>
  <div class="card" style="padding:8px 16px"><table class="t dec"><tr><th style="width:110px">Rule</th><th>What it says</th><th style="width:220px">See it</th></tr>
  ${NEW.map(([a, b, l]) => `<tr><td><b>${a}</b></td><td>${b}</td><td class="small">${links(l)}</td></tr>`).join('')}</table></div></div>`;
