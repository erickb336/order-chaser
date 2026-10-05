/* Setup: the account question (Q10), the key (Q3, Q4, Q9, C6, C9) and the safety timer. The key fields are not a
   form: the page sends the key one time and clears them; no page shows the key again. */
OC.chrome('setup');
const $ = OC.$;
$('host').textContent = location.host;
// C9: the labels of Kraken's support article, the call that tests each one, and why the tool needs it.
const PERMS = [
  ['need', 'Query Funds', 'Lets the tool check that Withdraw Funds is off.', 'Balance', ''],
  ['need', 'Modify Orders', 'Places and amends the chase order and the IOC.', 'AddOrder', ' with validate: Kraken checks the order and places nothing'],
  ['need', 'Cancel/Close Orders', 'Cancels the order and runs the safety timer.', 'CancelOrder', ' for an order id that does not exist. "Unknown order" means on; "Permission denied" means off'],
  ['need', 'Query Open Orders & Trades', 'Reads the open chase order, its fills and your other open orders.', 'OpenOrders', ''],
  ['need', 'Query Closed Orders & Trades', 'Reads the fills of an ended order, also after a restart.', 'ClosedOrders', ''],
  ['need', 'Access WebSockets API', 'Opens the private feed of your fills. Without it the tool sees no fills.', 'GetWebSocketsToken', ''],
  ['off', 'Withdraw Funds', 'Must be off. The tool refuses a key that can withdraw.', 'WithdrawMethods', ' must fail with "Permission denied". The test is valid only when Query Funds is on'],
];
const WHYSUB = 'Kraken keeps one safety timer for each account. During a live chase this tool sets that timer and renews it every 20 s. That replaces the timer of any other tool on the account. When the timer fires, Kraken cancels the orders of the other tool too.';
const ALLOW = '<div class="note info small" style="margin-top:10px"><b>At each start of the tool, macOS asks one time.</b> Click <b>Allow</b> in that prompt, not Always Allow. The tool reads the key then and keeps it only in memory until it stops. The prompt can name <span class="mono">python3</span>, the program that runs the tool.</div>';
const when = t => new Date(t * 1000).toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
let state = null, busy = false;

// on: the result of the last test (label -> true, false or null); null: not tested; 'testing': the test runs.
function perms(on, verdict) {
  const row = ([kind, name, why, call, res]) => {
    const v = on && on !== 'testing' ? on[name] : undefined;
    let ic = on === 'testing' ? '…' : '•', cls = 'muted', badge = on === 'testing' ? '<span class="badge plain">testing</span>' : kind === 'off' ? '<span class="badge plain">Leave off</span>' : '<span class="badge plain">Turn on</span>';
    if (v === true) [ic, cls, badge] = kind === 'off' ? ['✗', 'tone-bad', '<span class="badge bad">ON: refused</span>'] : ['✓', 'tone-fill', '<span class="badge ok">On</span>'];
    if (v === false) [ic, cls, badge] = kind === 'off' ? ['✓', 'tone-fill', '<span class="badge ok">Off</span>'] : ['✗', 'tone-bad', '<span class="badge bad">Off: turn it on</span>'];
    if (v === null) [ic, cls, badge] = ['?', 'tone-warn', kind === 'off' && verdict === 'nofunds' ? '<span class="badge warn">Cannot check</span>' : '<span class="badge warn">Not tested</span>'];
    return `<div class="perm"><span class="${cls}" aria-hidden="true">${ic}</span><div><b>${name}</b><div class="tiny muted">${why} Test: <span class="mono">${call}</span>${res}.</div></div>${badge}</div>`;
  };
  const grp = (k, t) => `<div class="permgrp">${t}</div>` + PERMS.filter(p => p[0] === k).map(row).join('');
  return '<div class="note plain tiny" style="margin:10px 0 4px"><b>Check these names against your Kraken key page.</b> They come from the Kraken support article. If a name on your key page differs, stop and ask before you create the key.</div>' +
    grp('need', 'Required (6)') + grp('off', 'Must be off (1)') + '<p class="tiny muted" style="margin:10px 0 0">Not needed, leave off: Deposit Funds, Query Ledger Entries, Export Data. The tool does not use them.</p>';
}

