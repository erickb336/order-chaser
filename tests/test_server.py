"""Tests of the shell: security guard, restart rule, book checksum on recorded Kraken data, and one
end-to-end dry run driven by a fake public feed. No network."""
import asyncio
import contextlib
import json
import re
import time
import zlib
from html.parser import HTMLParser
from decimal import Decimal as D
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from order_chaser import core, feed
from order_chaser.book import OrderBook
from order_chaser.db import Db, lock_folder
from order_chaser.server import PAGES, create_app

BASE = "http://127.0.0.1:5180"
ORIGIN = {"Origin": BASE}
FIXTURE = Path(__file__).parent / "fixtures" / "kraken-btcusd.jsonl"
PAIRS = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5",
                                      "pair_decimals": 1, "lot_decimals": 8, "status": "online"}})


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(json.loads(msg))


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    guard = create_app(tmp_path, connect=False, clock=clock, latency=0)
    app = guard.app
    with TestClient(guard, base_url=BASE) as client:
        eng = app.state.engine
        f = feed.PublicFeed(eng.on_book, eng.on_trade, eng.on_link)
        f.ws = FakeWs()
        eng.feed = eng_feed = f

        def call(fn, *args):
            return client.portal.call(fn, *args) if asyncio.iscoroutinefunction(fn) else client.portal.call(_sync, fn, *args)

        async def _sync(fn, *args):
            return fn(*args)

        async def setup_pairs():
            eng.pairs = PAIRS
            await eng_feed.watch(PAIRS["BTC/USD"])
        call(setup_pairs)
        yield client, app, eng, eng_feed, clock, call


def token_of(client):
    html = client.get("/new").text
    return re.search(r'name="oc-token" content="([^"]+)"', html).group(1)


# ---------- security guard (LOCALHOST-CSRF) ----------

def test_refuses_a_foreign_host(setup):
    client = setup[0]
    r = client.get("/api/state", headers={"Host": "evil.example:5180"})
    assert (r.status_code, r.text) == (403, "Refused: unknown Host.")
    r = client.get("/new", headers={"Host": "127.0.0.1:8080"})
    assert r.status_code == 403
    assert client.get("/api/state", headers={"Host": "localhost:5180"}).status_code == 200


def test_refuses_a_state_change_without_our_origin(setup):
    client = setup[0]
    tok = token_of(client)
    r = client.post("/api/chase/stop", headers={"X-Session-Token": tok, "Origin": "http://evil.example"})
    assert (r.status_code, r.text) == (403, "Refused: the Origin is not this tool.")
    r = client.post("/api/chase/stop", headers={"X-Session-Token": tok})
    assert r.status_code == 403
    # With our Origin and token the request passes the guard (409: no chase runs).
    assert client.post("/api/chase/stop", headers={"X-Session-Token": tok, **ORIGIN}).status_code == 409


def test_refuses_a_state_change_without_the_session_token(setup):
    client = setup[0]
    r = client.post("/api/chase", headers=ORIGIN, json={})
    assert (r.status_code, r.text) == (403, "Refused: no valid session token.")
    r = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": "guess"}, json={})
    assert r.status_code == 403


def test_the_token_is_only_in_the_served_page(setup):
    client = setup[0]
    tok = token_of(client)
    assert len(tok) >= 32
    assert tok not in client.get("/api/state").text
    assert tok not in client.get("/static/app.js").text


# ---------- restart: an unfinished chase ends, it is not resumed ----------

def test_unfinished_chase_in_sqlite_becomes_ended_on_server_start(tmp_path):
    pair = PAIRS["BTC/USD"]
    c, _ = core.begin("oc-old", pair, "buy", D("0.05"), D("62417.9"), D("62418.5"), 120, 500.0, core.SIM_VENUE)
    c, _ = core.step(c, core.Placed(500.0))
    c, _ = core.step(c, core.Filled(531.0, D("0.018"), D("62417.9"), True, cum=D("0.018")))
    db = Db(tmp_path)
    db.save(c, "dry", 562.0)
    db.cx.close()
    guard = create_app(tmp_path, connect=False, clock=lambda: 570.0, latency=0)
    with TestClient(guard, base_url=BASE) as client:
        got = client.get("/api/chase/oc-old").json()
    assert (got["phase"], got["outcome"], got["filled"]) == ("done", "ended", "0.018")
    assert [e["text"] for e in got["events"]] == [
        "Tool started again after 8 s off. Ended the simulated order. No order was on Kraken.",
        "Recorded the simulated fills: 0.0180 of 0.0500 BTC.", "Did not continue the dry run."]


