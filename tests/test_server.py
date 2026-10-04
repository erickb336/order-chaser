"""Tests of the shell: security guard, restart rule, book checksum on recorded Kraken data, and one
end-to-end dry run driven by a fake public feed. No network."""
import asyncio
import json
import re
import zlib
from decimal import Decimal as D
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from order_chaser import core, feed
from order_chaser.book import OrderBook
from order_chaser.db import Db
from order_chaser.server import create_app

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
    c, _ = core.begin("oc-old", pair, "buy", D("0.05"), D("62417.9"), D("62418.5"), 120, 500.0, "the simulation")
    c, _ = core.step(c, core.Placed(500.0))
    c, _ = core.step(c, core.Filled(531.0, D("0.018"), D("62417.9"), True))
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

    r = client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "side": "buy", "qty": "0.05", "timeout": 60})
    assert r.status_code == 200
    cid = r.json()["id"]
    # One chase at a time.
    r2 = client.post("/api/chase", headers=H, json={"pair": "BTC/USD", "side": "buy", "qty": "0.05", "timeout": 60})
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
    call(eng.tick)                                                           # timeout: fallback
    c = client.get(f"/api/chase/{cid}").json()
    texts = [e["text"] for e in c["events"]]
    assert texts == [
        "Recorded the start ask, 62,418.50, as the cap.",
        "Sending a post-only buy, 0.0500 BTC at 62,417.90…",
        "Placed a post-only buy, 0.0500 BTC at 62,417.90.",
        "Best bid rose to 62,418.10. Amended the order to 62,418.10.",
        "Filled 0.0180 BTC at 62,418.10 (maker).",
        "Timeout. Cancelling the resting order.",
        "The simulation confirmed the cancel.",
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
                    json={"pair": "BTC/USD", "side": "buy", "qty": "0.05", "timeout": 120})
    assert r.json() == {"errors": ["Kraken accepts no new chase for BTC/USD now. Pair status: maintenance."]}


def test_a_higher_limit_needs_the_tick_box(setup):
    client, app, eng, f, clock, call = setup
    tok = token_of(client)
    call(f._handle, book_msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    body = {"pair": "BTC/USD", "side": "buy", "qty": "0.05", "timeout": 120, "limit": "62480.0"}
    H = {**ORIGIN, "X-Session-Token": tok}
    assert client.post("/api/chase", headers=H, json=body).json() == {"errors": ["Accept the extra cost to start."]}
    r = client.post("/api/chase", headers=H, json={**body, "accept_extra": True})
    assert r.status_code == 200
    assert client.get("/api/state").json()["chase"]["limit"] == "62480.0"