function step(id, cls, no, badge) { $('s' + id).className = 'step ' + cls; $('n' + id).textContent = no; $('b' + id).innerHTML = badge; }

function draw() {
  const s = state, a = s.account, k = s.key;
  // Step 2 (Q10, G19): No, or Yes with a sub-account, makes live possible; Yes alone keeps it off.
  if (!a) {
    step('A', 'cur', '2', '<span class="badge plain">One question</span>');
    $('acctbody').innerHTML = `<p class="small" style="margin:8px 0 10px"><b>Does another bot or API tool use this Kraken account?</b></p>
      <div class="row"><button class="btn" id="ano" type="button">No, only this tool</button><button class="btn" id="ayes" type="button">Yes</button></div>
      <p class="tiny muted" style="margin:10px 0 0">Why we ask: ${WHYSUB} You answer one time. You can change the answer here.</p>`;
  } else if (a.answer === 'yes') {
    step('A', 'bad', '!', '<span class="badge warn">Sub-account needed</span>');
    $('acctbody').innerHTML = `<div class="note warn small" style="margin-top:10px"><b>Use a Kraken sub-account for the order chaser.</b> ${WHYSUB} In a sub-account, the timer touches only the chaser's orders.</div>
      <ol class="how small"><li>In Kraken Pro, create a sub-account for the order chaser.</li><li>Move to it only the funds that you want to chase with.</li><li>In step 3, create the API key in the sub-account.</li></ol>
      <div class="row"><button class="btn primary" id="asub" type="button">I use a sub-account for this tool</button><button class="btn sm" id="ano" type="button">Change: no other tool</button></div>
      <p class="tiny muted" style="margin:10px 0 0">You answered Yes on ${when(a.at)}. Live chases stay off until you confirm the sub-account.</p>`;
  } else {
    step('A', 'done', '✓', '<span class="badge ok">Answered</span>');
    $('acctbody').innerHTML = `<p class="small" style="margin:8px 0 0">${a.answer === 'sub' ? 'This tool uses a Kraken sub-account of its own.' : 'No other bot or API tool uses this Kraken account.'} Answered ${when(a.at)}. <button class="btn sm" id="achange" type="button">Change</button></p>`;
  }
  // Step 3: the key.
  const v = k && k.verdict;
  const showForm = !k || v === 'withdraw';
  $('keyform').classList.toggle('hidden', !showForm);
  const saved = '<b>Saved in your macOS Keychain. The tool never shows it again.</b>';
  let body = '', badge = '<span class="badge plain">Not set</span>', cls = 'cur', no = '3', acts = '';
  if (!k) body = '';
  else if (!v) { body = `<p class="small" style="margin:8px 0 0">${saved} Saved ${when(k.saved)}. Not tested yet.</p>` + ALLOW; badge = '<span class="badge plain">Not tested</span>'; acts = 'test'; }
  else if (v === 'ok') { body = `<p class="small" style="margin:8px 0 0">${saved} The Keychain item is <span class="mono">Kraken API key (order-chaser)</span>. Saved ${when(k.saved)}. Tested ${when(k.tested)}.</p><p class="tiny muted" style="margin:4px 0 0">No page shows the key again, also not a part of it. To see or delete it, use Keychain Access.</p>` + ALLOW; badge = '<span class="badge ok">Ready</span>'; cls = 'done'; no = '✓'; acts = 'ok'; }
  else if (v === 'missing') { const off = PERMS.filter(p => p[0] === 'need' && k.permissions[p[1]] === false).map(p => p[1]).join(', '); body = `<div class="note bad small" style="margin-top:10px"><b>The key does not have ${OC.esc(off)}.</b> In Kraken Pro, edit the key and turn it on. Then click Test again. You do not paste the key again.</div>`; badge = '<span class="badge bad">Not usable</span>'; cls = 'bad'; no = '!'; acts = 'bad'; }
  else if (v === 'nofunds') { body = '<div class="note bad small" style="margin-top:10px"><b>The key does not have Query Funds, so the tool cannot check that Withdraw Funds is off.</b> Without Query Funds, the Withdraw test fails for every key and proves nothing. The tool does not use the key. In Kraken Pro, edit the key and turn on Query Funds. Then click Test again.</div>'; badge = '<span class="badge bad">Not usable</span>'; cls = 'bad'; no = '!'; acts = 'bad'; }
  else if (v === 'withdraw') { body = '<div class="note bad small" style="margin-top:10px"><b>This key can withdraw funds. The tool refused it and removed it from the Keychain.</b> A key that can withdraw is too risky to keep on this Mac. In Kraken Pro, delete this key. Then create a new key without Withdraw Funds and paste it here.</div>'; badge = '<span class="badge bad">Refused</span>'; cls = 'bad'; no = '!'; }
  else { body = '<div class="note bad small" style="margin-top:10px"><b>The test did not finish.</b> Kraken answered with an error or did not answer. Click Test again.</div>'; badge = '<span class="badge bad">Not tested</span>'; cls = 'bad'; no = '!'; acts = 'bad'; }
  step('K', cls, no, badge);
  $('keybody').innerHTML = body;
  if (!busy) $('perms').innerHTML = k && k.permissions ? perms(k.permissions, v) : perms(null);
  $('keyacts').innerHTML = acts === 'ok' ? '<button class="btn sm" id="test" type="button">Test again</button><button class="btn sm" id="replace" type="button">Replace the key</button><button class="btn sm danger" id="remove" type="button">Remove the key from Keychain</button>'
    : acts ? '<button class="btn primary" id="test" type="button">Test again</button><button class="btn danger" id="remove" type="button">Remove the key from Keychain</button>' : '';
  // Step 4 and the end.
  step('T', v === 'ok' ? 'done' : '', v === 'ok' ? '✓' : '4', v === 'ok' ? '<span class="badge ok">On during each live chase</span>' : '<span class="badge plain">After step 3</span>');
  $('finish').innerHTML = s.ready
    ? '<div class="note ok"><b>Setup complete. Live chases are possible.</b> On the form, choose Live. Each live chase asks you to confirm before the first order goes to Kraken.</div><div class="row" style="margin-top:12px"><a class="btn drybtn" href="/new">Start a dry run</a><a class="btn livebtn" href="/new?mode=live">New live chase</a></div>'
    : '<button class="btn primary" disabled>Live chases are off</button> <span class="small muted">' + s.why.map(OC.esc).join(' ') + '</span>';
  wire();
}