def test_the_tool_records_when_it_last_ran_during_a_chase(setup):
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    call(f._handle, FakeBook().msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    cid = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": tok},
                      json={"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 120}).json()["id"]
    clock.t += 30
    call(eng.tick)
    row = eng.db.cx.execute("select updated from chase where id = ?", (cid,)).fetchone()
    assert row["updated"] == clock.t


def test_a_second_tool_on_the_same_data_folder_is_refused(tmp_path):
    first = lock_folder(tmp_path)
    with pytest.raises(BlockingIOError):
        lock_folder(tmp_path)
    first.close()
    lock_folder(tmp_path).close()       # free again once the first tool stops


# ---------- book checksum on recorded Kraken data ----------

def recorded_books():
    return [json.loads(line, parse_float=D) for line in FIXTURE.read_text().splitlines() if '"channel":"book"' in line]


def test_checksum_matches_every_recorded_kraken_book_message():
    book = OrderBook(1, 8)
    results = [book.apply(m["data"][0], m["type"] == "snapshot") for m in recorded_books()]
    assert len(results) > 250 and all(results)


def test_checksum_mismatch_resubscribes_and_keeps_the_book_stale_until_a_snapshot(setup):
    client, app, eng, f, clock, call = setup
    msgs = recorded_books()
    call(f._handle, msgs[0])
    assert eng.book_ok is True
    bad = json.loads(json.dumps(msgs[1], default=str), parse_float=D)
    bad["data"][0]["checksum"] = 12345
    f.ws.sent.clear()
    call(f._handle, bad)
    assert eng.book_ok is False
    assert [(m["method"], m["params"]["channel"]) for m in f.ws.sent] == [("unsubscribe", "book"), ("subscribe", "book")]
    call(f._handle, msgs[2])           # an update for the old book: ignored, still stale
    assert eng.book_ok is False
    call(f._handle, msgs[0])           # a new snapshot: valid again
    assert eng.book_ok is True


# ---------- end to end: one dry run on a fake public feed ----------

def fmt(v, d):
    return f"{D(v):.{d}f}".replace(".", "").lstrip("0")


class FakeBook:
    """The fake exchange side of the book: it writes v2 messages with the checksum of its full book,
    from the documented algorithm."""

    def __init__(self):
        self.bids, self.asks = {}, {}

    def msg(self, kind, bids, asks):
        if kind == "snapshot":
            self.bids, self.asks = {}, {}
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for p, q in levels:
                side.pop(D(p), None) if D(q) == 0 else side.__setitem__(D(p), D(q))
        s = "".join(fmt(p, 1) + fmt(q, 8) for p, q in sorted(self.asks.items())[:10])
        s += "".join(fmt(p, 1) + fmt(q, 8) for p, q in sorted(self.bids.items(), reverse=True)[:10])
        return {"channel": "book", "type": kind, "data": [{
            "symbol": "BTC/USD", "checksum": zlib.crc32(s.encode()),
            "bids": [{"price": D(p), "qty": D(q)} for p, q in bids], "asks": [{"price": D(p), "qty": D(q)} for p, q in asks]}]}


def book_msg(kind, bids, asks, book=None):
    return (book or FakeBook()).msg(kind, bids, asks)


def trade_msg(side, price, qty):
    return {"channel": "trade", "type": "update", "data": [{"symbol": "BTC/USD", "side": side, "price": D(price), "qty": D(qty)}]}


def test_one_dry_run_end_to_end_on_a_fake_feed(setup):
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    H = {**ORIGIN, "X-Session-Token": tok}
    kb = FakeBook()
    call(f._handle, kb.msg("snapshot", [("62417.9", "1.0"), ("62417.0", "2.0")],
                           [("62418.5", "0.01"), ("62418.6", "0.02"), ("62419.0", "3.0")]))

    r = client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 60})
    assert r.status_code == 200
    cid = r.json()["id"]
    # One chase at a time.
    r2 = client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 60})
    assert r2.json() == {"errors": ["A chase runs now. You can start a new chase when it ends."]}

    clock.t += 6
    call(f._handle, kb.msg("update", [("62418.1", "0.5")], []))          # the bid rises: amend
    clock.t += 1
    call(f._handle, trade_msg("buy", "62418.0", "1"))                       # buy side: no fill
    call(f._handle, trade_msg("sell", "62418.1", "1"))                      # at our price: no fill
    call(f._handle, trade_msg("sell", "62418.0", "0.018"))                  # through our price: fill
    mid = client.get("/api/state").json()["chase"]
    assert (mid["phase"], mid["price"], mid["filled"]) == ("resting", "62418.1", "0.018")

    clock.t += 60
    call(f._handle, {"channel": "heartbeat"})                                # no book change: the book is still valid
    call(eng.tick)                                                           # timeout: fallback
    c = client.get(f"/api/chase/{cid}").json()
    texts = [e["text"] for e in c["events"]]
    assert texts == [
        "Recorded the start ask, 62,418.50, as the cap.",
        "Placing a post-only buy, 0.0500 BTC at 62,417.90…",
        "Placed a post-only buy, 0.0500 BTC at 62,417.90.",
        "Best bid rose to 62,418.10. Amended the order to 62,418.10.",
        "Filled 0.0180 BTC at 62,418.10 (maker).",
        "Timeout. Cancelling the resting order.",
        "The simulated exchange confirmed the cancel.",
        "Read the filled quantity again: 0.0180 BTC.",
        "Sending an IOC buy, 0.0320 BTC at 62,418.50…",
        "Filled 0.0100 BTC at 62,418.50 (taker, IOC).",
        "IOC buy, 0.0320 BTC at 62,418.50: filled 0.0100 BTC. The ask is 62,418.50.",
    ]
    assert (c["phase"], c["outcome"], c["filled"]) == ("done", "notfilled", "0.028")
    s = c["summary"]
    # Market at the start for 0.028 BTC: 0.028 x 62418.5 x 1.008 = 1761.6997...
    # Chase: 0.018 x 62418.1 x 1.004 + 0.010 x 62418.5 x 1.008 = 1128.0197... + 629.1784... = 1757.1981...
    assert round(D(s["market"]), 2) == D("1761.70")
    assert round(D(s["saving"]), 2) == D("4.50")

    hist = client.get("/api/history").json()
    assert [(h["id"], h["outcome"], h["filled"]) for h in hist] == [(cid, "notfilled", "0.028")]
    # Both chase pages are served, and the result opens from its id.
    assert client.get("/result").status_code == 200
    assert client.get(f"/api/chase/{cid}").json()["id"] == cid


