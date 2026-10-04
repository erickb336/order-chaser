// Drive the real pages in Chrome and print what a user sees, as JSON. tests/test_pages.py calls it.
// Usage: node look.mjs '<spec>'. Spec: {"base": "http://127.0.0.1:port", "tabs": n, "steps": [step, ...]}. Each step acts on tab "tab" (default 0):
//   {"goto": "/path"} {"click": sel} {"select": [sel, value]} {"fill": [sel, text]}
//   {"waitFor": "js predicate"} {"text": sel, "as": name} {"eval": "js expression", "as": name}
//   {"shot": path} {"sleep": ms} {"front": true} (the tab gets the focus)
import { chromium } from 'playwright-core';

const spec = JSON.parse(process.argv[2]);
const BASE = spec.base;
const out = {};
const browser = await chromium.launch({ channel: 'chrome', headless: true });
try {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const tabs = [];
  for (let i = 0; i < (spec.tabs || 1); i++) tabs.push(await ctx.newPage());
  for (const s of spec.steps) {
    const p = tabs[s.tab || 0];
    console.error('step ' + JSON.stringify(s));
    if (s.goto) await p.goto(BASE + s.goto);
    else if (s.click) await p.click(s.click);
    else if (s.select) await p.selectOption(s.select[0], s.select[1]);
    else if (s.fill) await p.fill(s.fill[0], s.fill[1]);
    else if (s.waitFor) await p.waitForFunction(s.waitFor, null, { timeout: s.timeout || 10000 });
    else if (s.text) out[s.as] = await p.textContent(s.text);
    else if (s.eval) out[s.as] = await p.evaluate(s.eval);
    else if (s.shot) await p.screenshot({ path: s.shot, fullPage: true });
    else if (s.sleep) await p.waitForTimeout(s.sleep);
    else if (s.front) await p.bringToFront();
  }
} finally {
  await browser.close();
}
console.log('RESULT ' + JSON.stringify(out));
process.exit(0);