async function answer(v) { const r = await OC.post('/api/setup', { account: v }); if (r.ok) { state = { ...state, ...r.data }; draw(); } }
async function test() {
  busy = true; $('keymsg').textContent = 'Testing: the tool reads the key (macOS can ask: click Allow) and tests each permission with a call that changes nothing. This takes about 5 s.';
  $('perms').innerHTML = perms('testing');
  const r = await OC.post('/api/key/test');
  busy = false;
  $('keymsg').innerHTML = r.ok ? '' : `<span class="tone-bad">${(r.data.errors || []).map(OC.esc).join(' ')}</span>`;
  await load();
}
function wire() {
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  on('ano', () => answer('no')); on('ayes', () => answer('yes')); on('asub', () => answer('sub'));
  on('achange', () => { state.account = null; draw(); });
  on('test', test);
  on('replace', () => { $('keyform').classList.remove('hidden'); $('k1').focus(); });
  on('remove', async () => { const r = await OC.post('/api/key/remove'); $('keymsg').innerHTML = r.ok ? 'Removed from the Keychain.' : `<span class="tone-bad">${(r.data.errors || []).map(OC.esc).join(' ')}</span>`; await load(); });
}
// C6: send the key one time, then clear the fields at once, also when the save fails.
$('save').onclick = async () => {
  const body = { api_key: $('k1').value.trim(), private_key: $('k2').value.trim() };
  $('k1').value = ''; $('k2').value = '';
  const r = await OC.post('/api/key', body);
  if (!r.ok) { $('keymsg').innerHTML = `<span class="tone-bad">${(r.data.errors || []).map(OC.esc).join(' ')}</span>`; return; }
  await test();
};
async function load() { const r = await fetch('/api/setup'); state = await r.json(); draw(); }
load();
OC.stream(() => {});