def test_start_is_refused_for_a_pair_that_is_not_online(setup):
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    off = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5",
                                        "pair_decimals": 1, "lot_decimals": 8, "status": "maintenance"}})
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    eng.pairs = off
    r = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": tok},
                    json={"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 120})
    assert r.json() == {"errors": ["Kraken accepts no new chase for BTC/USD now. Pair status: maintenance."]}


def test_a_higher_limit_needs_the_tick_box(setup):
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    body = {"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 120, "limit": "62480.0"}
    H = {**ORIGIN, "X-Session-Token": tok}
    assert client.post("/api/chase", headers=H, json=body).json() == {"errors": ["Accept the extra cost to start."]}
    r = client.post("/api/chase", headers=H, json={**body, "accept_extra": True})
    assert r.status_code == 200
    assert client.get("/api/state").json()["chase"]["limit"] == "62480.0"


@pytest.mark.parametrize("what,limit,error", [("buy", "62400.0", "A higher limit must be at or above the ask now."),
                                               ("sell", "62430.0", "A lower limit must be at or below the bid now.")])
@pytest.mark.parametrize("accept", [None, True])
def test_a_limit_on_the_wrong_side_gets_the_limit_rule_with_or_without_the_tick_box(setup, what, limit, error, accept):
    # LOWER-CAP-WRONG-MESSAGE: a cap below the ask (a floor above the bid) is not an extra cost to accept.
    client, app, eng, f, clock, call = setup
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    body = {"pair": "BTC/USD", "what": what, "qty": "0.05", "timeout": 120, "limit": limit}
    if accept:
        body["accept_extra"] = True
    r = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": token_of(client)}, json=body)
    assert (r.status_code, r.json()) == (400, {"errors": [error]})


def test_a_limit_at_the_price_now_needs_no_tick_box(setup):
    # UX-ZERO-LOSS-ACCEPT-CHECKBOX: a floor at the bid (a cap at the ask) can lose nothing against a market order now.
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    r = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": tok},
                    json={"pair": "BTC/USD", "what": "sell", "qty": "0.05", "timeout": 120, "limit": "62417.9"})
    assert r.status_code == 200, r.json()
    assert client.get("/api/state").json()["chase"]["limit"] == "62417.9"


# ---------- repair round 1 (R13 code review, R14 security review) ----------

def started_book(setup):
    client, app, eng, f, clock, call = setup
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    return {**ORIGIN, "X-Session-Token": token_of(client)}


def raw_post(client, url, headers, text):
    return client.post(url, headers={**headers, "Content-Type": "application/json"}, content=text)


@pytest.mark.parametrize("text, error", [
    ('{"pair":"BTC/USD","what":"buy","qty":"1e999999999","timeout":120}', "Enter the amount as a plain number, such as 0.0500."),
    ('{"pair":"BTC/USD","what":"buy","qty":1e30,"timeout":120}', "Enter the amount as a plain number, such as 0.0500."),
    ('{"pair":"BTC/USD","what":"buy","qty":"1e30","timeout":120}', "Enter the amount as a plain number, such as 0.0500."),
    ('{"pair":"BTC/USD","what":"buy","qty":"0.05","limit":"1e999999999","accept_extra":true,"timeout":120}',
     "Enter the limit as a plain number, such as 62480.0."),
    ('{"pair":"BTC/USD","what":"buy","qty":"0.05","timeout":Infinity}', "Pick a timeout from the list: 30 s to 15 min."),
    ('{"pair":"BTC/USD","what":"buy","qty":"0.05","timeout":1e400}', "Pick a timeout from the list: 30 s to 15 min."),
    ('{"pair":"BTC/USD","what":"buy","qty":NaN,"timeout":120}', "Enter the amount as a plain number, such as 0.0500."),
    ('{"pair":["x"],"what":"buy","qty":"0.05","timeout":120}', "Unknown pair."),
])
def test_bad_numbers_at_the_http_boundary_give_400_with_the_form_copy(setup, text, error):
    # INPUT-500 and HUGE-QTY-500
    H = started_book(setup)
    r = raw_post(setup[0], "/api/chase", H, text)
    assert (r.status_code, r.json()) == (400, {"errors": [error]})


def test_watch_with_a_pair_that_is_not_a_string_gives_400(setup):
    H = started_book(setup)
    r = raw_post(setup[0], "/api/watch", H, '{"pair":["x"]}')
    assert (r.status_code, r.json()) == (400, {"errors": ["Unknown pair."]})


@pytest.mark.parametrize("field, value, error", [
    ("timeout", 30.99, "Pick a timeout from the list: 30 s to 15 min."),
    ("timeout", True, "Pick a timeout from the list: 30 s to 15 min."),
    ("timeout", "120", "Pick a timeout from the list: 30 s to 15 min."),
    ("timeout", 45, "Pick a timeout from the list: 30 s to 15 min."),
    ("limit", "1,0,0,0,0,0,0,0,0", "Enter the limit as a plain number, such as 62480.0."),
    ("limit", "62,480.0", "Enter the limit as a plain number, such as 62480.0."),
    ("qty", True, "Enter the amount as a plain number, such as 0.0500."),
    ("qty", "0x10", "Enter the amount as a plain number, such as 0.0500."),
])
def test_loose_numbers_are_refused(setup, field, value, error):
    # TIMEOUT-LOOSE-PARSE
    H = started_book(setup)
    body = {"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 120, "accept_extra": True, field: value}
    r = setup[0].post("/api/chase", headers=H, json=body)
    assert (r.status_code, r.json()) == (400, {"errors": [error]})


def test_a_json_number_amount_and_a_plain_limit_start_a_chase(setup):
    H = started_book(setup)
    r = setup[0].post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "buy", "qty": 0.05, "timeout": 60,
                                                    "limit": "62480.0", "accept_extra": True})
    assert r.status_code == 200
    assert setup[0].get("/api/state").json()["chase"]["qty"] == "0.05"


