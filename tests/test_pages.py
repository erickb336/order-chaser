"""Page tests: the real app on a free port of 127.0.0.1 with a FAKE public feed (sample prices), seen in Chrome.

They need node, `npm install` in tests/pages (playwright-core, pinned) and Google Chrome; else they skip.
Set OC_SHOTS to a folder to keep the screenshots.
"""
import asyncio
import datetime
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zlib
from decimal import Decimal as D
from pathlib import Path

import pytest
import uvicorn

from order_chaser import core, feed
from order_chaser.server import create_app

PAGES = Path(__file__).parent / "pages"
CHROME = Path("/Applications/Google Chrome.app")
MARGIN = {"margin_call": 80, "margin_stop": 40, "long_position_limit": 350, "short_position_limit": 250}
PAIRS = feed.parse_pairs({
    "BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5", "pair_decimals": 1, "lot_decimals": 8, "status": "online",
                "leverage_buy": list(range(2, 11)), "leverage_sell": list(range(2, 11)), **MARGIN},
    "ETH/USD": {"tick_size": "0.01", "ordermin": "0.002", "costmin": "0.5", "pair_decimals": 2, "lot_decimals": 8, "status": "online",
                "leverage_buy": [2, 3], "leverage_sell": [], **MARGIN}})
SNAPSHOTS = {"BTC/USD": ([("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.5", "3.0")]),
             "ETH/USD": ([("2500.10", "5")], [("2500.20", "5")])}

pytestmark = pytest.mark.skipif(not (shutil.which("node") and (PAGES / "node_modules" / "playwright-core").exists()
                                     and CHROME.exists()),
                                reason="page tests need node, npm install in tests/pages, and Google Chrome")


class Clock:
    """The tool's clock: it stands still until a test moves it."""
    def __init__(self):
        self.t = float(int(time.time()))

    def __call__(self):
        return self.t


class FakeBook:
    def __init__(self, symbol):
        self.symbol, self.pd = symbol, PAIRS[symbol].price_decimals
        self.bids, self.asks = {}, {}

    def msg(self, kind, bids, asks):
        if kind == "snapshot":
            self.bids, self.asks = {}, {}
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for p, q in levels:
                side.pop(D(p), None) if D(q) == 0 else side.__setitem__(D(p), D(q))
        f = lambda v, d: f"{D(v):.{d}f}".replace(".", "").lstrip("0")
        s = "".join(f(p, self.pd) + f(q, 8) for p, q in sorted(self.asks.items())[:10])
        s += "".join(f(p, self.pd) + f(q, 8) for p, q in sorted(self.bids.items(), reverse=True)[:10])
        return {"channel": "book", "type": kind, "data": [{"symbol": self.symbol, "checksum": zlib.crc32(s.encode()),
                "bids": [{"price": D(p), "qty": D(q)} for p, q in bids], "asks": [{"price": D(p), "qty": D(q)} for p, q in asks]}]}


class FakeWs:
    """The public feed: it answers a book subscription with a snapshot of sample prices."""
    def __init__(self, tool):
        self.tool = tool

    async def send(self, msg):
        m = json.loads(msg)
        if m["method"] == "subscribe" and m["params"]["channel"] == "book":
            symbol = m["params"]["symbol"][0]
            self.tool.books[symbol] = FakeBook(symbol)
            asyncio.get_running_loop().create_task(self.tool.feed._handle(self.tool.books[symbol].msg("snapshot", *SNAPSHOTS[symbol])))


class Tool:
    def __init__(self, data_dir, shots, **app):
        self.clock, self.shots, self.books = Clock(), shots, {}
        with socket.socket() as s:       # a free port: the app on 5180, or another agent, does not block the tests
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        guard = create_app(data_dir, connect=False, clock=self.clock, latency=0, port=self.port, **app)
        self.eng = guard.app.state.engine
        self.server = uvicorn.Server(uvicorn.Config(guard, host="127.0.0.1", port=self.port, log_level="warning"))
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(self.server.serve(),), daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.05)
        self.feed = feed.PublicFeed(self.eng.on_book, self.eng.on_trade, self.eng.on_link)
        self.feed.ws = FakeWs(self)
        self.eng.feed, self.eng.pairs = self.feed, PAIRS
        self.call(self.feed.watch, PAIRS["BTC/USD"])
        self.call(self.eng.on_link, True, 0, 0)

    def call(self, fn, *args):
        """Run fn on the server's event loop, as the feed and the routes do."""
        async def run():
            r = fn(*args)
            return await r if asyncio.iscoroutine(r) else r
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(10)

    def book(self, symbol, bids, asks):
        self.call(self.feed._handle, self.books[symbol].msg("update", bids, asks))

    def beat(self, seconds=0):
        self.clock.t += seconds
        self.call(self.feed._handle, self.books[self.eng.watched].msg("update", [], []))   # a book message: the book is fresh

    def trade(self, side, price, qty, symbol="BTC/USD"):
        self.call(self.feed._handle, {"channel": "trade", "type": "update",
                                      "data": [{"symbol": symbol, "side": side, "price": D(price), "qty": D(qty)}]})

    def start(self, side="buy", qty="0.05", timeout=120, leverage=None, symbol="BTC/USD"):
        """side: buy or sell; or a margin choice: long, short, close-long, close-short."""
        assert self.call(self.eng.start, symbol, side, D(qty), None, timeout, leverage) == []
        return self.eng.chase.id

    def timeout(self, seconds):
        self.beat(seconds)
        self.call(self.eng.tick)

    def look(self, steps, tabs=1, on=None):
        """Run the steps in Chrome; return what the steps read. on: {name: fn}, run at the step {"signal": name}."""
        steps = [{**s, "shot": str(self.shots / s["shot"])} if "shot" in s else s for s in steps]
        env = {**os.environ, "HOME": str(self.shots.parent), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
        with tempfile.TemporaryFile("w+") as err:
            p = subprocess.Popen(["node", str(PAGES / "look.mjs"), json.dumps({"base": f"http://127.0.0.1:{self.port}", "tabs": tabs, "steps": steps})],
                                 cwd=PAGES, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True)
            kill = threading.Timer(120, p.kill)      # the one allowed timer (tests/test_test_rules.py): a hung page ends the test
            kill.start()
            result = []
            try:
                for line in p.stdout:
                    if line.startswith("SIGNAL "):
                        on[line[7:].strip()]()
                        p.stdin.write("go\n")
                        p.stdin.flush()
                    elif line.startswith("RESULT "):
                        result.append(line)
                p.wait()
            finally:
                kill.cancel()
                if p.poll() is None:
                    p.kill()
            err.seek(0)
            log = err.read()
            last = ([l for l in log.splitlines() if l.startswith(("step ", "close"))] or ["no step"])[-1]
            assert result, f"look.mjs {'stopped after 120 s' if p.returncode == -9 else f'exited {p.returncode}'} at: {last}\n{log}"
        out = json.loads(result[0][7:])
        assert out.pop("csp") == []   # the pages run under the full CSP with no refusal
        return out

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def tool(tmp_path):
    shots = Path(os.environ.get("OC_SHOTS", tmp_path / "shots"))
    shots.mkdir(parents=True, exist_ok=True)
    t = Tool(tmp_path, shots)
    yield t
    t.stop()


OC_LOG = "Array.from(document.querySelectorAll('.log li span:last-child')).map(x => x.textContent)"


def card_shown(state):
    return {"waitFor": f"document.querySelector('#statuscard[data-state=\"{state}\"]')"}


# The lowest WCAG contrast of the texts that match sel, against the background they sit on (the first ancestor
# with a background colour; the page is dark), with the opacity of every ancestor. A disabled control is left out,
# unless disabled is true.
CONTRAST_JS = """(sel, disabled) => {
  const rgb = s => s.match(/[\\d.]+/g).map(Number);
  const lum = c => c.slice(0, 3).map(v => v / 255).map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
    .reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
  const back = el => { for (let e = el; e; e = e.parentElement) { const c = rgb(getComputedStyle(e).backgroundColor); if ((c[3] ?? 1) > 0.5) return c; } return [255, 255, 255]; };
  const one = el => {
    let o = 1; for (let e = el; e; e = e.parentElement) o *= Number(getComputedStyle(e).opacity);
    const f = rgb(getComputedStyle(el).color), b = back(el), a = (f[3] ?? 1) * o;
    const mix = [0, 1, 2].map(i => f[i] * a + b[i] * (1 - a));
    const [l1, l2] = [lum(mix), lum(b)].sort((x, y) => y - x);
    return { c: Math.round((l1 + 0.05) / (l2 + 0.05) * 100) / 100, t: el.textContent.trim().slice(0, 40) };
  };
  const els = Array.from(document.querySelectorAll(sel)).filter(el => el.offsetParent && el.textContent.trim() && (disabled || !el.closest('[disabled]')));
  return els.map(one).sort((x, y) => x.c - y.c)[0] || { c: 99, t: '' };
}"""


def contrast(sel, disabled=False):
    return {"eval": f"({CONTRAST_JS})({json.dumps(sel)}, {json.dumps(disabled)}).c", "as": "contrast"}


# Every visible text on the page: its lowest contrast and that text (to find it).
ALL_TEXT = {"eval": f"({CONTRAST_JS})('body *:not(script):not(style)')", "as": "all"}


# The space from the lowest rail label to the note under the rail, in px: below 0 they overlap.
NOTE_GAP = {"eval": "Math.round(OC.$('railnote').getBoundingClientRect().top - Math.max(...Array.from("
                    "document.querySelectorAll('#statuscard .rail .mk')).map(e => e.getBoundingClientRect().bottom)))", "as": "gap"}
RAIL_TEXT = "#statuscard .rail .val, #statuscard .rail .lab"


def clock_text(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


# ---------- the findings ----------

def test_page_a_finished_chase_shows_its_own_end_prices_not_another_pair(tool):
    # DONE-CHASE-SHOWS-OTHER-PAIR-PRICES: the chase ends on BTC/USD, then a tab watches ETH/USD.
    tool.start()
    tool.call(tool.eng.user, "stop")
    async def watch_eth():
        tool.eng.watched = "ETH/USD"
        await tool.feed.watch(PAIRS["ETH/USD"])
    tool.call(watch_eth)
    got = tool.look([{"goto": "/chase"}, card_shown("stopped"),
                     {"text": "#statuscard .mk.bid .val", "as": "bid"}, {"text": "#statuscard .mk.ask .val", "as": "ask"},
                     {"text": "#railnote", "as": "note"}, NOTE_GAP, {"shot": "done-chase-own-prices.png"}])
    assert (got["bid"], got["ask"]) == ("62,417.90", "62,418.50")
    assert got["gap"] >= 0                                # UX3-END-NOTE-OVERLAP: the note is below the "best bid" label
    assert got["note"] == f"Prices at the end of the chase, {clock_text(tool.eng.chase.book_at)}."


def test_page_a_tab_asks_again_for_its_pair_when_another_tab_changed_it(tool):
    # TWO-TABS-PRICES-STUCK
    # Headless Chrome gives every tab the focus; a real browser gives it to one. The steps set it as a user would.
    focus = lambda on: {"eval": f"document.hasFocus = () => {'true' if on else 'false'}; window.dispatchEvent(new Event('focus'))"}
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('bid').textContent === '62,417.90'"}, focus(False),
                     {"tab": 1, "goto": "/new"}, {"tab": 1, "select": ["#pairsel", "ETH/USD"]},
                     {"tab": 1, "waitFor": "OC.$('bid').textContent === '2,500.10'"}, {"tab": 1, **focus(False)},
                     {"waitFor": "OC.$('bid').textContent === '…'"},           # the first tab lost its prices
                     focus(True),                                             # the user goes back to the first tab
                     {"waitFor": "OC.$('bid').textContent === '62,417.90' && OC.$('pnote').textContent.startsWith('Spread')",
                      "timeout": 10000},
                     {"text": "#priceshead", "as": "head"}, {"shot": "two-tabs-first-tab.png"}], tabs=2)
    assert got["head"] == "Prices now: BTC/USD"


def test_page_disconnected_shows_the_age_of_the_values_and_dashes_the_rail_with_readable_prices(tool):
    # UX-STALE-PRICES-NO-AGE, UX3-STALE-RAIL-CONTRAST
    tool.start()
    tool.call(tool.eng.on_link, False, 2, 4)
    tool.clock.t += 9
    got = tool.look([{"goto": "/chase"}, card_shown("disconnected"),
                     {"text": "#statuscard .state", "as": "state"},
                     {"eval": "document.querySelector('#statuscard .rail').classList.contains('stale')", "as": "dim"},
                     contrast(RAIL_TEXT), {"shot": "disconnected-age.png"}])
    assert got.pop("contrast") >= 4.5
    assert got == {"state": "Disconnected · values from 9 s ago", "dim": True}


