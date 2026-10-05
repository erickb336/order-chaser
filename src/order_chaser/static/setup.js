/* Setup: the account question (Q10). The key lives only in the signed helper (R1): this page has no key field. */
OC.chrome('setup');
const $ = OC.$;
$('host').textContent = location.host;
const WHYSUB = 'Kraken keeps one safety timer for each account. During a live chase this tool sets that timer and renews it every 20 s. That replaces the timer of any other tool on the account. When the timer fires, Kraken cancels the orders of the other tool too.';
const when = t => new Date(t * 1000).toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
let state = null;

function step(id, cls, no, badge) { $('s' + id).className = 'step ' + cls; $('n' + id).textContent = no; $('b' + id).innerHTML = badge; }

function draw() {
  const s = state, a = s.account;
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
  // The end.
  $('finish').innerHTML = s.ready
    ? '<div class="note ok"><b>Setup complete. Live orders can be staged.</b> On the form, choose Live. The tool stages the order and places nothing; the signed helper (coming) places it after a Touch ID.</div><div class="row" style="margin-top:12px"><a class="btn drybtn" href="/new">Start a dry run</a><a class="btn livebtn" href="/new?mode=live">Stage a live order</a></div>'
    : '<button class="btn primary" disabled>Live is off</button> <span class="small muted">' + s.why.map(OC.esc).join(' ') + '</span>';
  wire();
}

async function answer(v) { const r = await OC.post('/api/setup', { account: v }); if (r.ok) { state = { ...state, ...r.data }; draw(); } }
function wire() {
  const on = (id, fn) => { const b = $(id); if (b) b.onclick = fn; };
  on('ano', () => answer('no')); on('ayes', () => answer('yes')); on('asub', () => answer('sub'));
  on('achange', () => { state.account = null; draw(); });
}
async function load() { const r = await fetch('/api/setup'); state = await r.json(); draw(); }
load();
OC.stream(() => {});