def test_a_floor_of_0_does_not_start_a_chase(setup):
    # LIMIT-NOT-POSITIVE at the server
    H = started_book(setup)
    r = setup[0].post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "sell", "qty": "0.05", "timeout": 120,
                                                    "limit": "0", "accept_extra": True})
    assert (r.status_code, r.json()) == (400, {"errors": ["The floor must be above 0."]})


def csp_of(r) -> dict[str, str]:
    return {d.split()[0]: " ".join(d.split()[1:]) for d in r.headers["content-security-policy"].split(";")}


@pytest.mark.parametrize("path", ["/", *(f"/{p}" for p in PAGES), "/api/state", "/static/app.js", "/static/new.js"])
def test_every_response_sends_the_full_csp(setup, path):
    # CLICKJACK-ONE-CLICK, and T4 C5: the full CSP comes before any key exists.
    r = setup[0].get(path, follow_redirects=False)
    assert r.headers["x-frame-options"] == "DENY"
    csp = csp_of(r)
    assert {k: csp.get(k) for k in ("script-src", "connect-src", "frame-ancestors", "object-src", "base-uri")} == {
        "script-src": "'self'", "connect-src": "'self'", "frame-ancestors": "'none'", "object-src": "'none'", "base-uri": "'none'"}
    assert "unsafe-inline" not in csp["script-src"] and "unsafe-eval" not in csp["script-src"]


def test_a_refused_request_also_sends_the_csp(setup):
    r = setup[0].get("/chase", headers={"Host": "evil.example:5180"})
    assert r.status_code == 403 and r.headers["x-frame-options"] == "DENY"
    assert csp_of(r)["script-src"] == "'self'"


class Scripts(HTMLParser):
    """The scripts of a page: the src of each, the inline code, and every inline event handler (onclick=...)."""
    def __init__(self):
        super().__init__()
        self.src, self.inline, self.handlers, self.open = [], [], [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.handlers += [f"{tag} {k}" for k in a if k.startswith("on")]
        if tag == "script":
            self.open = True
            self.src.append(a.get("src"))

    def handle_endtag(self, tag):
        self.open = self.open and tag != "script"

    def handle_data(self, data):
        if self.open and data.strip():
            self.inline.append(data.strip()[:40])


@pytest.mark.parametrize("page", PAGES)
def test_no_page_has_an_inline_script(setup, page):
    # The CSP (script-src 'self') blocks inline code; each page must run from its own script files only.
    client = setup[0]
    s = Scripts()
    s.feed(client.get(f"/{page}").text)
    assert (s.inline, s.handlers) == ([], [])
    assert s.src == ["/static/app.js", f"/static/{page}.js"]
    assert [client.get(src).status_code for src in s.src] == [200, 200]


def test_the_guard_checks_a_websocket_like_a_state_change():
    # GUARD-HTTP-ONLY: a future WebSocket route gets the same Host, Origin and token checks.
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.websockets import WebSocketDisconnect

    from order_chaser.server import Guard

    async def ws(websocket):
        await websocket.accept()
        await websocket.send_text("in")
        await websocket.close()

    client = TestClient(Guard(Starlette(routes=[WebSocketRoute("/ws", ws)]), "tok"), base_url=BASE)
    good = {"Host": "127.0.0.1:5180", "Origin": BASE, "X-Session-Token": "tok"}
    for bad in ({**good, "Host": "evil.example:5180"}, {**good, "Origin": "http://evil.example"},
                {**good, "X-Session-Token": "guess"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers=bad) as s:
                s.receive_text()
    with client.websocket_connect("/ws", headers=good) as s:
        assert s.receive_text() == "in"


def test_the_tool_creates_its_data_folder_0700(tmp_path):
    # DATA-DIR-CHMOD
    folder = tmp_path / "new" / "order-chaser"
    lock_folder(folder).close()
    assert folder.stat().st_mode & 0o777 == 0o700


def test_the_tool_never_changes_the_mode_of_an_existing_folder_and_warns(tmp_path, caplog):
    folder = tmp_path / "Documents"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    lock_folder(folder).close()
    Db(folder)
    assert folder.stat().st_mode & 0o777 == 0o755
    assert [r.getMessage() for r in caplog.records] == [
        f"The data folder {folder} is readable by other users (mode 755). The tool did not change it."]


# ---------- repair round 2 (R18 security review, R20 QA) ----------

@pytest.mark.parametrize("qty", ["١", "０.００１", "0.0٥"])
def test_digits_that_are_not_ascii_are_refused(setup, qty):
    # UNICODE-DIGITS
    H = started_book(setup)
    r = setup[0].post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "buy", "qty": qty, "timeout": 120})
    assert (r.status_code, r.json()) == (400, {"errors": ["Enter the amount as a plain number, such as 0.0500."]})
    assert setup[2].chase is None