def test_page_a_feed_lost_end_shows_the_age_and_dashes_the_rail_with_readable_prices(tool):
    # UX-STALE-PRICES-NO-AGE, after a feed-lost end
    tool.start(timeout=30)
    tool.clock.t += 5
    tool.call(tool.eng.on_link, False, 2, 4)
    tool.clock.t += 25
    tool.call(tool.eng.tick)
    assert (tool.eng.chase.outcome, tool.eng.chase.end_ask) == ("notfilled", None)
    got = tool.look([{"goto": "/chase"}, card_shown("notfilled"), {"text": "#railnote", "as": "note"},
                     {"eval": "document.querySelector('#statuscard .rail').classList.contains('stale')", "as": "dim"},
                     {"text": "#statuscard .state", "as": "state"}, contrast(RAIL_TEXT), NOTE_GAP, {"shot": "feed-lost-end.png"}])
    assert got.pop("contrast") >= 4.5 and got.pop("gap") >= 0
    assert got == {"note": "Prices: values from 30 s before the end. There was no valid price at the end.", "dim": True,
                   "state": "Nothing filled · values from 30 s before the end"}


def test_page_a_dry_run_cancel_failed_says_there_is_nothing_to_check_in_kraken_pro(tool):
    # UX-CANCELFAIL-DRY-TODO
    send = tool.eng.gw.answer
    def refuse_cancel(cmd, now):
        if isinstance(cmd, core.Cancel):
            return [core.Rejected(now, "cancel", "EGeneral:Internal error")]
        if isinstance(cmd, core.Query):
            o = tool.eng.gw.order
            return [core.OrderState(now, True, o["cum"], o["price"])]
        return send(cmd, now)
    tool.eng.gw.answer = refuse_cancel
    cid = tool.start()
    tool.call(tool.eng.user, "stop")
    assert tool.eng.chase.outcome == "cancelfail"
    todo = "Array.from(document.querySelectorAll('.note.info li')).map(x => x.textContent)"
    got = tool.look([{"goto": "/chase"}, card_shown("cancelfail"), {"eval": todo, "as": "chase"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"eval": todo, "as": "result"}, {"shot": "cancelfail-dry-result.png"},
                     {"goto": "/history"}, {"waitFor": "document.querySelector('tr.click')"},
                     {"text": "tr.click td:last-child .badge", "as": "history"}])
    dry = ["Nothing to check in Kraken Pro: a dry run sends no orders."]
    assert (got["chase"], got["result"], got["history"]) == (dry, dry, "Cancel failed")


def test_page_not_filled_and_part_filled_follow_the_filled_quantity(tool):
    # UX-HISTORY-PARTFILLED-ZERO, UX-REST-NOTHING-FILLED, UX-RAIL-LABEL-OVERLAP
    part = tool.start(timeout=30)
    tool.trade("sell", "62417.0", "0.01")                                   # 0.0100 fills
    tool.book("BTC/USD", [], [("62418.5", "0"), ("62419.5", "0"), ("62431.0", "3")])
    tool.timeout(30)                                                         # the IOC at 62,418.50 gets nothing
    assert (tool.eng.chase.outcome, tool.eng.chase.filled) == ("notfilled", D("0.01"))
    none = tool.start(timeout=30)                                            # cap: the ask now, 62,431.00
    tool.book("BTC/USD", [("62439.9", "1")], [("62431.0", "0"), ("62440.0", "3")])
    tool.timeout(30)                                                         # bid and ask close, far above the cap
    assert (tool.eng.chase.outcome, tool.eng.chase.filled) == ("notfilled", D("0"))
    rects = ("(() => { const r = s => { const g = document.createRange(); g.selectNodeContents(document.querySelector('#statuscard .mk.' + s + ' .val')); return g.getBoundingClientRect(); };"
             " return Math.round(r('ask').left - r('bid').right); })()")
    got = tool.look([
        {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 2"},
        {"eval": "Array.from(document.querySelectorAll('tr.click td:last-child .badge')).map(x => x.textContent)", "as": "history"},
        {"shot": "history-not-filled.png"},
        {"goto": "/chase"}, card_shown("notfilled"),
        {"text": "#statuscard .state", "as": "state"}, {"text": "#statuscard .head", "as": "head"},
        {"text": "#actions a[href^='/new?rest']", "as": "again"}, {"eval": rects, "as": "gap"},
        {"shot": "rail-at-end.png"},
        {"goto": f"/result?id={none}"}, {"waitFor": "document.querySelector('.facts')"},
        {"text": "#out .badge", "as": "none_badge"}, {"text": "a[href^='/new?rest']", "as": "none_again"},
        {"goto": f"/result?id={part}"}, {"waitFor": "document.querySelector('.facts')"},
        {"text": "#out .badge", "as": "part_badge"}, {"text": "a[href^='/new?rest']", "as": "part_again"},
        {"goto": f"/new?rest={none}"}, {"waitFor": "OC.$('restnote').textContent !== ''"},
        {"text": "#restnote", "as": "restnote"}])
    assert got["history"] == ["Not filled: above cap", "Part filled: above cap"]
    assert (got["state"], got["head"], got["again"]) == ("Nothing filled (above cap)", "Stopped: nothing filled",
                                                         "Chase again (0.0500 BTC), new cap")
    assert got["gap"] >= 8                               # the bid and ask labels do not touch
    assert (got["none_badge"], got["none_again"]) == ("Not filled", "Chase again (0.0500 BTC)")
    assert (got["part_badge"], got["part_again"]) == ("Part filled", "Chase the rest (0.0400 BTC)")
    assert got["restnote"] == ("You chase your last order again: 0.0500 BTC. Nothing filled. "
                               "The cap is the ask at the new start, not the old cap.")


def test_page_the_title_says_resting_at_the_cap_when_the_order_waits_there(tool):
    # TITLE-RESTING-AT-CAP
    tool.start()
    tool.book("BTC/USD", [("62420.0", "1")], [("62418.5", "0"), ("62419.5", "0"), ("62421.0", "3")])
    tool.beat(6)                                          # the amend to the cap goes out and lands
    assert tool.eng.chase.price == tool.eng.chase.limit == D("62418.5")
    got = tool.look([{"goto": "/chase"}, card_shown("resting"), {"text": "#statuscard .head", "as": "head"},
                     {"shot": "resting-at-cap.png"}])
    assert got["head"] == "Resting at the cap"


def test_page_a_pair_change_clears_the_limit(tool):
    # UX-OVERRIDE-PAIR-CARRY
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"}, {"click": "#ovbtn"},
                     {"eval": "OC.$('ovp').value", "as": "before"}, {"select": ["#pairsel", "ETH/USD"]},
                     {"waitFor": "OC.$('ask').textContent === '2,500.20'"},
                     {"eval": "OC.$('ovp').value", "as": "after"}, {"shot": "override-pair-change.png"}])
    assert (got["before"], got["after"]) == ("62418.5", "")



def test_page_a_rest_below_the_minimum_offers_no_chase_of_the_rest_and_history_shows_the_fill_word(tool):
    # UX3-CHASE-REST-BELOW-MIN, HIST-ZERO-STOP-BADGE
    none = tool.start()
    tool.call(tool.eng.user, "stop")                                         # nothing filled
    tool.beat(1)                                                             # history: newest first
    tiny = tool.start()
    tool.trade("sell", "62417.0", "0.04996")                                 # the rest, 0.00004 BTC, is below 0.00005
    tool.call(tool.eng.user, "stop")
    assert (tool.eng.chase.outcome, tool.eng.chase.qty - tool.eng.chase.filled) == ("stopped", D("0.00004"))
    rest_links = "document.querySelectorAll(\"a[href^='/new?rest']\").length"
    got = tool.look([
        {"goto": "/chase"}, card_shown("stopped"), {"eval": rest_links, "as": "chase_links"},
        {"text": "#restmin", "as": "chase_text"}, {"shot": "rest-below-min-chase.png"},
        {"goto": f"/result?id={tiny}"}, {"waitFor": "document.querySelector('.facts')"},
        {"eval": rest_links, "as": "result_links"}, {"text": "#restmin", "as": "result_text"}, {"shot": "rest-below-min-result.png"},
        {"goto": f"/result?id={none}"}, {"waitFor": "document.querySelector('.facts')"},
        {"text": "a[href^='/new?rest']", "as": "none_again"},
        {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 2"},
        {"eval": "Array.from(document.querySelectorAll('tr.click td:last-child .badge')).map(x => x.textContent)", "as": "history"},
        {"shot": "history-stopped.png"}])
    text = "The rest, 0.00004 BTC, is below the Kraken minimum (0.00005 BTC or 0.5 USD). You cannot chase it."
    assert (got["chase_links"], got["chase_text"], got["result_links"], got["result_text"]) == (0, text, 0, text)
    assert got["none_again"] == "Chase again (0.0500 BTC)"                   # a rest above the minimum keeps the button
    assert got["history"] == ["Stopped by you · Part filled", "Stopped by you · Not filled"]


def test_page_connecting_text_is_readable(tool):
    # UX3-PRICES-LOADING-CONTRAST: no valid book for more than 10 s, the link is up: "Connecting…"
    tool.clock.t += 11
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('pnote').textContent.startsWith('Connecting')"},
                     contrast("#prices .tiny, #pnote, #bid, #ask"), {"shot": "connecting.png"}])
    assert got["contrast"] >= 4.5


# ---------- margin (simulated account) ----------

def opened_long(tool, qty="0.05", lev=3):
    """A BTC/USD long in the simulated account, opened by a margin chase that filled at 62,417.90."""
    cid = tool.start("long", qty, leverage=lev)
    tool.trade("sell", "62417.0", qty)
    assert tool.eng.chase.outcome == "filled"
    return cid


TEXTS = lambda sel: {"eval": f"Array.from(document.querySelectorAll({json.dumps(sel)})).filter(e => e.offsetParent).map(e => e.textContent.trim())", "as": sel}


