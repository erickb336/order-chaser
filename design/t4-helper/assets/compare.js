/* Three ways to show stage-then-touch. A is the recommendation. */
'use strict';
OC.chrome('compare', 'none');
OC.$('sw').remove();
const OPTS = [
  ['A', true, 'Stage opens the chase page', 'Stage goes straight to the chase page. "Waiting for your touch" is the first state of the status card, with the sheet preview and the copy number. After the touch the same card shows Placing, then Resting.',
    ['One page from stage to result: the status card and the log just go on.', 'The log shows the stage, the touch and the order in one list.', 'Fits direction B with no new screen.'], ['A refused order shows on the chase page, not on the form. "Back to the form" keeps your values.'], 'chase.html#staged'],
  ['B', false, 'A separate review page', 'Stage opens a review page: the order, the limits, the sheet preview. The page waits there for the touch, then opens the chase page.',
    ['A calm place to read the order before the touch.'], ['One more page and one more page change in the core loop.', 'The sheet already shows the order: the review page says it twice.'], null],
  ['C', false, 'A dialog on the form', 'Stage opens a dialog over the form, like the old live confirm. The dialog waits for the touch, then the page goes to the chase page.',
    ['You stay on the form if the helper refuses.'], ['A dialog and a macOS sheet at the same time: two dialogs to read.', 'It looks like the old "Start live chase" confirm, but this page can no longer start anything.'], null],
];
document.getElementById('view').innerHTML = '<div style="max-width:1100px"><h1>Stage then touch: three options</h1><p class="muted">After you click Stage, the page waits for your Touch ID on the Mac. Where does that wait show? The prototype builds option A.</p><div class="cmp">' +
  OPTS.map(([k, rec, t, d, pro, con, link]) => `<div class="card"><h2>${k}. ${t} ${rec ? '<span class="rec">Recommended</span>' : ''}</h2><p class="small">${d}</p>
    <h3 class="small">For</h3><ul class="small">${pro.map(x => `<li>${x}</li>`).join('')}</ul><h3 class="small">Against</h3><ul class="small">${con.map(x => `<li>${x}</li>`).join('')}</ul>
    ${link ? `<a href="${link}">See it in the prototype</a>` : '<span class="small muted">Not built: rejected.</span>'}</div>`).join('') + '</div></div>';