def test_a_token_header_that_is_not_ascii_gets_403_with_the_frame_headers(setup):
    # TOKEN-NONASCII-500
    r = setup[0].post("/api/chase/stop", headers={**ORIGIN, "X-Session-Token": "tök".encode("utf-8")})
    assert (r.status_code, r.text) == (403, "Refused: no valid session token.")
    assert r.headers["x-frame-options"] == "DENY"


def test_start_needs_a_book_at_most_10_s_old_and_a_heartbeat_renews_it(setup):
    # STALE-FEED-FREEZE-AFTER-20S: Start and the running chase use one age limit, core.STALE_AFTER.
    client, app, eng, f, clock, call = setup
    H = started_book(setup)
    body = {"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 120}
    clock.t += 10.5
    assert client.get("/api/state").json()["feed"]["fresh"] is False
    assert client.post("/api/chase", headers=H, json=body).json() == {"errors": ["Start needs live prices."]}
    call(f._handle, {"channel": "heartbeat"})
    assert client.get("/api/state").json()["feed"]["fresh"] is True
    assert client.post("/api/chase", headers=H, json=body).status_code == 200


def test_a_stalled_feed_is_lost_10_s_after_its_last_message_not_after_the_close(monkeypatch):
    # STALE-FEED-FREEZE-AFTER-20S: a stalled link answers nothing, also not the close. The feed must report
    # the loss when the silence passes the limit, not after the close handshake times out.
    import time

    import websockets
    monkeypatch.setattr(core, "STALE_AFTER", 0.5)
    events = []

    async def scenario():
        async def handler(ws):
            for _ in range(10_000):
                await ws.send(json.dumps({"channel": "heartbeat"}))
                await asyncio.sleep(0.05)

        stalled = asyncio.Event()

        async def pipe(reader, writer):
            while data := await reader.read(65536):
                if not stalled.is_set():
                    writer.write(data)
                    await writer.drain()

        async def proxy(cr, cw):
            ur, uw = await asyncio.open_connection("127.0.0.1", up_port)
            await asyncio.gather(pipe(cr, uw), pipe(ur, cw), return_exceptions=True)

        async with websockets.serve(handler, "127.0.0.1", 0, close_timeout=0.1) as up:
            up_port = up.sockets[0].getsockname()[1]
            px = await asyncio.start_server(proxy, "127.0.0.1", 0)
            url = f"ws://127.0.0.1:{px.sockets[0].getsockname()[1]}"
            f = feed.PublicFeed(lambda b, ok: None, lambda *a: None, lambda up, *a: events.append((up, time.monotonic())), url)
            task = asyncio.create_task(f.run())
            while not events:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.3)
            stalled.set()
            t_stall = time.monotonic()
            while len(events) < 2:
                await asyncio.sleep(0.01)
            task.cancel()
            px.close()
            return events[1][1] - t_stall

    lost_after = asyncio.run(asyncio.wait_for(scenario(), 15))
    assert events[0][0] is True and events[1][0] is False
    assert 0.4 < lost_after < 0.9          # 0.5 s of silence; the old code added the 10 s close timeout


# ---------- cycle 1 at 8e13b1e ----------

ETH = feed.parse_pairs({"ETH/USD": {"tick_size": "0.01", "ordermin": "0.002", "costmin": "0.5",
                                    "pair_decimals": 2, "lot_decimals": 8, "status": "online"}})