def test_page_the_form_offers_five_choices_and_an_open_shows_leverage_cost_collateral_and_the_gauge(tool):
    got = tool.look([
        {"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
        TEXTS("#what button"), {**ALL_TEXT, "as": "spot"}, {"shot": "m-new-spot-dark.png"},
        {"click": "#what button[data-w=long]"}, {"waitFor": "!OC.$('mgbox').classList.contains('hidden') && OC.$('mgbox').querySelector('.gauge')"},
        TEXTS("#lev button:not([disabled])"), {"eval": "OC.$('lev').querySelector('[aria-pressed=true]').textContent", "as": "lev"},
        TEXTS("#mgbox table td:first-child"), {"eval": "OC.$('mgbox').querySelector('.gauge').getAttribute('aria-label')", "as": "gauge"},
        {"text": "#lvlline", "as": "line"}, {"text": "#worst", "as": "worst"}, {"text": "#start", "as": "start"},
        {"text": "#amthelp", "as": "free"}, {"text": "#mgbox label", "as": "mglabel"}, {"text": "#mgbox p.tiny", "as": "fees"},
        ALL_TEXT, {"shot": "m-new-open-long.png"},
        {"select": ["#pairsel", "ETH/USD"]}, {"waitFor": "OC.$('ask').textContent === '2,500.20'"},
        TEXTS("#lev button:not([disabled])"), {"click": "#what button[data-w=short]"},
        {"waitFor": "OC.$('levhelp').textContent.startsWith('Kraken allows no short')"}, {"text": "#mnote", "as": "noshort"},
        {"eval": "OC.$('start').disabled", "as": "blocked"}, {"shot": "m-new-noshort.png"}])
    assert got["#what button"] == ["Buy", "Sell", "Open long", "Open short", "Close a position"]
    low = [got.pop("spot"), got.pop("all")]
    assert min(x["c"] for x in low) >= 4.5, low
    assert got["#lev button:not([disabled])"] == ["2x", "3x"]          # ETH/USD: AssetPairs leverage_buy [2, 3]
    assert got["lev"] == "2x"                                         # the form starts at 2x
    assert got["#mgbox table td:first-child"] == ["Position cost", "Collateral it uses", "Opening fee", "Rollover, every 4 h"]
    assert got["gauge"] == "Margin level now no position, after 320%. Margin call at 80%, liquidation at 40%."
    assert got["line"] == ("Now no position. After this open: 320%. The tool does not stop an open for its margin level. "
                           "It shows the level so that you decide.")
    assert got["worst"].startswith("The open never costs more than 3,147.45 USD in price and fees: 0.0500 × 62,418.50 (cap) + 24.97 taker fee (0.80%) + 1.56 opening fee")
    assert got["start"] == "Start dry run: open long"
    assert got["free"].startswith("Free margin: 5,000.00 USD (simulated account, read at ")
    # MARGIN-NUMBER-LABELS, MARGIN-RATES-NO-API (PE): the numbers are estimates, and the page says why.
    assert got["mglabel"] == "Margin: what this open uses and costs estimate"
    assert got["fees"] == ("Kraken US margin fees are 0.01% to 0.05% of the position cost. Kraken can change them without notice, "
                           "and no API gives them, so the tool counts the stated maximum, 0.05%.")
    assert got["noshort"].startswith("Kraken allows no short on ETH/USD.") and got["blocked"] is True


def test_page_close_lists_the_simulated_positions_with_the_whole_size_reduce_only_and_the_level_after(tool):
    opened_long(tool)
    whole = "OC.$('amt').value === '0.0500'"
    got = tool.look([
        {"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
        {"click": "#what button[data-w=close]"}, {"waitFor": "document.querySelector('#poslist label') && " + whole},
        TEXTS("#poslist label b"), {"text": "#est", "as": "est"}, {"text": "#amthelp", "as": "help"},
        {"waitFor": "OC.$('mgbox').querySelector('.gauge')"},          # the level after comes with the close plan
        {"eval": "OC.$('mgbox').querySelector('.gauge').getAttribute('aria-label')", "as": "gauge"},
        {"text": "#start", "as": "start"}, {"text": "#worst", "as": "worst"}, ALL_TEXT, {"shot": "m-new-close.png"},
        {"fill": ["#amt", "0.04999"]}, {"waitFor": "!OC.$('mnote').classList.contains('hidden')"},
        {"text": "#mnote", "as": "rest"}, {"eval": "OC.$('start').disabled", "as": "rest_blocked"}, {"shot": "m-new-close-remainder.png"},
        {"fill": ["#amt", "0.06"]}, {"waitFor": "OC.$('amterr').textContent"},
        {"text": "#amterr", "as": "over"}, {"eval": "OC.$('start').disabled", "as": "over_blocked"},
        {"click": "#whole"}, {"waitFor": whole}, {"text": "#est", "as": "after_all"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["#poslist label b"] == ["BTC/USD Long 0.0500 BTC"]
    assert got["est"] == "Whole position" and got["after_all"] == "Whole position"
    assert got["help"].startswith("Reduce-only. The order can only make this long smaller. It can never open a short")
    assert got["gauge"].startswith("Margin level now ") and ", after the close no position." in got["gauge"]
    assert got["start"] == "Start dry run: close"
    assert got["worst"].startswith("The close never brings less than 3,095.93 USD: 0.0500 × 62,417.90 (floor) − 24.97 taker fee (0.80%).")
    assert got["rest"].startswith("This close leaves 0.00001 BTC open. That is below the Kraken minimum") and got["rest_blocked"] is False
    assert got["over"] == ("A close cannot be larger than the position, 0.0500 BTC. Reduce-only orders never grow or flip a position. "
                           "Enter 0.0500 or less.") and got["over_blocked"] is True


def test_page_cancel_and_replace_shows_its_steps_and_its_costs(tool):
    tool.eng.gw.refuse_margin_amends = True
    send = tool.eng.gw.answer
    held = []
    tool.eng.gw.answer = lambda cmd, now: held.append(cmd) or [] if isinstance(cmd, core.Cancel) else send(cmd, now)
    tool.start("long", leverage=3)
    tool.trade("sell", "62417.0", "0.018")
    tool.clock.t += 6
    tool.book("BTC/USD", [("62418.3", "1")], [])                    # the bid rises: the amend is refused, the cancel goes out
    assert tool.eng.chase.phase == "cancelling" and len(held) == 1
    steps = "Array.from(document.querySelectorAll('#statuscard li')).map(x => x.textContent)"
    got = tool.look([{"goto": "/chase"}, card_shown("replacing"), {"text": "#statuscard .head", "as": "head"},
                     {"eval": steps, "as": "steps"}, {"text": "#cardnote", "as": "cost"}, {"text": "#ratetext", "as": "rate"},
                     {"text": "#order", "as": "order"}, {"text": "#ro", "as": "ro"}, ALL_TEXT, {"shot": "m-chase-replace.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["head"] == "Amend refused: the tool cancels and replaces"
    assert got["steps"] == ["✓ Cancel the order.", "→ Wait for the simulated exchange to confirm the cancel.",
                            "Read the fills again: 0.0180 BTC filled.", "Place a new post-only buy for the rest, 0.0320 BTC at 62,418.30, leverage 3x."]
    assert got["cost"].startswith("What a replace costs: each move is a cancel and a new order, not one amend. A cancel adds up to 8")
    assert (got["rate"], got["order"], got["ro"]) == ("Cancel and replace: a cancel adds up to 8", "Open long 0.0500 BTC, 3x", "No: this order opens")
    tool.eng.gw.answer = send
    tool.call(lambda: send(held[0], tool.clock.t) and tool.eng.handle(core.Canceled(tool.clock.t)))
    assert tool.eng.chase.legs[-1].endswith("-1") and tool.eng.chase.phase == "resting"
    got = tool.look([{"goto": "/chase"}, card_shown("partial"), {"text": "#statuscard .sub", "as": "sub"}, {"shot": "m-chase-after-replace.png"}])
    assert "The simulated exchange refuses amends of this order, so each move is a cancel and a new order (1 so far)." in got["sub"]


def test_page_a_liquidation_ends_the_chase_on_the_chase_result_and_history_pages(tool):
    tool.eng.gw.account.cash = D("700")                             # a small simulated account
    opened_long(tool, lev=5)
    tool.beat(1)
    cid = tool.start("close-long")
    tool.book("BTC/USD", [("62417.9", "0"), ("52000.0", "1")], [("62418.5", "0"), ("62419.5", "0"), ("52000.6", "3")])
    assert tool.eng.chase.outcome == "liquidated"
    got = tool.look([
        {"goto": "/chase"}, card_shown("liquidated"), {"text": "#statuscard .head", "as": "head"}, {"text": "#pstat", "as": "pstat"},
        ALL_TEXT, {"shot": "m-chase-liquidated.png"},
        {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"text": "#out .badge", "as": "badge"},
        {**ALL_TEXT, "as": "result_all"}, {"shot": "m-result-liquidated.png"},
        {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 2"}, {"click": "#filt button[data-f=spot]"},
        {"eval": "document.querySelectorAll('tr.click:not(.hidden)').length", "as": "spot_rows"}, {"click": "#filt button[data-f=margin]"},
        TEXTS("tr.click td:nth-child(3)"), TEXTS("tr.click td:last-child .badge"), {**ALL_TEXT, "as": "history_all"},
        {"shot": "m-history.png"}, {"goto": "/setup"}, {"text": "#mgline", "as": "setup"}, {**ALL_TEXT, "as": "setup_all"}])
    assert min(got.pop(k)["c"] for k in ("all", "result_all", "history_all", "setup_all")) >= 4.5
    assert got["head"] == "Ended: the simulated exchange liquidated the position" and got["pstat"] == "Liquidated"
    assert got["badge"] == "Liquidated" and got["spot_rows"] == 0
    assert got["tr.click td:nth-child(3)"] == ["Margin Close long", "Margin Open long · 5x"]
    assert got["tr.click td:last-child .badge"] == ["Liquidated", "Position opened"]
    assert got["setup"].startswith("margin Margin uses the same key and needs no new permission.")


# ---------- repair round 1 of the margin build ----------

def watch(tool, symbol):
    async def run():
        tool.eng.watched = symbol
        await tool.feed.watch(PAIRS[symbol])
    tool.call(run)


def test_page_the_close_list_shows_one_row_for_each_pair_and_direction_with_the_count_the_average_leverage_and_each_mark(tool):
    # Mixed leverage (owner's decision) and UNWATCHED-PAIR-MARK-STALE
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    opened_long(tool, "0.02", 5)                     # the same cost at 5x: 3.75 on average, "average 4x"
    tool.beat(1)
    watch(tool, "ETH/USD")
    tool.start("long", "1", leverage=3, symbol="ETH/USD")
    tool.trade("sell", "2500.00", "1", "ETH/USD")
    assert tool.eng.chase.outcome == "filled"
    tool.beat(61)                                    # a heartbeat renews the ETH book: the last ETH price seen
    eth_at = tool.clock.t
    watch(tool, "BTC/USD")
    tool.beat(1)
    tool.eng.account = None                          # the form reads the account when it opens
    rows = "Array.from(document.querySelectorAll('#poslist label')).map(l => [l.querySelector('b').textContent, l.querySelector('.muted.small').textContent, l.querySelector('.r').textContent])"
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"click": "#what button[data-w=close]"}, {"waitFor": "document.querySelectorAll('#poslist label').length === 2"},
                     {"click": "#poslist label[data-k='BTC/USD|long']"}, {"waitFor": "OC.$('marknote')"},
                     {"eval": rows, "as": "rows"}, {"text": "#marknote", "as": "marks"}, {"text": "#poslist > div:last-child", "as": "foot"},
                     ALL_TEXT, {"shot": "r1-close-list-two-positions.png"}])
    assert got.pop("all")["c"] >= 4.5
    btc, eth = got["rows"]
    assert btc[0] == "BTC/USD Long 0.0400 BTC" and btc[1].startswith("· 2 positions · average 4x · average entry 62,417.90 · oldest opened ")
    assert btc[2].endswith("profit or loss now, at the mark (estimate)")
    assert eth[0] == "ETH/USD Long 1.0000 ETH" and eth[1].startswith("· 3x · opened ")
    assert eth[2].endswith(f"at the last price seen, {clock_text(eth_at)} (estimate)")
    assert got["marks"] == f"The level uses ETH/USD at the last price seen, {clock_text(eth_at)}."
    assert got["foot"].endswith("One row for each pair and direction. A close takes the oldest position first.")
    # After a restart the account has no price for a pair until its book arrives: "no price yet", not 0.
    from order_chaser.sim import SimAccount
    tool.eng.gw.account = SimAccount.from_json(tool.eng.gw.account.to_json())
    tool.eng.account = None
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelectorAll('#poslist label').length === 2"},
                     {"waitFor": "OC.$('marknote')"}, {"eval": rows, "as": "rows"}, {"text": "#marknote", "as": "marks"},
                     {"shot": "r1-close-list-after-restart.png"}])
    assert [r[2] for r in got["rows"]] == ["no price yetprofit or loss shows when BTC/USD prices arrive",
                                           "no price yetprofit or loss shows when ETH/USD prices arrive"]
    # UX-MARKNOTE-COPY
    assert got["marks"] == "No price yet for ETH/USD. The level counts its profit or loss as 0."


def test_page_close_with_no_position_shows_nothing_to_close_and_no_figures(tool):
    # UX-CLOSE-NO-POSITION-FIGURES
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"click": "#what button[data-w=close]"}, {"waitFor": "OC.$('poslist').textContent.startsWith('You have no open')"},
                     {"text": "#whatdoes", "as": "does"}, {"text": "#worst", "as": "worst"}, {"text": "#tohelp", "as": "to"},
                     {"eval": "OC.$('start').disabled", "as": "blocked"}, ALL_TEXT, {"shot": "r1-close-no-position.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert (got["does"], got["worst"], got["blocked"]) == ("Nothing to close.", "Nothing to close.", True)
    # CLOSE-EMPTY-FLOOR-COPY: no IOC at a floor without a position
    assert got["to"] == "The timeout counts when a close runs. You have no position to close."


GAUGE_OVERLAP = """(() => {
  const host = document.createElement('div'); host.className = 'card'; host.style.width = '360px'; document.body.append(host);
  const cases = [[42, 39], [41, undefined], [39, 42], [79, 82], [40, 80], [null, 41], [81, 79], [299, 290], [5, 2]];
  const worst = [];
  for (const [now, after] of cases) {
    host.innerHTML = OC.gauge(now, after, 80, 40);
    const boxes = Array.from(host.querySelectorAll('.mk span, .pt')).map(e => [e.textContent.trim(), e.getBoundingClientRect()]);
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
      const [a, ra] = boxes[i], [b, rb] = boxes[j];
      const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w > 0 && h > 0) worst.push([now, after, a, b]);
    }
  }
  host.remove();
  return worst;
})()"""


def test_page_gauge_labels_never_overlap(tool):
    # UX-GAUGE-LABEL-OVERLAP: the case of the review (now 42%, after 39%, liquidation 40%) and others near the marks.
    opened_long(tool, "0.05", 5)
    tool.eng.gw.account.cash = D("262")              # a small account: the level is near 42%
    tool.eng.account = None
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"click": "#what button[data-w=long]"}, {"fill": ["#amt", "0.002"]},
                     {"waitFor": "OC.$('mgbox').querySelector('.gauge')"},
                     {"eval": "OC.$('mgbox').querySelector('.gauge').getAttribute('aria-label')", "as": "aria"},
                     {"eval": GAUGE_OVERLAP, "as": "overlaps"}, {"shot": "r1-gauge-near-40.png"}])
    assert got["aria"].startswith("Margin level now 42%, after 38%.")
    assert got["overlaps"] == []


def test_page_no_order_price_after_a_maker_fill_ended_the_order(tool):
    # UX-PRICE-AFTER-ORDER-GONE, margin and spot
    opened_long(tool, "0.05", 2)
    look = [{"goto": "/chase"}, card_shown("filled"), {"text": "#yp", "as": "yp"},
            {"eval": "document.querySelectorAll('#statuscard .rail .mk.you').length", "as": "mark"},
            {"eval": "OC.$('statuscard').querySelector('.rail .mk.cap .lab').textContent", "as": "cap"}]
    margin = tool.look(look + [{"shot": "r1-maker-fill-no-order.png"}, {"text": "#mgstrip", "as": "strip"}, {"text": "#pstays", "as": "stays"}])
    # POSITION-SURVIVES-CRASH (PE): the page says that the position stays open when the tool stops.
    assert margin.pop("strip") == "MARGIN: this chase opens a 2x long position. A position stays open when the tool stops."
    assert margin.pop("stays") == ("If the tool stops: the simulated position stays in the simulated account. "
                                   "No order and no position is on Kraken.")
    tool.start("buy", "0.01")
    tool.trade("sell", "62417.0", "0.01")
    spot = tool.look(look + [{"shot": "r1-spot-maker-fill-no-order.png"}])
    assert margin == spot == {"yp": "no order", "mark": 0, "cap": "cap (start ask)"}


def test_page_an_amount_error_is_linked_to_the_field_and_said_in_a_live_region(tool):
    # UX-AMOUNT-ERROR-NOT-LINKED, spot and margin
    field = ("[OC.$('amt').getAttribute('aria-invalid'), OC.$('amt').getAttribute('aria-describedby'), "
             "OC.$('amterr').getAttribute('aria-live'), OC.$('amterr').textContent]")
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"eval": field, "as": "ok"}, {"fill": ["#amt", "abc"]}, {"waitFor": "OC.$('amterr').textContent"},
                     {"eval": field, "as": "spot"}, {"shot": "r1-amount-error.png"},
                     {"click": "#what button[data-w=long]"}, {"fill": ["#amt", "1"]},
                     {"waitFor": "OC.$('amterr').textContent.startsWith('This open needs')"}, {"eval": field, "as": "margin"}])
    assert got["ok"] == ["false", "amterr amthelp", "polite", ""]
    assert got["spot"] == ["true", "amterr amthelp", "polite", "Enter an amount in BTC, above 0, with at most 8 decimals."]
    assert got["margin"][:3] == ["true", "amterr amthelp", "polite"] and "free margin for new orders" in got["margin"][3]


