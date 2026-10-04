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
    def __init__(self, data_dir, shots):
        self.clock, self.shots, self.books = Clock(), shots, {}
        with socket.socket() as s:       # a free port: the app on 5180, or another agent, does not block the tests
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        guard = create_app(data_dir, connect=False, clock=self.clock, latency=0, port=self.port)
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
        self.call(self.feed._handle, {"channel": "heartbeat"})

    def trade(self, side, price, qty):
        self.call(self.feed._handle, {"channel": "trade", "type": "update",
                                      "data": [{"symbol": "BTC/USD", "side": side, "price": D(price), "qty": D(qty)}]})

    def start(self, side="buy", qty="0.05", timeout=120, leverage=None):
        """side: buy or sell; or a margin choice: long, short, close-long, close-short."""
        assert self.call(self.eng.start, "BTC/USD", side, D(qty), None, timeout, leverage) == []
        return self.eng.chase.id

    def timeout(self, seconds):
        self.beat(seconds)
        self.call(self.eng.tick)

    def look(self, steps, tabs=1):
        """Run the steps in Chrome; return what the steps read."""
        steps = [{**s, "shot": str(self.shots / s["shot"])} if "shot" in s else s for s in steps]
        env = {**os.environ, "HOME": str(self.shots.parent), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
        p = subprocess.run(["node", str(PAGES / "look.mjs"), json.dumps({"base": f"http://127.0.0.1:{self.port}", "tabs": tabs, "steps": steps})], cwd=PAGES, env=env,
                           capture_output=True, text=True, timeout=120)
        result = [x for x in p.stdout.splitlines() if x.startswith("RESULT ")]
        assert result, p.stderr
        return json.loads(result[0][7:])

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


def card_shown(state):
    return {"waitFor": f"document.querySelector('#statuscard[data-state=\"{state}\"]')"}


# The lowest WCAG contrast of the texts that match sel, against the background they sit on (the first ancestor
# with a background colour; the page is dark), with the opacity of every ancestor. A disabled control is left out.
CONTRAST_JS = """(sel) => {
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
  const els = Array.from(document.querySelectorAll(sel)).filter(el => el.offsetParent && el.textContent.trim() && !el.closest('[disabled]'));
  return els.map(one).sort((x, y) => x.c - y.c)[0] || { c: 99, t: '' };
}"""


def contrast(sel):
    return {"eval": f"({CONTRAST_JS})({json.dumps(sel)}).c", "as": "contrast"}


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
    send = tool.eng.gw.send
    def refuse_cancel(cmd, now):
        if isinstance(cmd, core.Cancel):
            return [core.Rejected(now, "cancel", "EGeneral:Internal error")]
        if isinstance(cmd, core.Query):
            o = tool.eng.gw.order
            return [core.OrderState(now, True, o["cum"], o["price"])]
        return send(cmd, now)
    tool.eng.gw.send = refuse_cancel
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
        {"text": "#amthelp", "as": "free"}, ALL_TEXT, {"shot": "m-new-open-long.png"},
        {"select": ["#pairsel", "ETH/USD"]}, {"waitFor": "OC.$('ask').textContent === '2,500.20'"},
        TEXTS("#lev button:not([disabled])"), {"click": "#what button[data-w=short]"},
        {"waitFor": "OC.$('levhelp').textContent.startsWith('Kraken allows no short')"}, {"text": "#mnote", "as": "noshort"},
        {"eval": "OC.$('start').disabled", "as": "blocked"}, {"shot": "m-new-noshort.png"}])
    assert got["#what button"] == ["Buy", "Sell", "Open long", "Open short", "Close a position"]
    low = [got.pop("spot"), got.pop("all")]
    assert min(x["c"] for x in low) >= 4.5, low
    assert got["#lev button:not([disabled])"] == ["2x", "3x"]          # ETH/USD: AssetPairs leverage_buy [2, 3]
    assert got["lev"] == "2x"                                         # the form starts at 2x
    assert got["#mgbox table td:first-child"] == ["Position cost", "Collateral it uses", "Opening fee", "Rollover, each started 4 h"]
    assert got["gauge"] == "Margin level now no position, after 320%. Margin call at 80%, liquidation at 40%."
    assert got["line"] == ("Now no position. After this open: 320%. The tool does not stop an open for its margin level. "
                           "It shows the level so that you decide.")
    assert got["worst"].startswith("The open never costs more than 3,147.45 USD in price and fees: 0.0500 × 62,418.50 (cap) + 24.97 taker fee (0.80%) + 1.56 opening fee")
    assert got["start"] == "Start dry run: open long"
    assert got["free"].startswith("Free margin: 5,000.00 USD (simulated account, read at ")
    assert got["noshort"].startswith("Kraken allows no short on ETH/USD.") and got["blocked"] is True


def test_page_close_lists_the_simulated_positions_with_the_whole_size_reduce_only_and_the_level_after(tool):
    opened_long(tool)
    whole = "OC.$('amt').value === '0.0500'"
    got = tool.look([
        {"goto": "/new"}, {"waitFor": "OC.$('ask').textContent === '62,418.50'"},
        {"click": "#what button[data-w=close]"}, {"waitFor": "document.querySelector('#poslist label') && " + whole},
        TEXTS("#poslist label b"), {"text": "#est", "as": "est"}, {"text": "#amthelp", "as": "help"},
        {"eval": "OC.$('mgbox').querySelector('.gauge').getAttribute('aria-label')", "as": "gauge"},
        {"text": "#start", "as": "start"}, {"text": "#worst", "as": "worst"}, ALL_TEXT, {"shot": "m-new-close.png"},
        {"fill": ["#amt", "0.04999"]}, {"waitFor": "!OC.$('mnote').classList.contains('hidden')"},
        {"text": "#mnote", "as": "rest"}, {"eval": "OC.$('start').disabled", "as": "rest_blocked"}, {"shot": "m-new-close-remainder.png"},
        {"fill": ["#amt", "0.06"]}, {"waitFor": "OC.$('amthelp').classList.contains('err')"},
        {"text": "#amthelp", "as": "over"}, {"eval": "OC.$('start').disabled", "as": "over_blocked"},
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
    send = tool.eng.gw.send
    held = []
    tool.eng.gw.send = lambda cmd, now: held.append(cmd) or [] if isinstance(cmd, core.Cancel) else send(cmd, now)
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
    tool.eng.gw.send = send
    tool.call(lambda: send(held[0], tool.clock.t) and tool.eng.handle(core.Canceled(tool.clock.t)))
    assert tool.eng.chase.legs[-1].endswith("-1") and tool.eng.chase.phase == "resting"
    got = tool.look([{"goto": "/chase"}, card_shown("partial"), {"text": "#statuscard .sub", "as": "sub"}, {"shot": "m-chase-after-replace.png"}])
    assert "The venue refuses amends of this order, so each move is a cancel and a new order (1 so far)." in got["sub"]


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