def test_watch_allows_one_pair_change_each_second_and_answers_429_to_more(setup):
    # WATCH-BURST-STUCK-FEED: 40 fast switches made Kraken answer "Exceeded msg rate".
    client, app, eng, f, clock, call = setup
    H = started_book(setup)
    eng.pairs = {**PAIRS, **ETH}
    sent = len(f.ws.sent)
    codes = [client.post("/api/watch", headers=H, json={"pair": p}).status_code for p in ("ETH/USD", "BTC/USD", "ETH/USD")]
    assert codes == [200, 429, 200]                       # the third asks for the pair it watches: no change
    assert client.post("/api/watch", headers=H, json={"pair": "BTC/USD"}).json() == {
        "errors": ["Too many pair changes. Wait 1 s and try again."]}
    assert [m["params"]["symbol"][0] for m in f.ws.sent[sent:] if m["method"] == "subscribe"] == ["ETH/USD", "ETH/USD"]
    time.sleep(1)
    assert client.post("/api/watch", headers=H, json={"pair": "BTC/USD"}).status_code == 200
    assert eng.watched == "BTC/USD"


def test_a_book_message_alone_does_not_write_the_chase(setup):
    # BOOK-AT-SAVES-EVERY-MESSAGE
    client, app, eng, f, clock, call = setup
    kb = FakeBook()
    call(f._handle, kb.msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    H = {**ORIGIN, "X-Session-Token": token_of(client)}
    assert client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "buy", "qty": "0.05", "timeout": 600}).status_code == 200
    saves = []
    save = eng.db.save
    eng.db.save = lambda *a: (saves.append(a[0].phase), save(*a))
    for i in range(20):
        clock.t += 0.05
        call(f._handle, kb.msg("update", [], [("62419.0", str(3 + i))]))     # a deeper ask changes: no amend
    call(f._handle, {"channel": "heartbeat"})
    assert (eng.chase.book_at, saves) == (clock.t, [])                        # valid books, and no write
    call(f._handle, trade_msg("sell", "62417.0", "0.01"))                      # a fill changes the chase: it writes
    assert saves == ["resting"]


def test_the_guard_takes_the_port_the_tool_runs_on(tmp_path):
    # TEST-PORT-CONTENTION: the page tests run the app on a free port.
    guard = create_app(tmp_path, connect=False, port=5199)
    with TestClient(guard, base_url="http://127.0.0.1:5199") as client:
        assert client.get("/api/state").status_code == 200
        assert client.get("/api/state", headers={"Host": "localhost:5199"}).status_code == 200
        assert client.get("/api/state", headers={"Host": "127.0.0.1:5180"}).text == "Refused: unknown Host."


def test_a_refused_subscription_counts_as_feed_lost_and_the_feed_subscribes_again():
    # WATCH-BURST-STUCK-FEED: before, the error reply was ignored and heartbeats kept the link "up" with no book.
    import websockets
    links, subs, conns = [], [], []

    async def scenario():
        async def handler(ws):
            conn = len(conns)
            conns.append(conn)
            with contextlib.suppress(websockets.ConnectionClosed):
                async for raw in ws:
                    m = json.loads(raw)
                    subs.append((conn, m["params"]["channel"]))
                    if conn == 0 and m["params"]["channel"] == "book":
                        await ws.send(json.dumps({"method": "subscribe", "success": False, "error": "Exceeded msg rate"}))
                        for _ in range(100):
                            await ws.send(json.dumps({"channel": "heartbeat"}))
                            await asyncio.sleep(0.05)

        async with websockets.serve(handler, "127.0.0.1", 0) as srv:
            f = feed.PublicFeed(lambda b, ok: None, lambda *a: None, lambda up, attempt, wait: links.append((up, attempt, wait)),
                                f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}")
            await f.watch(PAIRS["BTC/USD"])
            task = asyncio.create_task(f.run())
            while len(links) < 3 or len(subs) < 3:
                await asyncio.sleep(0.01)
            task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 10))
    assert links[:3] == [(True, 0, 0), (False, 1, 2), (True, 1, 0)]     # lost at once, a new link after 2 s
    assert [s for s in subs if s[0] == 1] == [(1, "book"), (1, "trade")]  # the new link subscribes again


def test_the_demo_reaches_the_rest_below_the_minimum(tmp_path):
    # UX3-DEMO-NO-BELOWMIN: without heartbeats the book was older than 10 s at the end, so the demo ended
    # "no valid price" and never showed the rest below the minimum. The demo runs 50 times faster here.
    import importlib.util
    import time
    spec = importlib.util.spec_from_file_location("demo_states", Path(__file__).parent.parent / "scripts" / "demo_states.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    speed, t0 = 50, time.time()
    eng = create_app(tmp_path, connect=False, clock=lambda: t0 + (time.time() - t0) * speed).app.state.engine

    async def run():
        task = asyncio.create_task(demo.drive(eng, speed))
        while not (eng.chase and eng.chase.timeout == 60 and eng.chase.phase == "done"):
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(asyncio.wait_for(run(), 15))
    assert [(c.timeout, c.outcome) for c, _ in eng.db.history()] == [(60, "belowmin"), (120, "notfilled")]


# ---------- margin through the API (simulated account) ----------

MARGIN_BTC = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5", "pair_decimals": 1,
                                           "lot_decimals": 8, "status": "online", "leverage_buy": [2, 3, 4, 5, 6, 7, 8, 9, 10],
                                           "leverage_sell": [2, 3, 4, 5, 6, 7, 8, 9, 10], "margin_call": 80, "margin_stop": 40,
                                           "long_position_limit": 350, "short_position_limit": 250}})