# ---------- repair round 2: every screen of a close follows the FIFO close plan ----------

def two_longs(tool):
    """A 2x long of 0.01 at 62,417.90, then a 4x long of 0.02 at 61,000.00; the book is back at 62,000.00 / 62,000.60."""
    opened_long(tool, "0.01", 2)
    t1 = tool.clock.t
    tool.beat(60)
    tool.book("BTC/USD", [("62417.9", "0"), ("61000.0", "1")], [("62418.5", "0"), ("62419.5", "0"), ("61000.6", "3")])
    tool.start("long", "0.02", leverage=4)
    tool.trade("sell", "60999.0", "0.02")
    assert tool.eng.chase.outcome == "filled"
    t2 = tool.clock.t
    tool.beat(60)
    tool.book("BTC/USD", [("61000.0", "0"), ("62000.0", "1")], [("61000.6", "0"), ("62000.6", "3")])
    return clock_text(t1)[:5], clock_text(t2)[:5]


def test_page_a_part_close_says_which_positions_it_takes_and_what_stays_at_their_own_leverage(tool):
    # UX-CLOSE-WHICH-POSITION-UNSAID, UX-CLOSE-REMAINDER-WRONG-LEVERAGE, UX-CLOSE-AFTER-LEVEL-NOT-FIFO, CLOSE-PL-PARTIAL-FIFO
    t1, t2 = two_longs(tool)
    tool.eng.account = None
    acc = tool.eng.gw.account
    plan = f"the 2x position opened {t1} (0.0100 BTC) and 0.0050 BTC of the 4x position opened {t2}"
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "OC.$('ask').textContent === '62,000.60'"},
                     {"fill": ["#amt", "0.015"]}, {"waitFor": "OC.$('plan').textContent.startsWith('Closes')"},
                     {"waitFor": "OC.$('lvlline').textContent.startsWith('Now')"},
                     {"text": "#plan", "as": "plan"}, {"text": "#lvlline", "as": "level"}, ALL_TEXT, {"shot": "r2-close-form-plan.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["plan"] == f"Closes, oldest first: {plan}. Stays open: 0.0150 BTC at 4x."
    # FIFO releases the 2x collateral (312.0895) and a quarter of the 4x collateral (76.25), not half of all at 3x.
    mark = D("62000.3")
    after = (acc.equity(tool.clock.t) - D("0.015") * mark * core.MAKER_FEE) / (acc.used() - D("312.0895") - D("76.25")) * 100
    assert f"After the close: {round(after)}%" in got["level"], (got["level"], after)
    assert round(after) != round(acc.equity(tool.clock.t) / (acc.used() / 2) * 100)
    cash = acc.cash
    cid = tool.start("close-long", "0.015")
    tool.trade("buy", "62001.0", "0.012")                           # a part fill at 62,000.60
    got = tool.look([{"goto": "/chase"}, card_shown("partial"), {"text": "#pstat", "as": "pstat"}, {"text": "#pdet", "as": "pdet"},
                     {"text": "#lev", "as": "lev"}, TEXTS("#log li"), ALL_TEXT, {"shot": "r2-chase-part-fill.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["pstat"] == "Partly closed: 0.0120 of 0.0300 BTC" and got["lev"] == "2x, 4x (2 positions)"
    assert got["pdet"] == (f"Closed so far: the 2x position opened {t1} (0.0100 BTC) and 0.0020 BTC of the 4x position opened {t2}. "
                           "Stays open: 0.0180 BTC at 4x. (simulated)")
    log = got["#log li"]
    assert log[-1].endswith(f"Read the positions: 0.0300 BTC in 2 positions · average 3x · average entry 61,472.63. The close takes "
                            f"the oldest first: {plan}. Stays open: 0.0150 BTC at 4x.")
    assert log[0].endswith("Filled 0.0120 BTC at 62,000.60 (maker). Stays open: 0.0180 BTC at 4x.")
    tool.call(tool.eng.user, "stop")
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#out .badge", "as": "badge"}, {"text": "#out .pstat", "as": "pstat"}, {"text": "#pstart", "as": "start"},
                     {"text": "#plan", "as": "plan"}, {"text": "#pl", "as": "pl"}, TEXTS("tr.plpart"), ALL_TEXT,
                     {"text": "#out h1", "as": "head"}, {"text": "#out .facts .s", "as": "share"}, {"text": "#notclosed", "as": "notclosed"},
                     {"text": "#stays", "as": "stays"}, {"shot": "r2-result-part-close.png"},
                     {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 3"},
                     {"eval": "document.querySelector('tr.click td:last-child .badge').textContent", "as": "history"},
                     {"shot": "g16-history-stopped-part-close.png"}])
    assert got.pop("all")["c"] >= 4.5
    # UX-PLANNED-PART-CLOSE-SHOWN-AS-FAILURE (G16 = A): the order decides; what stays of the position is a neutral fact.
    assert (got["head"], got["share"], got["notclosed"], got["stays"]) == (
        "Closed 0.0120 of 0.0150 BTC (simulated)", "80% of 0.0150", "Not closed: 0.0030 BTC.", "Stays open: 0.0180 BTC at 4x.")
    assert (got["badge"], got["history"], got["pstat"]) == ("Part closed", "Part closed", "Open: 0.0180 BTC at 4x")
    assert got["start"] == "0.0300 BTC in 2 positions · average 3x · average entry 61,472.63"
    assert got["plan"] == f"Closed: the 2x position opened {t1} (0.0100 BTC) and 0.0020 BTC of the 4x position opened {t2}. Stays open: 0.0180 BTC at 4x."
    # (62,000.60 - 62,417.90) x 0.01 + (62,000.60 - 61,000.00) x 0.002 - 2.98 fee = -5.15; the account books the same.
    assert got["pl"] == "−5.15 USD" and round(acc.cash - cash, 2) == D("-5.15")
    assert got["tr.plpart"] == [f"2x opened {t1}: (62,000.60 − 62,417.90) × 0.0100 − fee−6.65", f"4x opened {t2}: (62,000.60 − 61,000.00) × 0.0020 − fee+1.51"]


FOCUS = ("(() => { const e = document.activeElement; return [e.tagName, e.id || e.name || '', "
         "e.type === 'radio' ? e.checked : null]; })()")


def test_page_the_close_list_keeps_the_focus_and_the_selection_while_prices_update(tool):
    # UX-POSLIST-FOCUS-LOST, also the "Close all" button
    two_longs(tool)

    def move(bid, ask, old_bid, old_ask):            # a new mark and a new account read: the close list draws again
        def go():
            tool.book("BTC/USD", [(old_bid, "0"), (bid, "1")], [(old_ask, "0"), (ask, "3")])
            tool.call(tool.eng.read_account, True)
        return go
    pl = "document.querySelector('#poslist label .r').textContent"
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": "OC.$('pairsel').focus()", "as": "_"}, {"press": "Tab"}, {"eval": FOCUS, "as": "radio_before"},
                     {"eval": pl, "as": "pl_before"}, {"shot": "r2-focus-radio-before.png"},
                     {"signal": "down"}, {"waitFor": f"{pl}.startsWith('+12.83')", "timeout": 8000},
                     {"eval": FOCUS, "as": "radio_after"}, {"eval": pl, "as": "pl_after"}, {"shot": "r2-focus-radio-after.png"},
                     {"fill": ["#amt", "0.01"]}, {"waitFor": "OC.$('whole')"}, {"eval": "OC.$('whole').focus()", "as": "_"},
                     {"eval": FOCUS, "as": "whole_before"},
                     {"signal": "up"}, {"waitFor": f"{pl}.startsWith('+15.83')", "timeout": 8000}, {"eval": FOCUS, "as": "whole_after"},
                     {"shot": "r2-focus-close-all-after.png"}],
                    on={"down": move("61900.0", "61900.6", "62000.0", "62000.6"), "up": move("62000.0", "62000.6", "61900.0", "61900.6")})
    assert got["radio_before"] == got["radio_after"] == ["INPUT", "pos", True]
    assert got["pl_before"].startswith("+15.83") and got["pl_after"].startswith("+12.83")   # the row's profit changed under the focus
    assert got["whole_before"] == got["whole_after"] == ["BUTTON", "whole", None]


def test_page_the_close_list_profit_follows_the_mark_of_each_price_with_no_new_account_read(tool):
    # CLOSE-LIST-PL-STALE: the row of the watched pair says "now", so its profit or loss follows each price.
    two_longs(tool)
    reads, read = [], tool.eng.gw.read
    tool.eng.gw.read = lambda now: reads.append(now) or read(now)     # Kraken's TradeBalance and OpenPositions (simulated)
    at_move = []

    def move():                                      # the mid moves from 62,000.30 to 61,900.30
        at_move.append(len(reads))
        tool.book("BTC/USD", [("62000.0", "0"), ("61900.0", "1")], [("62000.6", "0"), ("61900.6", "3")])
    pl = "document.querySelector('#poslist label .r').textContent"
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": pl, "as": "before"}, {"signal": "move"}, {"waitFor": f"{pl}.startsWith('+12.83')", "timeout": 8000}, {"sleep": 500},
                     {"eval": pl, "as": "after"}, {"shot": "r3-close-list-pl-follows-the-mark.png"}], on={"move": move})   # the page read "before" first
    # (62,000.30 - 62,417.90) x 0.01 + (62,000.30 - 61,000.00) x 0.02 = +15.83; at 61,900.30: -5.18 + 18.01 = +12.83
    assert got["before"] == "+15.83 USDprofit or loss now, at the mark (estimate)"
    assert got["after"] == "+12.83 USDprofit or loss now, at the mark (estimate)"
    assert at_move[0] >= 1 and len(reads) == at_move[0]     # the form read the account when it opened; the price made no read


NAMES = ("Array.from(document.querySelectorAll('#poslist input')).map(r => [r.getAttribute('aria-labelledby'), r.getAttribute('aria-describedby')]"
         ".map(ids => ids.split(' ').map(id => OC.$(id).textContent.trim()).join(' ')).join(' | '))")


def test_page_each_close_radio_names_its_row_with_the_size_count_leverage_and_profit(tool):
    # UX-POSLIST-ARIA-LABEL-HIDES-DETAIL
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    opened_long(tool, "0.02", 5)
    tool.eng.account = None
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": NAMES, "as": "names"}, {"eval": "document.querySelector('#poslist input').getAttribute('aria-label')", "as": "label"}])
    name, = got["names"]
    assert got["label"] is None
    assert name.startswith("BTC/USD Long 0.0400 BTC | · 2 positions · average 4x · average entry 62,417.90 · oldest opened ")
    assert "Rollover so far: 0.00 USD (estimate)" in name and name.endswith("USDprofit or loss now, at the mark (estimate)")
    from order_chaser.sim import SimAccount
    tool.eng.gw.account = SimAccount.from_json(tool.eng.gw.account.to_json())   # a restart: no price yet
    tool.eng.account = None
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": NAMES, "as": "names"}])
    assert got["names"][0].endswith("no price yetprofit or loss shows when BTC/USD prices arrive")


def test_page_the_close_list_shows_the_profit_when_the_first_price_of_a_pair_arrives(tool):
    # POSLIST-MARK-NOT-REFRESHED: after a restart the pair has no price; its first book updates the open form.
    opened_long(tool, "0.02", 3)
    from order_chaser.sim import SimAccount
    tool.eng.gw.account = SimAccount.from_json(tool.eng.gw.account.to_json())
    tool.eng.account = None
    pl = "document.querySelector('#poslist label .r').textContent"
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": pl, "as": "before"}, {"shot": "r2-close-list-no-price.png"}])
    assert got["before"].startswith("no price yet")
    # the first book of BTC/USD after the restart, while the form is open and after it read "before"
    got = tool.look([{"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('#poslist input')"},
                     {"eval": pl, "as": "before"}, {"signal": "price"}, {"waitFor": f"!{pl}.startsWith('no price yet')", "timeout": 8000},
                     {"eval": pl, "as": "after"}, {"shot": "r2-close-list-first-price.png"}], on={"price": lambda: tool.beat(1)})
    assert got["before"].startswith("no price yet")
    assert got["after"] == "+0.01 USDprofit or loss now, at the mark (estimate)"      # (62,418.20 - 62,417.90) x 0.02


def test_page_a_close_that_closed_nothing_before_a_restart_says_not_closed_on_its_result(tool):
    # BADGE-KILLED-CLOSE
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    cid = tool.start("close-long", "0.02")
    tool.call(tool.eng.handle, core.Restarted(tool.clock.t + 30, tool.clock.t))
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"text": "#out .badge", "as": "badge"},
                     {"text": "#out .note.info li", "as": "todo"}, {"text": "#out a.btn.primary", "as": "button"},
                     {"text": "#saving", "as": "saving"}, {"eval": "OC.$('saving').className", "as": "tone"}, ALL_TEXT,
                     {"shot": "r2-killed-close-result.png"},
                     {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 2"},
                     {"eval": "document.querySelector('tr.click td:last-child .badge').textContent", "as": "history"},
                     {"shot": "r2-killed-close-history.png"}])
    assert got.pop("all")["c"] >= 4.5
    # UX-RESTART-WHAT-TO-DO-IGNORES-POSITION
    assert got == {"badge": "Ended at a restart", "history": "Ended at a restart",
                   "todo": "Your long is still open: 0.0200 BTC at 3x. Close it from the form.",
                   "button": "Close the position (0.0200 BTC)", "saving": "—", "tone": "v muted"}
    # RESTART-RESULT-CLAIMS-STILL-OPEN: the result reads the account when it loads. After a liquidation it offers no close.
    tool.eng.gw.account.cash = D("100")
    tool.book("BTC/USD", *CRASH)
    assert tool.eng.gw.account.positions == []
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"eval": "!!document.querySelector('#out .note.info')", "as": "todo"}, {"text": "#out a.btn.primary", "as": "button"}])
    assert got == {"todo": False, "button": "New chase"}


def test_page_a_close_that_filled_its_whole_order_says_closed_as_asked_and_what_stays_is_neutral(tool):
    # UX-PLANNED-PART-CLOSE-SHOWN-AS-FAILURE (G16 = A): a planned part close is not a failure.
    two_longs(tool)
    cid = tool.start("close-long", "0.015")
    tool.trade("buy", "62001.0", "0.015")
    assert tool.eng.chase.outcome == "filled"
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#out .badge", "as": "badge"}, {"eval": "document.querySelector('#out .badge').className", "as": "tone"},
                     {"text": "#out h1", "as": "head"}, {"text": "#stays", "as": "stays"}, {"eval": "OC.$('stays').className", "as": "stays_tone"},
                     {"eval": "!!OC.$('notclosed')", "as": "red"}, ALL_TEXT, {"shot": "g16-result-planned-part-close.png"},
                     {"goto": "/history"}, {"waitFor": "document.querySelectorAll('tr.click').length === 3"},
                     {"eval": "document.querySelector('tr.click td:last-child .badge').textContent", "as": "history"},
                     {"shot": "g16-history-planned-part-close.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got == {"badge": "Closed as asked", "tone": "badge ok", "head": "Closed 0.0150 BTC of the long (simulated)",
                   "stays": "Stays open: 0.0150 BTC at 4x.", "stays_tone": "note plain", "red": False, "history": "Closed as asked"}


def test_page_an_open_against_the_other_direction_is_blocked_at_once_at_the_top_with_a_link_to_the_close(tool):
    # UX-OPPOSING-OPEN-BLOCK-LATE
    tool.start("short", "0.01", leverage=3)
    tool.trade("buy", "62419.0", "0.01")
    assert tool.eng.chase.outcome == "filled"
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"click": "#what button[data-w=long]"}, {"waitFor": "!OC.$('oppose').classList.contains('hidden')"},
                     {"text": "#oppose", "as": "block"}, {"eval": "OC.$('oppose').getAttribute('role')", "as": "role"}, {"text": "#startnote", "as": "note"}, {"eval": "OC.$('start').disabled", "as": "blocked"},
                     {"eval": "OC.$('mgbox').classList.contains('hidden')", "as": "no_figures"}, {"text": "#est", "as": "est"},
                     {"text": "#worst", "as": "worst"}, {"eval": "OC.$('oppose').getBoundingClientRect().top < OC.$('what').getBoundingClientRect().top", "as": "top"},
                     ALL_TEXT, {"shot": "opposite-open-block.png"},
                     {"click": "#toclose"}, {"waitFor": "OC.$('amtlabel').textContent === 'Size to close'"},
                     {"eval": "document.querySelector('#what button[aria-pressed=true]').textContent", "as": "what"},
                     {"eval": "document.querySelector('#poslist label.on b').textContent", "as": "pos"}, {"eval": "OC.$('amt').value", "as": "size"},
                     {"eval": "OC.$('oppose').classList.contains('hidden')", "as": "gone"},
                     # CLOSE-LINK-FOCUS-LOST: the focus goes to the chosen position, not to the page
                     {"eval": "document.activeElement === document.querySelector('#poslist label.on input')", "as": "focus"},
                     {"shot": "opposite-open-after-link.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got.pop("focus") is True and got.pop("role") == "alert"   # OPPOSE-BLOCK-NOT-ANNOUNCED
    assert got == {"block": "Close the short first. You have an open short position on BTC/USD: 0.0100 BTC, 3x (simulated account). "
                            "The tool does not open a position against it. Close the short on BTC/USD",
                   "note": "Close the short first.", "blocked": True, "no_figures": True, "est": "", "worst": "Close the short first.",
                   "top": True, "what": "Close a position", "pos": "BTC/USD Short 0.0100 BTC", "size": "0.0100", "gone": True}


def test_page_a_lower_limit_at_the_bid_shows_no_tick_box_and_starts(tool):
    # UX-ZERO-LOSS-ACCEPT-CHECKBOX: "Set a lower limit" fills in the bid; the worst loss is 0.00, so nothing to accept.
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
                     {"click": "#what button[data-w=sell]"}, {"click": "#ovbtn"}, {"waitFor": "OC.$('ovp').value === '62417.9'"},
                     {"text": "#ovwarn", "as": "warn"}, {"eval": "OC.$('ovokrow').classList.contains('hidden')", "as": "no_box"},
                     {"eval": "OC.$('start').disabled", "as": "blocked"}, {"shot": "zero-loss-limit.png"},
                     {"click": "#start"}, {"waitFor": "location.pathname === '/chase'"}])
    assert got == {"warn": "Your limit is the bid now. Worst loss against a market sell now: 0.00 USD.", "no_box": True, "blocked": False}
    assert (tool.eng.chase.side, tool.eng.chase.limit) == ("sell", D("62417.9"))


def test_page_a_disabled_start_keeps_a_readable_label(tool):
    # UX-DISABLED-START-CONTRAST: no opacity fade; the label keeps 4.5:1 in dark mode.
    got = tool.look([{"goto": "/new"}, {"click": "#what button[data-w=close]"},
                     {"waitFor": "OC.$('start').disabled && OC.$('startnote').textContent === 'Nothing to close.'"},
                     contrast("#start", disabled=True), {"eval": "getComputedStyle(OC.$('start')).opacity", "as": "opacity"},
                     {"shot": "disabled-start.png"}])
    assert got["contrast"] >= 4.5 and got["opacity"] == "1", got


def test_page_an_open_short_shows_its_worst_case_as_the_short_value(tool):
    # UX-SHORT-OPEN-WORST-CASE-WORDS-DIFFER: a margin short gives no cash, so no "received".
    # SHORT-WORST-CASE-LESS-FEES: the value already has the fees taken off: "after fees".
    tool.start("short", "0.01", leverage=3)
    got = tool.look([{"goto": "/chase"}, {"waitFor": "OC.$('worst').textContent"}, {"text": "#worsthead", "as": "head"},
                     {"text": "#worst", "as": "worst"}])
    value = core.worst_case("sell", D("0.01"), D("62417.9"), core.Margin(3))
    assert got == {"head": "The short never sells for less than this, after fees:", "worst": f"Short value: {value:,.2f} USD"}


# ---------- repair round 5: liquidation reaches the pages ----------

CRASH = ([("62417.9", "0"), ("52000.0", "1")], [("62418.5", "0"), ("62419.5", "0"), ("52000.6", "3")])   # the mark falls to 52,000.30