def test_a_margin_open_and_its_close_through_the_api_and_the_account_stays_after_a_restart(setup, tmp_path):
    client, app, eng, f, clock, call = setup
    eng.pairs = MARGIN_BTC
    H = {**ORIGIN, "X-Session-Token": token_of(client)}
    kb = FakeBook()
    call(f._handle, kb.msg("snapshot", [("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.0", "3.0")]))
    post = lambda body: client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "qty": "0.05", "timeout": 60, **body})
    assert post({"what": "long", "leverage": 6}).json() == {"errors": ["Pick a leverage that BTC/USD allows: 2x, 3x, 4x, 5x."]}
    assert post({"what": "close-long"}).json() == {"errors": ["You have no open long position on BTC/USD."]}
    assert post({"what": "long", "leverage": 3}).status_code == 200
    call(f._handle, trade_msg("sell", "62417.0", "0.05"))
    acc = client.get("/api/account").json()
    assert [(p["dir"], p["qty"], p["leverage"]) for p in acc["positions"]] == [("long", "0.05", 3)]
    assert acc["simulated"] is True
    assert post({"what": "short", "leverage": 2}).json() == {"errors": ["Close the long first. You have an open long position on BTC/USD."]}
    assert post({"what": "close-long", "qty": "0.06"}).json()["errors"] == [
        "A close cannot be larger than the position, 0.0500 BTC. Reduce-only orders never grow or flip a position. Enter 0.0500 or less."]
    # The form's close plan (the account's FIFO rule): which positions a size takes, what stays, the level after.
    plan = client.get("/api/plan", params={"pair": "BTC/USD", "dir": "long", "qty": "0.02"}).json()
    assert (plan["takes"].split(" opened ")[0], plan["stays"]) == ("0.0200 BTC of the 3x position", "0.0300 BTC at 3x")
    assert float(plan["level_after"]) > float(plan["level_now"])
    assert [client.get("/api/plan", params=q).status_code for q in ({"pair": "BTC/USD", "dir": "long", "qty": "1e3"},
            {"pair": "BTC/USD", "dir": "short", "qty": "0.02"}, {"pair": "X", "dir": "long", "qty": "0.02"})] == [400, 400, 400]
    # PLAN-ACCEPTS-SIZE-ABOVE-POSITION: the plan refuses a size with the rule and the text of POST /api/chase
    for qty in ("0.06", "0.00001", "0.000000001"):
        got = client.get("/api/plan", params={"pair": "BTC/USD", "dir": "long", "qty": qty})
        assert (got.status_code, got.json()["errors"]) == (400, post({"what": "close-long", "qty": qty}).json()["errors"]), qty
    # PLAN-TEXT-DIFFERS-FROM-POST: a size that is not a plain number has the number check and the text of POST
    for qty in ("-1", "1e-3", "abc"):
        got = client.get("/api/plan", params={"pair": "BTC/USD", "dir": "long", "qty": qty})
        assert (got.status_code, got.json()["errors"]) == (400, ["Enter the amount as a plain number, such as 0.0500."]), qty
        assert post({"what": "close-long", "qty": qty}).json()["errors"] == ["Enter the amount as a plain number, such as 0.0500."], qty
    r = post({"what": "close-long", "qty": "0.02"})
    c = client.get(f"/api/chase/{r.json()['id']}").json()
    assert (c["side"], c["dir"], c["margin"]["close"], [p["qty"] for p in c["margin"]["positions"]]) == ("sell", "long", True, ["0.05"])
    call(f._handle, trade_msg("buy", "62419.0", "0.02"))                  # the reduce-only sell at the ask fills
    assert client.get(f"/api/chase/{c['id']}").json()["outcome"] == "filled"
    # The simulated account is in the SQLite file: a new start of the tool shows the same position.
    guard = create_app(tmp_path, connect=False, clock=clock, latency=0)
    with TestClient(guard, base_url=BASE) as again:
        acc = again.get("/api/account").json()
    assert [(p["dir"], p["qty"]) for p in acc["positions"]] == [("long", "0.03")]


def test_the_account_is_read_at_the_start_after_fills_at_most_every_3_s_and_at_the_end_never_each_second(setup):
    client, app, eng, f, clock, call = setup
    eng.pairs = MARGIN_BTC
    reads = []
    read = eng.gw.read
    eng.gw.read = lambda now: reads.append(now - clock.t0) or read(now)
    clock.t0 = clock.t
    kb = FakeBook()
    call(f._handle, kb.msg("snapshot", [("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.0", "3.0")]))
    assert call(eng.start, "BTC/USD", "long", D("0.05"), None, 60, 3) == []
    for s in range(1, 21):                                                  # 20 s, a tick each 0.5 s
        clock.t += 0.5
        if s in (2, 3, 4):
            call(f._handle, trade_msg("sell", "62417.0", "0.01"))          # three fills in 1.5 s
        call(eng.tick)
    call(eng.user, "stop")
    assert reads == [0.0, 3.0, 10.0]                                        # start, one read for the fills, the end