def test_page_a_liquidation_with_no_chase_shows_on_the_form_and_the_old_result_offers_no_close(tool):
    # LIQUIDATION-UNSEEN-WITHOUT-A-CHASE
    tool.eng.gw.account.cash = D("700")                             # a small simulated account
    cid = opened_long(tool, lev=5)
    tool.beat(1)
    assert [p["qty"] for p in tool.eng.account["positions"]] == ["0.05"]
    tool.book("BTC/USD", *CRASH)                                    # no chase runs
    assert (tool.eng.account["positions"], tool.eng.account["level"]) == ([], None)
    got = tool.look([{"goto": "/new"}, {"waitFor": "!OC.$('liqnote').classList.contains('hidden')"},
                     {"text": "#liqnote b", "as": "head"}, {"text": "#liqlist", "as": "list"},
                     {"eval": "OC.$('liqnote').getAttribute('role')", "as": "role"},
                     {"click": "#what button[data-w=close]"}, {"waitFor": "OC.$('poslist').textContent.startsWith('You have no open')"},
                     ALL_TEXT, {"shot": "liq-no-chase-form.png"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"text": "#out .badge", "as": "badge"},
                     {"text": "#out a.btn.primary", "as": "button"}, {"text": "#out .pstat", "as": "pstat"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["head"].startswith("The simulated exchange liquidated a position on ")
    # (52,000.30 - 62,417.90) x 0.05 = -520.88
    assert got["list"] == "BTC/USD long, 0.0500 BTC at 5x: closed at the mark 52,000.30 (entry 62,417.90), −520.88 USD"
    assert (got["role"], got["badge"], got["button"], got["pstat"]) == ("status", "Opened · not open now", "New chase", "Closed")


def test_page_a_chase_whose_last_book_fills_the_rest_and_liquidates_says_liquidated(tool):
    # LIQUIDATION-IN-FINAL-FILL-BOOK-HIDDEN
    tool.eng.gw.account.cash = D("700")
    cid = tool.start("long", "0.05", leverage=5)
    tool.trade("sell", "62417.0", "0.01")                          # a part fill
    tool.beat(1)
    tool.book("BTC/USD", *CRASH)                                    # the ask falls through the order: the rest fills, then the account is liquidated
    assert (tool.eng.chase.filled, tool.eng.chase.outcome, tool.eng.gw.account.positions) == (D("0.05"), "liquidated", [])
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#out .badge", "as": "badge"}, {"text": "#out h1", "as": "head"}, {"text": "#notclosed", "as": "note"},
                     {"text": "#out a.btn.primary", "as": "button"}, TEXTS("#out .log li"), ALL_TEXT,
                     {"shot": "liq-same-book-result.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert (got["badge"], got["head"], got["button"]) == ("Liquidated", "Liquidated: the simulated exchange closed the long (simulated)", "New chase")
    assert got["note"] == "Liquidated by the simulated exchange: 0.0500 BTC, at the mark. A dry run has no position on Kraken."
    log = " ".join(got["#out .log li"])
    assert "Order complete. Position now: 0.0500 BTC long, 5x." in log
    assert "The simulated exchange liquidated the position on the same book: the account margin level fell to 40%." in log


def test_page_resting_at_the_cap_after_a_cancel_and_replace_names_the_simulated_exchange(tool):
    # REPLACE-NOTE-COPY: the note also shows when the order rests at the cap
    tool.eng.gw.refuse_margin_amends = True
    tool.start("long", leverage=3)
    tool.book("BTC/USD", [("62420.0", "1")], [("62418.5", "0"), ("62419.5", "0"), ("62421.0", "3")])
    for _ in range(4):
        tool.timeout(6)                                             # the amend is refused: cancel, read, a new order at the cap
    c = tool.eng.chase
    assert (c.phase, c.price, len(c.legs)) == ("resting", c.limit, 2)
    got = tool.look([{"goto": "/chase"}, card_shown("resting"), {"text": "#statuscard .head", "as": "head"},
                     {"text": "#statuscard .sub", "as": "sub"}])
    assert got["head"] == "Resting at the cap"
    assert "The simulated exchange refuses amends of this order, so each move is a cancel and a new order (1 so far)." in got["sub"]


def test_page_a_close_that_filled_nothing_gives_the_one_cause_on_its_result(tool):
    # RESULT-NOTCLOSED-VAGUE: the cause that the tool knows, as the chase page states it
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    cid = tool.start("close-long", "0.02")
    tool.book("BTC/USD", [("62417.9", "0"), ("62400.0", "1")], [])   # the bid falls below the floor, 62,417.90
    tool.timeout(121)
    assert (tool.eng.chase.outcome, tool.eng.chase.filled) == ("notfilled", 0)
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"text": "#out h1 + p", "as": "line"}])
    assert got["line"] == "The price fell below your floor before the rest could close. The reduce-only IOC at the floor filled nothing."


# ---------- repair round 8: the close button names the whole row; ends judged as the result judges them ----------

BUTTON = "Array.from(document.querySelectorAll('#out a.btn.primary, #actions a.btn')).map(e => e.textContent).filter(t => t.startsWith('Close'))[0]"


def test_page_the_close_button_of_an_open_names_the_whole_row_it_closes(tool):
    # CLOSE-THIS-POSITION-CLOSES-ALL: the link closes the whole row (2x and 4x), oldest first.
    opened_long(tool, "0.01", 2)
    got = tool.look([{"goto": "/chase"}, card_shown("filled"), {"eval": BUTTON, "as": "one"}])
    assert got == {"one": "Close the position (0.0100 BTC)"}
    two_longs(tool)
    cid = tool.eng.chase.id
    got = tool.look([{"goto": "/chase"}, card_shown("filled"), {"eval": BUTTON, "as": "chase"},
                     {"text": "#statuscard .note.info li:last-child", "as": "todo"},
                     ALL_TEXT, {"shot": "r85-close-row-chase.png"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"eval": BUTTON, "as": "result"},
                     ALL_TEXT, {"shot": "r85-close-row-result.png"}])
    assert got.pop("all")["c"] >= 4.5
    row = "Close the long (0.0400 BTC, 3 positions)"     # the first 2x, then the 2x and the 4x of two_longs
    assert got == {"chase": row, "todo": f'To close it, use "{row}".', "result": row}


def test_page_an_open_refused_with_nothing_filled_says_nothing_opened_in_the_position_card(tool):
    # NOTHING-OPENED-SAYS-CLOSED
    send = tool.eng.gw.answer
    tool.eng.gw.answer = lambda cmd, now: [core.Rejected(now, "place", "EOrder:Insufficient margin")] if isinstance(cmd, core.Place) else send(cmd, now)
    cid = tool.start("long", "0.01", leverage=2)
    assert (tool.eng.chase.outcome, tool.eng.chase.filled) == ("refused", 0)
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#out .pstat", "as": "pstat"}, ALL_TEXT, {"shot": "r85-refused-open-result.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got == {"pstat": "Nothing opened"}


def test_page_a_first_order_refused_as_would_cross_says_so_and_goes_out_again_at_the_new_bid(tool):
    # T44 (owner G21 B): the first order waits and goes out again, it does not end the chase at 0:00.
    refused = refuse_first_place(tool)
    tool.start()
    assert (tool.eng.chase.phase, tool.eng.chase.price, len(refused)) == ("resting", None, 1)

    def bid_rises():
        tool.book("BTC/USD", [("62418.1", "1")], [])
        tool.beat(5)                                     # the wait from the first order ends: the new order goes out

    log = "Array.from(document.querySelectorAll('#log li')).map(x => x.textContent)"
    got = tool.look([{"goto": "/chase"}, card_shown("placeagain"), {"text": "#statuscard .head", "as": "head"},
                     {"text": "#statuscard .sub", "as": "sub"}, {"eval": log, "as": "log"},
                     {"eval": "[!!OC.$('stop'), !!OC.$('fillnow')]", "as": "buttons"}, ALL_TEXT, {"shot": "t44-first-order-refused.png"},
                     {"signal": "bid"}, card_shown("resting"), {"text": "#statuscard .head", "as": "after"},
                     {"eval": log, "as": "log_after"}, {"shot": "t44-placed-again.png"}], on={"bid": bid_rises})
    assert got.pop("all")["c"] >= 4.5
    assert got["head"] == "Order rejected: placing it again at the best bid"
    assert got["sub"] == ("The simulated exchange rejected the order: would cross the ask (post-only). The tool places it again at "
                          "the best bid after the wait. No order rests now. The new order never goes above the cap, 62,418.50. "
                          "At the timeout, the tool places no new order. If the price is valid then, it sends one IOC at the cap "
                          "for the rest. (Simulated. No order goes to Kraken.)")
    assert got["buttons"] == [True, True]
    assert got["log"][0].endswith("The tool places it again at the best bid after the wait.")
    assert got["after"] == "Resting at the best bid"
    assert got["log_after"][0].endswith("Placed a post-only buy, 0.0500 BTC at 62,418.10.")


def refuse_first_place(tool):
    """The simulated exchange rejects the next place as would-cross (T44); the ones after it go through."""
    send, refused = tool.eng.gw.send, []

    async def first_refused(cmd, now):   # the gateway's send is async (T4)
        if isinstance(cmd, core.Place) and not refused:
            refused.append(cmd)
            return [core.Rejected(now, "place", "would_cross")]
        return await send(cmd, now)
    tool.eng.gw.send = first_refused
    return refused


def test_page_the_result_counts_a_first_order_placed_again_apart_from_the_cancel_and_replace(tool):
    # RESULT-SAYS-AMEND-REFUSED-AFTER-RETRY: the first order placed again is not a cancel and replace.
    refuse_first_place(tool)
    cid = tool.start("long", leverage=3)
    tool.book("BTC/USD", [("62418.1", "1")], [])
    tool.beat(5)                                                    # the first order goes out again: leg -1
    assert tool.eng.chase.legs[-1].endswith("-1") and tool.eng.chase.price is not None
    tool.call(tool.eng.user, "stop")
    assert tool.eng.chase.outcome == "stopped"
    first = cid

    refuse_first_place(tool)
    tool.eng.gw.refuse_margin_amends = True
    cid = tool.start("long", leverage=3)
    tool.book("BTC/USD", [("62418.2", "1")], [])
    tool.beat(5)                                                    # placed again: leg -1
    tool.book("BTC/USD", [("62418.3", "1")], [])
    for _ in range(3):
        tool.timeout(6)                                             # the amend is refused: cancel, read, a new order (-2)
    c = tool.eng.chase
    assert (c.replace, len(c.legs), c.phase) == (True, 3, "resting")
    tool.call(tool.eng.user, "stop")
    got = tool.look([{"goto": f"/result?id={first}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"eval": "[OC.$('againnote')?.textContent, OC.$('replacenote')?.textContent ?? null]", "as": "first"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#againnote", "as": "again"}, {"eval": "OC.$('replacenote').textContent.split('.')[0]", "as": "replace"},
                     ALL_TEXT, {"shot": "t44-result-again-and-replace.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got["first"] == ["First order placed again: 1 time. The simulated exchange rejected it, so the tool placed it again after the wait.", None]
    assert got["again"].startswith("First order placed again: 1 time.")
    assert got["replace"] == "Cancel and replace: 1 time"


def test_page_a_stop_with_no_order_resting_says_there_was_nothing_to_cancel(tool):
    # STOPPED-COPY-CLAIMS-CANCEL: after a rejected first order no order rests, so Stop cancels nothing.
    refuse_first_place(tool)
    cid = tool.start("long", leverage=3)
    tool.call(tool.eng.user, "stop")
    assert (tool.eng.chase.outcome, tool.eng.chase.price) == ("stopped", None)
    spot = tool.start()                                             # a spot chase with an order resting: a real cancel
    tool.call(tool.eng.user, "stop")
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "main .card p.muted", "as": "result"}, {"eval": OC_LOG, "as": "log"},
                     {"goto": f"/result?id={spot}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"eval": "document.body.innerText.includes('The simulated exchange confirmed the cancel.')", "as": "spot"}])
    assert got["result"].endswith("No order rested, so there was nothing to cancel.")
    assert "You pressed Stop. No order rests, so there is nothing to cancel." in got["log"]
    assert got["spot"] is True


def test_page_the_chase_card_after_a_stop_with_no_order_resting_says_there_was_nothing_to_cancel(tool):
    refuse_first_place(tool)
    tool.start()
    tool.call(tool.eng.user, "stop")
    got = tool.look([{"goto": "/chase"}, card_shown("stopped"), {"text": "#statuscard .sub", "as": "sub"}])
    assert got["sub"].startswith("You stopped the chase. No order rested, so there was nothing to cancel. 0.0000 BTC filled before the stop.")


def test_page_a_close_ended_by_a_restart_names_the_open_position_on_the_chase_page(tool):
    # RESTART-CLOSE-SAYS-TEST-AGAIN: the chase page says what the result page says.
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    cid = tool.start("close-long", "0.02")
    tool.call(tool.eng.handle, core.Restarted(tool.clock.t + 30, tool.clock.t))
    todo = "Array.from(document.querySelectorAll('.note.info li')).map(x => x.textContent)"
    got = tool.look([{"goto": "/chase"}, card_shown("ended"), {"eval": todo, "as": "chase"}, ALL_TEXT, {"shot": "r85-restart-close-chase.png"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"eval": todo, "as": "result"}])
    assert got.pop("all")["c"] >= 4.5
    still = "Your long is still open: 0.0200 BTC at 3x. Close it from the form."
    assert got == {"chase": ["Nothing to check in Kraken Pro: a dry run sends no orders.", still], "result": [still]}


def test_page_the_chase_page_judges_a_whole_close_order_by_the_order_and_what_stays_is_neutral(tool):
    # CHASE-PAGE-CLOSE-JUDGED-BY-POSITION (G16 = A), as the result and the history judge it
    t1, t2 = two_longs(tool)
    tool.start("close-long", "0.015")
    tool.trade("buy", "62001.0", "0.015")
    assert tool.eng.chase.outcome == "filled"
    got = tool.look([{"goto": "/chase"}, card_shown("filled"), {"text": "#pstat", "as": "pstat"},
                     {"eval": "OC.$('pstat').className", "as": "tone"}, {"text": "#pdet", "as": "pdet"},
                     ALL_TEXT, {"shot": "r85-close-as-asked-chase.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got == {"pstat": "Closed as asked", "tone": "pstat tone-fill",
                   "pdet": f"Closed: the 2x position opened {t1} (0.0100 BTC) and 0.0050 BTC of the 4x position opened {t2}. "
                           "Stays open: 0.0150 BTC at 4x. (simulated)"}


# ---------- repair round r92 ----------

CARD = "document.querySelector('#statuscard').innerText"


def test_page_an_open_with_no_free_margin_says_close_a_position_first_and_links_the_close(tool):
    # NO-FREE-MARGIN-DEAD-END: no amount can open, so the form does not ask for a smaller amount.
    opened_long(tool, "0.02", 3)
    tool.eng.gw.account.cash = D(300)       # collateral in use 416.12 USD > equity: free margin for new orders below 0
    tool.eng.account = None
    got = tool.look([{"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"}, {"click": "#what button[data-w=long]"},
                     {"waitFor": "!OC.$('oppose').classList.contains('hidden')"}, {"text": "#oppose", "as": "oppose"}, {"text": "#amterr", "as": "amterr"},
                     ALL_TEXT, {"shot": "r92-no-free-margin.png"}, {"click": "#toclose"},
                     {"waitFor": "document.querySelector('#what button[data-w=close][aria-pressed=true]') && document.querySelector('#poslist input')"},
                     {"eval": "OC.$('oppose').classList.contains('hidden')", "as": "gone"}])
    assert got.pop("all")["c"] >= 4.5
    head, rest = got.pop("oppose").split(" Free margin for new orders: ")
    assert head == "No free margin for a new open. Close a position first."
    assert rest.startswith("−") and rest.endswith(" USD (simulated account). Close a position"), rest
    assert got == {"amterr": "", "gone": True}


def test_page_a_close_that_ends_with_nothing_closed_has_the_close_title_on_the_chase_page(tool):
    # CLOSE-END-TITLE-STOPPED: the words of the result and the history
    opened_long(tool, "0.02", 3)
    tool.beat(1)
    tool.start("close-long", "0.02", timeout=30)
    tool.book("BTC/USD", [("62417.9", "0"), ("60000.0", "1")], [])     # the bid falls below the floor: the IOC fills nothing
    tool.timeout(31)
    assert (tool.eng.chase.outcome, tool.eng.chase.filled) == ("notfilled", 0)
    got = tool.look([{"goto": "/chase"}, card_shown("notfilled"), {"eval": "OC.$('live').textContent", "as": "title"}])
    assert got == {"title": "Not closed: position open"}


def test_page_the_chase_page_after_a_restart_gives_one_number_for_the_gap(tool):
    # RESTART-GAP-NUMBERS: the last event at 0:09.7, the new start at 0:21.4: 11 s off, as the event log says
    tool.start()
    t0 = tool.clock.t
    tool.call(tool.eng.handle, core.Restarted(t0 + 21.4, t0 + 9.7))
    got = tool.look([{"goto": "/chase"}, card_shown("ended"), {"eval": CARD, "as": "card"}, {"text": "#log", "as": "log"}])
    assert "The tool stopped and started again after 11 s off." in got["card"]
    assert "Tool started again after 11 s off." in got["log"]
    assert "ENDED: TOOL STOPPED OR RESTARTED · VALUES FROM BEFORE THE STOP" in got["card"]
    assert "Prices: values from before the stop. There was no valid price at the end." in got["card"]
    assert not any(f"{n} s" in got["card"].replace("11 s off", "") for n in range(1, 60)), got["card"]   # no other number of seconds


def test_page_the_result_of_an_open_whose_position_closed_later_shows_no_present_costs(tool):
    # CLOSED-POSITION-PRESENT-TENSE: the rows "Collateral it uses" and "Rollover, each 4 h while open" only while it is open
    cid = opened_long(tool, "0.02", 3)
    rows = {"eval": "['collat', 'rollnow'].map(id => !!OC.$(id))", "as": "rows"}
    open_now = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, rows])
    tool.beat(1)
    tool.start("close-long", "0.02")
    tool.trade("buy", "62419.0", "0.02")
    assert tool.eng.chase.outcome == "filled"
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, rows,
                     {"eval": "document.body.innerText.includes('Collateral it uses') || document.body.innerText.includes('while open')", "as": "words"},
                     {"text": ".pstat", "as": "pstat"}, ALL_TEXT, {"shot": "r92-open-result-position-closed.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert open_now == {"rows": [True, True]}
    assert got == {"rows": [False, False], "words": False, "pstat": "Closed"}


def test_page_the_gauge_has_no_now_dot_when_there_is_no_position(tool):
    # NO-POSITION-GAUGE-MARKER
    tool.start("long", "0.02", leverage=3)
    tool.call(tool.eng.user, "stop")
    assert tool.eng.chase.outcome == "stopped"
    gauge = {"eval": "[document.querySelectorAll('#pgauge .pt').length, OC.$('pgauge').innerText.trim().split('\\n').pop()]", "as": "gauge"}
    got = tool.look([{"goto": "/chase"}, card_shown("stopped"), {"waitFor": "document.querySelector('#pgauge .gauge')"}, gauge,
                     ALL_TEXT, {"shot": "r92-gauge-no-position.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got == {"gauge": [0, "Now: no position, so no margin level."]}
    opened_long(tool, "0.02", 3)                       # with a position: the dot is there
    got = tool.look([{"goto": "/chase"}, card_shown("filled"), {"waitFor": "document.querySelector('#pgauge .pt')"}, gauge])
    assert got["gauge"][0] == 1


def test_page_a_liquidation_before_the_ioc_fills_ends_liquidated_with_the_position_that_stays(tool):
    # LATE-IOC-AFTER-LIQUIDATION-SAYS-FILLED: the IOC that the core sent before the liquidation fills after it.
    send, held = tool.eng.gw.answer, []
    tool.eng.gw.answer = lambda cmd, now: held.append(cmd) or [] if isinstance(cmd, core.MarginIoc) and not held else send(cmd, now)
    tool.eng.gw.account.cash = D(1500)
    cid = tool.start("long", "0.105", timeout=30, leverage=5)
    tool.trade("sell", "62417.0", "0.10")
    tool.timeout(31)
    assert (tool.eng.chase.phase, len(held)) == ("ioc", 1)
    tool.book("BTC/USD", [("62417.9", "0"), ("49000.0", "5")], [("62418.5", "0"), ("62419.5", "0"), ("49000.6", "5")])   # liquidates
    assert tool.eng.chase.exit == "liquidated"
    tool.call(lambda: [tool.eng.handle(ev) for ev in send(held[0], tool.clock())])
    c = tool.eng.chase
    assert (c.outcome, c.filled, [str(p["qty"]) for p in tool.eng.gw.account.positions]) == ("liquidated", D("0.105"), ["0.005"])
    got = tool.look([{"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"}, {"text": "#out .badge", "as": "badge"},
                     {"text": "#notclosed", "as": "note"}, {"text": ".pstat", "as": "pstat"}, {"text": ".log li", "as": "last"},
                     ALL_TEXT, {"shot": "r92-late-ioc-after-liquidation-result.png"}])
    assert got.pop("all")["c"] >= 4.5
    assert got == {"badge": "Liquidated",
                   "note": "Liquidated by the simulated exchange: 0.1000 BTC, at the mark. A dry run has no position on Kraken. "
                           "The 0.0050 BTC that filled after it stays open as a position.",
                   "pstat": "Open: 0.0050 BTC long, 5x",
                   "last": "0:31Filled 0.0050 BTC at 49,000.60 (taker, IOC). Order complete. Position now: 0.0050 BTC long, 5x."}


# ---------- T4 U6: the live pages, against the LOCAL fake Kraken (tests/fake_kraken.py; sample data, no real key) ----------

AWAKE = ["/bin/sh", "-c", "sleep 30", "sh"]   # stands in for caffeinate -i


@pytest.fixture
def fake():
    from fake_kraken import FakeKraken
    k = FakeKraken(time.time)
    yield k
    k.stop()


def live_tool(tmp_path, fake, ready=True):
    """The tool with the fake Kraken. ready: Setup is complete (account answered, the key saved and tested)."""
    from fake_kraken import API, SECRET
    from order_chaser import keys
    shots = Path(os.environ.get("OC_SHOTS", tmp_path / "shots"))
    shots.mkdir(parents=True, exist_ok=True)
    if ready:
        keys.KeyStore().save(keys.Key(API, SECRET))    # the in-memory keyring of conftest.py
    t = Tool(tmp_path, shots, kraken_url=fake.url, kraken_ws=fake.ws_url, awake_cmd=AWAKE)
    fake.clock = t.clock
    if ready:
        t.eng.db.set_setting("account", json.dumps({"answer": "no", "at": t.clock()}))
        t.eng.db.set_setting("key", json.dumps({"saved": t.clock(), "tested": t.clock(), "verdict": "ok", "permissions": {}}))
    return t


def test_page_setup_asks_the_account_question_then_saves_and_tests_the_key(tmp_path, fake):
    from fake_kraken import API, SECRET
    t = live_tool(tmp_path, fake, ready=False)
    try:
        got = t.look([{"goto": "/setup"}, {"waitFor": "document.getElementById('ano')"}, {"shot": "live-setup-account-question.png"},
                      {"text": "#acctbody", "as": "question"}, ALL_TEXT,
                      {"click": "#ano"}, {"waitFor": "!document.getElementById('keyform').classList.contains('hidden')"},
                      {"fill": ["#k1", API]}, {"fill": ["#k2", SECRET]}, {"click": "#save"},
                      {"eval": "[OC.$('k1').value, OC.$('k2').value]", "as": "fields"},
                      {"waitFor": "OC.$('bK').textContent === 'Ready'"}, {"shot": "live-setup-key-tested.png"},
                      {"text": "#keybody", "as": "key"}, {"text": "#perms", "as": "perms"}, {"text": "#finish", "as": "finish"},
                      {"text": "#host", "as": "host"},
                      {"eval": "document.body.innerText.includes(" + json.dumps(SECRET[:12]) + ")", "as": "shown"},
                      {**ALL_TEXT, "as": "all2"}])
    finally:
        t.stop()
    assert "Does another bot or API tool use this Kraken account?" in got["question"]
    assert got["fields"] == ["", ""]                                           # C6: cleared after the send
    assert "Saved in your macOS Keychain. The tool never shows it again." in got["key"]
    assert "At each start of the tool, macOS asks one time." in got["key"]     # Q9
    assert "Check these names against your Kraken key page." in got["perms"]  # C9
    assert "Withdraw Funds" in got["perms"] and "Setup complete. Live chases are possible." in got["finish"]
    assert got["host"] == f"127.0.0.1:{t.port}" and got["shown"] is False
    assert got["all"]["c"] >= 4.5 and got["all2"]["c"] >= 4.5, (got["all"], got["all2"])


def test_page_live_confirm_names_stop_loss_orders_then_the_chase_renews_the_timer_and_loses_the_private_feed(tmp_path, fake):
    fake.add_other("stop-loss", "XETHZUSD", "sell 0.4 ETHUSD @ stop loss 2310.00")
    fake.add_other("take-profit", "XETHZUSD", "sell 0.4 ETHUSD @ take profit 2780.00")
    fake.add_other("limit", "SOLUSD", "buy 12 SOLUSD @ limit 138.50")
    t = live_tool(tmp_path, fake)
    renewed = clock_text(t.clock.t + 21)

    def renew():
        t.beat(21)
        t.call(t.eng.tick)

    def lose_feed():
        fake.ws_down = True
        fake.run(fake.drop_ws)
    try:
        got = t.look([{"goto": "/new"}, {"waitFor": "!OC.$('livebtn').disabled && !OC.$('start').disabled"},
                      {"click": "#livebtn"}, {"text": "#whatchanges", "as": "changes"}, {"text": "#modebadge", "as": "badge"},
                      {"click": "#start"}, {"waitFor": "!OC.$('confirm').classList.contains('hidden')"},
                      {"shot": "live-confirm-stop-loss.png"}, {"text": "#otherlist", "as": "others"}, {"eval": "OC.$('amt').value", "as": "amt"},
                      {"eval": "OC.$('cyes').disabled", "as": "blocked"}, {**ALL_TEXT, "as": "cc"},
                      {"click": "#okothers"}, {"click": "#okfirst"}, {"click": "#cyes"},
                      {"waitFor": "location.pathname === '/chase' && document.getElementById('timerline') && /Renewed/.test(OC.$('timerline').textContent)"},
                      {"signal": "renew"}, {"waitFor": f"OC.$('timerline').textContent.includes('Renewed at {renewed}')"},
                      {"shot": "live-chase-timer-renewing.png"}, {"text": "#livepanel", "as": "panel"}, {**ALL_TEXT, "as": "ct"},
                      {"signal": "lose"}, {"waitFor": "document.getElementById('pfeedline')"},
                      {"shot": "live-chase-private-feed-lost.png"}, {"text": "#pfeedline", "as": "pfeed"}, {**ALL_TEXT, "as": "pt"}],
                     on={"renew": renew, "lose": lose_feed})
    finally:
        t.stop()
    assert "You chose Live for the first time." in got["changes"] and got["badge"] == "LIVE: real orders"
    assert got["others"].index("stop-loss") < got["others"].index("limit") and "2 of them protect a position." in got["others"]
    assert got["amt"] == "0.00005" and got["blocked"] is True                   # Q6 minimum; the ticks come first
    assert "If the tool stops, Kraken cancels ALL orders on this account" in got["panel"]
    assert "Reading the order by REST every 5 s" in got["pfeed"] or "reads the order by REST every 5 s" in got["pfeed"]
    for k in ("cc", "ct", "pt"):
        assert got[k]["c"] >= 4.5, (k, got[k])


def test_page_the_timer_fired_lists_the_orders_that_kraken_cancelled(tmp_path, fake):
    fake.add_other("take-profit", "XETHZUSD", "sell 0.4 ETHUSD @ take profit 2780.00")
    fake.add_other("stop-loss", "XETHZUSD", "sell 0.4 ETHUSD @ stop loss 2310.00")
    t = live_tool(tmp_path, fake)

    def fire():
        t.beat(61)                                                            # no renewal reached Kraken in 60 s
        t.eng.kgw.timer["deadline"] = t.clock.t - 1
        fake.run(fake.fire_timer)
    try:
        assert t.call(t.eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        got = t.look([{"goto": "/chase"}, {"waitFor": "document.getElementById('timerline')"}, {"signal": "fire"},
                      card_shown("timer"), {"waitFor": "document.getElementById('othersgone')"},
                      {"shot": "live-chase-timer-fired.png"}, {"text": "#othersgone", "as": "gone"}, {"text": "#statuscard", "as": "card"},
                      ALL_TEXT], on={"fire": fire})
    finally:
        t.stop()
    assert "Kraken also cancelled 2 of your other open orders, 2 of them stop-loss or take-profit orders." in got["gone"]
    assert "The tool does not place them for you." in got["gone"] and len(fake.calls_of("AddOrder")) == 1
    assert got["all"]["c"] >= 4.5, got["all"]


def test_page_kraken_not_answering_ends_the_chase_in_a_clear_state(tmp_path, fake):
    t = live_tool(tmp_path, fake)
    try:
        assert t.call(t.eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        got = t.look([{"goto": "/chase"}, {"waitFor": "document.getElementById('timerline')"}, {"signal": "silent"},
                      card_shown("noanswer"), {"shot": "live-chase-kraken-not-answering.png"}, {"text": "#statuscard", "as": "card"}, ALL_TEXT],
                     on={"silent": lambda: t.call(t.eng.handle, core.NoAnswer(t.clock(), ""))})
    finally:
        t.stop()
    assert "Kraken did not answer the reads of the order for 60 s." in got["card"]
    assert "The safety timer cancels the order on Kraken within 60 s." in got["card"]
    assert got["all"]["c"] >= 4.5, got["all"]


def test_page_reconcile_says_why_new_live_chases_wait_then_what_it_found(tmp_path, fake):
    t = live_tool(tmp_path, fake)
    assert t.call(t.eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    cid = t.eng.chase.id
    t.look([{"goto": "/chase"}, card_shown("resting"), {"signal": "fill"}],
           on={"fill": lambda: fake.run(fake.fill, cid, "0.018", None, False)})   # a fill that the tool does not see
    t.stop()                                                                  # the tool stops during the chase
    fake.rest_down = True
    t = live_tool(tmp_path, fake, ready=False)
    try:
        got = t.look([{"goto": "/"}, {"waitFor": "document.querySelector('#statuscard[data-state=\"cantread\"]')"},
                      {"shot": "live-reconcile-cannot-read.png"}, {"text": "#root", "as": "cant"}, {**ALL_TEXT, "as": "a1"},
                      {"signal": "back"}, {"waitFor": "document.querySelector('#statuscard[data-state=\"open\"]')", "timeout": 20000},
                      {"shot": "live-reconcile-found-open.png"}, {"text": "#root", "as": "found"}, {**ALL_TEXT, "as": "a2"}],
                     on={"back": lambda: setattr(fake, "rest_down", False)})
    finally:
        t.stop()
    assert "The tool blocks new live chases until it reads Kraken." in got["cant"]
    assert "Your order was still open on Kraken. The tool cancelled it." in got["found"] and "0.0180 of 0.0500 BTC (36%)" in got["found"]
    assert got["a1"]["c"] >= 4.5 and got["a2"]["c"] >= 4.5, (got["a1"], got["a2"])


def test_page_setup_says_why_the_key_cannot_be_removed_while_a_live_chase_runs(tmp_path, fake, memory_keyring):
    from order_chaser import keys
    t = live_tool(tmp_path, fake)
    try:
        assert t.call(t.eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        got = t.look([{"goto": "/setup"}, {"waitFor": "document.getElementById('remove')"}, {"click": "#remove"},
                      {"waitFor": "OC.$('keymsg').textContent.length > 0"}, {"shot": "live-setup-key-busy.png"},
                      {"text": "#keymsg", "as": "msg"}, ALL_TEXT])
    finally:
        t.stop()
    assert got["msg"] == "A live chase runs now. It uses the saved key. Change, test or remove the key when the chase ends."
    assert list(memory_keyring.items) == [(keys.SERVICE, keys.ACCOUNT)]
    assert got["all"]["c"] >= 4.5, got["all"]


def test_page_a_kraken_text_with_html_shows_as_text(tmp_path, fake):
    """KRAKEN-TEXT-UNESCAPED: Kraken's error text goes into the page as text, never as HTML."""
    evil = 'EOrder:<img src=x id=pwn><a id=pwnlink href="https://evil.example">Re-enter your key</a>'
    fake.errors["AddOrder"] = [evil]
    t = live_tool(tmp_path, fake)
    try:
        assert t.call(t.eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        cid = t.eng.chase.id
        got = t.look([{"goto": "/chase"}, card_shown("refused"), {"text": "#statuscard", "as": "card"},
                      {"eval": "!!document.getElementById('pwn') || !!document.getElementById('pwnlink')", "as": "html"},
                      {"goto": f"/result?id={cid}"}, {"waitFor": "document.body.innerText.includes('Re-enter your key')"},
                      {"eval": "!!document.getElementById('pwn') || !!document.getElementById('pwnlink')", "as": "html2"}])
    finally:
        t.stop()
    assert f"Kraken rejected the order: {evil}." in got["card"]
    assert got["html"] is False and got["html2"] is False


# ---------- phone width (T45) ----------

# The page width, each element that goes past the left or right edge of the window, or into the 16 px gutter of the
# page (UX R90, QA R91 of T5, QA R138), and each box
# whose content is wider than the box (it scrolls or cuts the content sideways; QA R128). An element inside such a box
# does not count, because the box is reported. Only a box that matches "scrolls" may scroll: the history table.
# A pill or a time that breaks over two lines is reported too (QA R138).
WIDTH_JS = """(scrolls) => {
  const W = innerWidth;
  const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\\s+/).join('.') : '');
  const shown = e => e.getBoundingClientRect().width > 0 && !e.closest('.sr-only');
  const boxed = e => { for (let a = e.parentElement; a && a !== document.body; a = a.parentElement) if (getComputedStyle(a).overflowX !== 'visible') return true; return false; };
  const all = Array.from(document.querySelectorAll('body *')).filter(shown);
  const gutter = e => e.parentElement.closest('.app') && !e.closest('.scrim') ? 16 : 0;
  const wide = all.filter(e => { const r = e.getBoundingClientRect(), g = gutter(e); return (r.right > W - g + 0.5 || r.left < g - 0.5) && !boxed(e); })
    .map(e => name(e) + ' ' + Math.round(e.getBoundingClientRect().left) + '..' + Math.round(e.getBoundingClientRect().right));
  const cut = all.filter(e => getComputedStyle(e).overflowX !== 'visible' && e.scrollWidth > e.clientWidth + 1 && !(scrolls && e.matches(scrolls)))
    .map(e => name(e) + ' ' + e.scrollWidth + '>' + e.clientWidth);
  const lines = e => { const g = document.createRange(); g.selectNodeContents(e); return new Set(Array.from(g.getClientRects()).map(r => Math.round(r.top))).size; };
  const split = all.filter(e => e.matches('.badge, td.mono') && lines(e) > 1).map(e => name(e) + ' ' + e.textContent.trim());
  return { scroll: document.scrollingElement.scrollWidth, wide: wide.slice(0, 12), cut: cut.slice(0, 12), split: split.slice(0, 12) };
}"""


def test_page_every_page_fits_a_375_px_phone_with_no_sideways_scroll_and_readable_text(tool):
    spot = tool.start(qty="0.5", timeout=30)                      # the widest fills: 8-decimal quantities and a taker (IOC) fill
    tool.trade("sell", "62417.0", "0.49345678")                   # the IOC fills the rest, 0.00654322
    tool.timeout(30)
    assert [f.maker for f in tool.eng.chase.fills] == [True, False]
    tool.beat(1)
    opened_long(tool)                                             # a position to close, a result and a history row
    tool.beat(1)
    cid = tool.start("close-long", "0.02")                        # a part close: its result has the position and P/L rows
    tool.trade("buy", "62419.0", "0.012")
    tool.call(tool.eng.user, "stop")
    def rest():                                                   # a stopped chase with a long rest: a long button
        tool.call(tool.eng.user, "stop")
        tool.start(qty="0.5")
        tool.trade("sell", "62417.0", "0.00153499")
        tool.call(tool.eng.user, "stop")
    phone = lambda name, scrolls=None: [{"eval": f"({WIDTH_JS})({json.dumps(scrolls)})", "as": name}, {**ALL_TEXT, "as": name + " contrast"},
                                        {"shot": f"phone-{name.replace('/', '').replace(' ', '-') or 'new'}.png"}]
    got = tool.look([
        {"viewport": [375, 812]},
        {"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"}, *phone("/new buy"),
        {"click": "#what button[data-w=long]"}, {"waitFor": "OC.$('mgbox').querySelector('.gauge')"}, *phone("/new open long"),
        {"goto": "/new?what=close&pair=BTC/USD&dir=long"}, {"waitFor": "document.querySelector('.poslist label') && OC.$('mgbox').querySelector('.gauge')"},
        *phone("/new close"),
        {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('#pl') && document.querySelector('#plan')"}, *phone("/result close"),
        {"goto": f"/result?id={spot}"}, {"waitFor": "document.querySelector('.facts')"}, *phone("/result spot"),
        {"eval": "Array.from(document.querySelectorAll('table.fills tr'), r => Array.from(r.cells, c => c.textContent))", "as": "fills"},
        {"viewport": [390, 844]}, {"goto": f"/result?id={spot}"}, {"waitFor": "document.querySelector('.facts')"}, *phone("/result spot 390"),
        {"viewport": [375, 812]},
        {"goto": "/history"}, {"waitFor": "document.querySelector('tr.click')"}, *phone("/history", "#out"),
        {"eval": "getComputedStyle(OC.$('out'), '::before').content", "as": "hint"},
        {"goto": "/setup"}, {"waitFor": "OC.$('mgline').textContent"}, *phone("/setup"),
        {"signal": "start"}, {"goto": "/chase"}, card_shown("resting"), {"waitFor": "document.querySelector('#statuscard .rail .mk')"}, *phone("/chase"),
        {"click": "#stop"}, {"waitFor": "!OC.$('stopdlg').classList.contains('hidden')"}, *phone("/chase stop dialog"),
        {"eval": "(r => r.right <= innerWidth && r.bottom <= innerHeight)(OC.$('stopyes').getBoundingClientRect())", "as": "stop button in view"},
        {"signal": "rest"}, {"goto": "/chase"}, card_shown("stopped"), {"text": "#actions a[href^='/new?rest']", "as": "rest button"},
        *phone("/chase stopped with a long rest"),
        {"viewport": [1280, 900]}, {"goto": "/history"}, {"waitFor": "document.querySelector('tr.click')"},
        {"eval": "getComputedStyle(OC.$('out'), '::before').content", "as": "desktop hint"}],
        on={"start": lambda: tool.start(), "rest": rest})
    pages = ["/new buy", "/new open long", "/new close", "/result close", "/result spot", "/history", "/setup", "/chase",
             "/chase stop dialog", "/chase stopped with a long rest"]
    assert {p: got[p] for p in pages} == {p: {"scroll": 375, "wide": [], "cut": [], "split": []} for p in pages}
    assert got["/result spot 390"] == {"scroll": 390, "wide": [], "cut": [], "split": []}
    assert got["fills"] == [["Time", "BTC", "Price", "Type", "Fee USD"], ["0:00", "0.49345678", "62,417.90", "maker", "123.20"],
                            ["0:30", "0.00654322", "62,418.50", "taker (IOC)", "3.27"]]
    assert got["stop button in view"] is True
    assert got["rest button"] == "Chase the rest (0.49846501 BTC), new cap"
    assert (got["hint"], got["desktop hint"]) == ('"Scroll sideways to see every column \u2192"', "none")
    low = {p: got[p + " contrast"] for p in pages if got[p + " contrast"]["c"] < 4.5}
    assert low == {}


def test_page_the_t44_states_fit_a_375_px_phone(tool):
    # T44 at phone width: the chase page while the first order waits to go out again, and the result with its note.
    refuse_first_place(tool)
    cid = tool.start("long", leverage=3)                          # a margin open: its result has the note
    assert (tool.eng.chase.phase, tool.eng.chase.price) == ("resting", None)

    def again_and_stop():
        tool.book("BTC/USD", [("62418.1", "1")], [])
        tool.beat(5)                                                # the first order goes out again
        tool.call(tool.eng.user, "stop")

    fit = lambda name: [{"eval": f"({WIDTH_JS})(null)", "as": name}, {**ALL_TEXT, "as": name + " contrast"},
                        {"shot": f"phone-t44-{name.split()[0].strip('/')}.png"}]
    got = tool.look([{"viewport": [375, 812]},
                     {"goto": "/chase"}, card_shown("placeagain"), {"text": "#statuscard .head", "as": "head"}, *fit("/chase placeagain"),
                     {"signal": "again"},
                     {"goto": f"/result?id={cid}"}, {"waitFor": "document.querySelector('.facts')"},
                     {"text": "#againnote", "as": "note"}, *fit("/result again")], on={"again": again_and_stop})
    assert got["head"] == "Order rejected: placing it again at the best bid"
    assert got["note"].startswith("First order placed again: 1 time.")
    pages = ["/chase placeagain", "/result again"]
    assert {p: got[p] for p in pages} == {p: {"scroll": 375, "wide": [], "cut": [], "split": []} for p in pages}
    assert {p: got[p + " contrast"]["c"] >= 4.5 for p in pages} == {p: True for p in pages}