@pytest.mark.parametrize("body, error", [
    ({"what": "buy", "leverage": 3}, "Leverage is only for an open long or an open short. Leave it out."),
    ({"what": "close-long", "leverage": 5}, "Leverage is only for an open long or an open short. Leave it out."),
    ({"what": "buy", "side": "long", "leverage": 3}, 'Send what to do in the field "what" only, not "side".'),
    ({"side": "long", "leverage": 3}, 'Send what to do in the field "what" only, not "side".'),
    ({"side": "buy"}, 'Send what to do in the field "what" only, not "side".'),
    ({"what": "long"}, "Pick a leverage for the open: 2x to 5x."),
    ({"what": "short", "leverage": "3"}, "Pick a leverage for the open: 2x to 5x."),
    ({"what": "long", "leverage": True}, "Pick a leverage for the open: 2x to 5x."),
    ({"what": ["long"], "leverage": 3}, "Pick what to do: buy, sell, open long, open short or close a position."),
])
def test_start_takes_one_action_field_and_leverage_only_for_an_open(setup, body, error):
    # MIXED-SPOT-MARGIN-FIELDS: a mix of fields is refused, never run as something else.
    client, app, eng, f, clock, call = setup
    eng.pairs = MARGIN_BTC
    call(f._handle, FakeBook().msg("snapshot", [("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.0", "3.0")]))
    r = client.post("/api/chase", headers={**ORIGIN, "X-Session-Token": token_of(client)},
                    json={"pair": "BTC/USD", "qty": "0.05", "timeout": 60, **body})
    assert (r.status_code, r.json()) == (400, {"errors": [error]})
    assert eng.chase is None


def test_the_dry_run_has_no_code_that_reads_a_key_or_calls_a_private_endpoint():
    # DRY-RUN-READS-KEY (PE): no Kraken key and no private request in this version.
    from pathlib import Path
    src = " ".join(f.read_text() for f in (Path(__file__).parent.parent / "src" / "order_chaser").glob("*.py"))
    assert [w for w in ("API-Key", "API-Sign", "/0/private", "KRAKEN_KEY", "KRAKEN_API") if w in src] == []


def test_an_open_against_a_short_while_its_chase_runs_says_a_chase_runs_first(setup):
    # OPPOSE-MSG-WHILE-CHASE-RUNS: "a chase runs" comes before every check of the open.
    client, app, eng, f, clock, call = setup
    eng.pairs = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5", "pair_decimals": 1,
                                              "lot_decimals": 8, "status": "online", "leverage_buy": [2, 3, 4, 5],
                                              "leverage_sell": [2, 3, 4, 5]}})
    H = started_book(setup)
    assert client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "what": "short", "qty": "0.02", "leverage": 2,
                                                      "timeout": 120}).status_code == 200
    call(f._handle, trade_msg("buy", "62419.0", "0.01"))                   # a part fill: a short of 0.01 is open
    assert eng.gw.account.position("BTC/USD", "short")["qty"] == D("0.01")
    long = {"pair": "BTC/USD", "what": "long", "qty": "0.01", "leverage": 2, "timeout": 120}
    r = client.post("/api/chase", headers=H, json=long)
    assert (r.status_code, r.json()) == (400, {"errors": ["A chase runs now. You can start a new chase when it ends."]})
    assert client.post("/api/chase/stop", headers=H).status_code == 200
    r = client.post("/api/chase", headers=H, json=long)
    assert (r.status_code, r.json()) == (400, {"errors": ["Close the short first. You have an open short position on BTC/USD."]})


# ---------- the gateway call is async (T4 U1): a slow gateway does not stop the engine ----------

def test_a_slow_gateway_call_does_not_block_and_the_commands_go_out_in_order(setup):
    client, app, eng, f, clock, call = setup
    started_book(setup)
    sim, sent, gate = eng.gw, [], asyncio.Event()

    async def slow_send(cmd, now):   # the network: each call waits until the test opens the gate
        sent.append(type(cmd).__name__)
        await gate.wait()
        return sim.answer(cmd, now)
    eng.gw = type("Slow", (), {"send": staticmethod(slow_send), "account": sim.account, "read": sim.read})()

    assert call(eng.start, "BTC/USD", "buy", D("0.05"), None, 120) == []
    assert (eng.chase.phase, sent) == ("placing", ["Place"])          # start returned while the Place waits
    assert call(eng.user, "stop") is True                              # the user still reaches the core
    assert sent == ["Place"]                                           # one call at a time: the next command waits

    async def open_gate():
        gate.set()
        while eng.draining:
            await asyncio.sleep(0.01)
    call(open_gate)
    assert sent[:2] == ["Place", "Cancel"]
    assert (eng.chase.phase, eng.chase.outcome) == ("done", "stopped")
