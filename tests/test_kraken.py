"""Tests of the live side (T4 U4, U5) against a LOCAL fake Kraken (tests/fake_kraken.py): REST and WebSocket v2 on
127.0.0.1. Sample data only: no test sends a request to Kraken, and no test uses a real key."""
import asyncio
import json
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from fake_kraken import API, SECRET, FakeKraken
from order_chaser import core, feed, keys, rest
from order_chaser.kraken import PrivateFeed, exec_events

FIXTURE = Path(__file__).parent / "fixtures" / "kraken-executions.json"
PAIR = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5", "pair_decimals": 1,
                                     "lot_decimals": 8, "status": "online", "altname": "XBTUSD"}})["BTC/USD"]
KEY = keys.Key(API, SECRET)
ID = "ocfix00000001"


@pytest.fixture
def fake():
    k = FakeKraken(time.time)
    yield k
    k.stop()


def chase() -> core.Chase:
    c, _ = core.begin(ID, PAIR, "buy", D("0.05"), D("62400.0"), D("62420.0"), 120, 1000.0, core.LIVE_VENUE)
    return c


async def until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


def private_feed(fake, http, on_exec, on_link, **kw) -> PrivateFeed:
    r = rest.KrakenRest(KEY, http, url=fake.url)

    async def token():
        return (await r.call("GetWebSocketsToken"))["token"]
    return PrivateFeed(token, on_exec, on_link, fake.ws_url, **kw)


# ---------- U4: the private executions feed ----------

def test_the_fixture_replayed_through_the_fake_kraken_gives_the_core_events_of_the_chase(fake):
    msgs = json.loads(FIXTURE.read_text())["messages"]
    got, links = [], []
    c = chase()

    async def run():
        async with httpx.AsyncClient() as http:
            f = private_feed(fake, http, lambda kind, items: got.extend(exec_events(c, kind, items, 5.0)),
                             lambda *a: links.append(a))
            task = asyncio.create_task(f.run())
            await until(lambda: f.ws is not None)
            for m in msgs:
                await asyncio.to_thread(fake.run, fake.push, m["type"], m["data"])
            await until(lambda: len(got) >= 5)
            await asyncio.sleep(0.1)
            task.cancel()
    asyncio.run(run())

    assert got == [
        core.Filled(5.0, D("0.02"), D("62400.0"), True, "chase", D("0.02"), ID),     # the other bot's trade is left out
        core.Filled(5.0, D("0.01"), D("62400.0"), True, "chase", D("0.03"), ID),
        core.OrderState(5.0, True, D("0.035"), D("62405.5"), ID),                     # snapshot: the cum after a gap
        core.OrderState(5.0, False, D("0.035"), D("62405.5"), ID),                    # a cancel the tool did not ask for
        core.Filled(5.0, D("0.01"), D("62418.5"), False, "ioc", D("0.01"), ID + "-i"),  # the IOC's cancel: left out
    ]
    assert links == [(True, 0, 0)]
    assert fake.calls_of("GetWebSocketsToken") == [{}]
    assert fake.calls_of("subscribe") == [{"channel": "executions", "snap_orders": True, "snap_trades": True}]


def test_a_cancel_that_the_tool_asked_for_is_no_event_and_the_core_counts_each_fill_once():
    c = chase()
    cancel = {"exec_type": "canceled", "cl_ord_id": ID, "order_status": "canceled", "cum_qty": 0.01, "limit_price": 62400.0}
    assert exec_events(c, "update", [cancel], 5.0, asked={ID}) == []
    assert exec_events(c, "update", [cancel], 5.0) == [core.OrderState(5.0, False, D("0.01"), D("62400.0"), ID)]

    c, _ = core.step(c, core.Placed(1001.0))
    trade = {"exec_type": "trade", "cl_ord_id": ID, "last_qty": 0.02, "last_price": 62400.0, "liquidity_ind": "m",
             "cum_qty": 0.02}
    snap = {"exec_type": "new", "cl_ord_id": ID, "order_status": "partially_filled", "cum_qty": 0.02, "limit_price": 62400.0}
    # The same fill as a trade, again in a snapshot after a reconnect, and the trade again: counted once.
    for kind, x in (("update", trade), ("snapshot", snap), ("snapshot", trade)):
        for ev in exec_events(c, kind, [x], 1002.0):
            c, _ = core.step(c, ev)
    assert c.filled == D("0.02")


def test_a_lost_link_subscribes_again_with_a_new_token_and_the_backoff_restarts_only_after_a_stable_link(fake):
    fake.r_AddOrder({"cl_ord_id": ID, "type": "buy", "volume": "0.05", "price": "62400.0", "pair": "XBTUSD", "oflags": "post"})
    t = [0.0]
    links, snaps = [], []

    async def short_sleep(s):
        await asyncio.sleep(0.3)

    def on_exec(kind, items):
        if kind == "snapshot":
            snaps.append(exec_events(chase(), kind, items, 0.0))

    async def drop_and_wait(n):
        await asyncio.to_thread(fake.run, fake.drop_ws)
        await until(lambda: len(links) >= n)

    async def run():
        async with httpx.AsyncClient() as http:
            f = private_feed(fake, http, on_exec, lambda *a: links.append(a), clock=lambda: t[0], sleep=short_sleep)
            task = asyncio.create_task(f.run())
            await until(lambda: len(links) == 1)
            await drop_and_wait(3)          # dropped at once: attempt 1
            await drop_and_wait(5)          # dropped at once again: attempt 2, no reset
            t[0] += 10
            await drop_and_wait(7)          # up 10 s: the backoff starts again at attempt 1
            fake.refuse_subscribe = "EGeneral:Permission denied"
            await drop_and_wait(8)          # the drop: attempt 2
            await until(lambda: len(links) >= 9)   # Kraken refused: attempt 3, never "up"
            fake.refuse_subscribe = None
            await until(lambda: links[-1][0] is True)
            task.cancel()
    asyncio.run(run())

    assert links[:9] == [(True, 0, 0), (False, 1, 2), (True, 1, 0), (False, 2, 4), (True, 2, 0),
                         (False, 1, 2), (True, 1, 0), (False, 2, 4), (False, 3, 8)]
    assert links[-1][0] is True and links[-1][1] >= 3
    ups = sum(1 for x in links if x[0])
    assert len(fake.calls_of("GetWebSocketsToken")) == len(fake.calls_of("subscribe"))   # a new token for each link
    assert snaps[0] == snaps[-1] == [core.OrderState(0.0, True, D("0"), D("62400.0"), ID)]   # each subscribe: the snapshot
    assert len(snaps) == ups


# ---------- U5: the Kraken gateway, end to end against the fake Kraken ----------

AWAKE = ["/bin/sh", "-c", "sleep 30", "sh"]   # stands in for caffeinate -i (it gets "-w <pid>" as $1 $2)


@pytest.fixture
def live(tmp_path, fake):
    with live_app(tmp_path, fake) as got:
        yield got


@__import__("contextlib").contextmanager
def live_app(tmp_path, fake):
    from starlette.testclient import TestClient
    from test_server import BASE, Clock, FakeWs
    from order_chaser.server import create_app
    clock = Clock()
    fake.clock = clock
    keys.KeyStore().save(KEY)        # the in-memory keyring of conftest.py: a dummy key
    guard = create_app(tmp_path, connect=False, clock=clock, latency=0, kraken_url=fake.url, kraken_ws=fake.ws_url,
                       awake_cmd=AWAKE)
    with TestClient(guard, base_url=BASE) as client:
        eng = guard.app.state.engine
        f = feed.PublicFeed(eng.on_book, eng.on_trade, eng.on_link)
        f.ws = FakeWs()
        eng.feed = f

        def call(fn, *args):
            async def sync():
                return fn(*args)
            return client.portal.call(fn, *args) if asyncio.iscoroutinefunction(fn) else client.portal.call(sync)

        async def pairs():
            eng.pairs = {"BTC/USD": PAIR}
            await f.watch(PAIR)
        call(pairs)
        yield eng, f, clock, call, client


def wait_for(cond, timeout: float = 8.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def book(f, call, kind, bids, asks, kb=[]):
    from test_server import FakeBook
    if kind == "snapshot":
        kb[:] = [FakeBook()]
    call(f._handle, kb[0].msg(kind, bids, asks))


def texts(eng):
    return [e["text"] for e in eng.db.events(eng.chase.id)]


def test_a_live_chase_end_to_end_timer_post_only_amend_fill_timeout_ioc_and_the_end(live, fake):
    eng, f, clock, call, _ = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.0", "3.0")])
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 60) == []
    cid = eng.chase.id
    awake = eng.kgw.awake
    assert awake.poll() is None                                              # caffeinate runs
    wait_for(lambda: eng.chase.phase == "resting")
    assert [m for m, _ in fake.calls][:4] == ["GetWebSocketsToken", "subscribe", "CancelAllOrdersAfter", "AddOrder"]
    assert fake.calls_of("AddOrder") == [{"ordertype": "limit", "type": "buy", "volume": "0.05", "price": "62417.9",
                                          "pair": "XBTUSD", "cl_ord_id": cid, "oflags": "post"}]

    clock.t += 6
    book(f, call, "update", [("62418.1", "0.5")], [])                        # the bid rises: amend_order on WS v2
    wait_for(lambda: eng.chase.price == D("62418.1"))
    assert fake.calls_of("amend_order") == [{"cl_ord_id": cid, "limit_price": 62418.1, "post_only": True}]

    fake.run(fake.fill, cid, "0.018")                                        # a fill on the executions feed
    wait_for(lambda: eng.chase.filled == D("0.018"))

    clock.t += 60
    book(f, call, "update", [], [])                                          # a valid book at the timeout
    call(eng.tick)                                                           # timeout: cancel, reread, IOC at the cap
    wait_for(lambda: eng.chase.phase == "done")
    wait_for(lambda: awake.poll() is not None and eng.kgw.task is None)      # caffeinate and the feed stopped
    c = eng.chase
    assert (c.outcome, c.filled) == ("notfilled", D("0.028"))                # the IOC got the 0.01 at the ask
    assert fake.calls_of("CancelOrder") == [{"cl_ord_id": cid}]
    assert fake.calls_of("AddOrder")[1] == {"ordertype": "limit", "type": "buy", "volume": "0.032", "price": "62418.5",
                                            "pair": "XBTUSD", "cl_ord_id": cid + "-i", "timeinforce": "IOC"}
    timer = fake.calls_of("CancelAllOrdersAfter")
    assert (timer[0], timer[-1]) == ({"timeout": "60"}, {"timeout": "0"})   # set first, 0 at the end
    assert {"timeout": "60"} in timer[1:-1]                                  # renewed (20 s passed)
    assert eng.db.get(cid)[1] == "live"


def test_the_safety_timer_fires_when_it_is_not_renewed_and_the_chase_ends_with_no_ioc(live, fake, monkeypatch):
    from order_chaser import kraken
    monkeypatch.setattr(kraken, "RETRY_EVERY", 0.05)
    eng, f, clock, call, _ = live
    fake.add_other("limit", "XBTUSD", "sell 1 XBTUSD @ limit 70000")
    fake.add_other("take-profit", "XBTUSD", "sell 0.1 XBTUSD @ take profit 75000")
    fake.add_other("stop-loss", "XBTUSD", "sell 0.1 XBTUSD @ stop loss 58000")
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    wait_for(lambda: eng.chase.phase == "resting")
    fake.rest_down = True                                                    # no renewal reaches Kraken
    for _ in range(4):
        clock.t += 20
        call(eng.tick)
    fake.run(fake.fire_timer)                                                # Kraken's cancel-all at the deadline
    wait_for(lambda: eng.chase.phase == "done")
    assert eng.chase.outcome == "timer"
    assert texts(eng)[-1] == "Kraken cancelled the order: the safety timer fired. Filled 0.0000 BTC. Chase ended. No IOC sent."
    wait_for(lambda: not eng.kgw.renewing)                                   # no renewal in flight (it would set a timer)
    fake.rest_down = False
    wait_for(lambda: eng.kgw.task is None)
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}]     # no "0" after the timer fired
    assert len(fake.calls_of("AddOrder")) == 1                               # no IOC
    wait_for(lambda: eng.chase.others_cancelled is not None)                 # C8: read when Kraken answers again
    assert [o[:2] for o in eng.chase.others_cancelled] == [("take-profit", "XBTUSD"), ("stop-loss", "XBTUSD"),
                                                           ("limit", "XBTUSD")]   # stop-loss and take-profit first
    assert texts(eng)[-1] == ("Kraken cancelled 3 other orders of the account: sell 0.1 XBTUSD @ take profit 75000; "
                              "sell 0.1 XBTUSD @ stop loss 58000; sell 1 XBTUSD @ limit 70000. The tool does not "
                              "place them again.")
    assert len(fake.calls_of("AddOrder")) == 1                               # never placed again (G19 P3)


def test_a_lost_private_feed_reads_the_order_by_rest_every_5_s_and_pauses_amends(live, fake):
    eng, f, clock, call, _ = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    cid = eng.chase.id
    wait_for(lambda: eng.chase.phase == "resting")
    fake.ws_down = True
    fake.run(fake.drop_ws)
    wait_for(lambda: not eng.chase.pfeed_ok)
    fake.run(fake.fill, cid, "0.02", None, False)                            # a fill while the feed is down
    clock.t += 6
    book(f, call, "update", [("62418.1", "0.5")], [])                        # the bid rises: no amend now
    call(eng.tick)                                                           # 5 s: QueryOrders by the txid
    wait_for(lambda: eng.chase.filled == D("0.02"))
    assert fake.calls_of("amend_order") == []
    assert fake.calls_of("QueryOrders") == [{"txid": "OFAKE01-AAAAA-BBBBBB"}]
    clock.t += 2
    call(eng.tick)                                                           # not 5 s yet: no read
    assert len(fake.calls_of("QueryOrders")) == 1

    fake.ws_down = False                                                     # the feed comes back (2 s backoff)
    wait_for(lambda: eng.chase.pfeed_ok)
    clock.t += 5
    book(f, call, "update", [("62418.2", "0.5")], [])                        # amends go on
    wait_for(lambda: len(fake.calls_of("amend_order")) >= 1)                 # at PrivateBack or at this book
    assert eng.chase.filled == D("0.02")                                     # the reread after the gap counts nothing twice
    assert "Private feed lost. Reading the order by REST every 5 s. Amends paused." in texts(eng)
    assert "The private feed is back. Amends go on." in texts(eng)


def test_the_gateway_maps_margin_orders_a_refused_margin_amend_and_an_unknown_order(fake, tmp_path):
    c, cmds = core.begin(ID, PAIR, "buy", D("0.05"), D("62417.9"), D("62418.5"), 120, 1000.0, core.LIVE_VENUE,
                         margin=core.Margin(3))
    fake.refuse_amend = "EOrder:Invalid arguments"
    from order_chaser.kraken import KrakenGateway

    async def run():
        async with httpx.AsyncClient() as http:
            gw = KrakenGateway(rest.KrakenRest(KEY, http, url=fake.url), lambda: KEY, fake.ws_url, lambda: 1000.0, AWAKE)
            gw.chase = c
            await gw.open()
            place = [x for x in cmds if isinstance(x, core.MarginPlace)][0]
            placed = await gw.send(place, 1000.0)
            amended = await gw.send(core.Amend(ID, D("62418.0")), 1001.0)
            unknown = await gw.send(core.Query("ocnever0000001"), 1002.0)
            await gw.send(core.Cancel(ID), 1003.0)
            await gw.close()
            return placed, amended, unknown
    placed, amended, unknown = asyncio.run(run())
    assert placed == [core.Placed(1000.0)]
    assert fake.calls_of("AddOrder")[0] | {} == {"ordertype": "limit", "type": "buy", "volume": "0.05", "price": "62417.9",
                                                 "pair": "XBTUSD", "cl_ord_id": ID, "oflags": "post", "leverage": "3"}
    assert amended == [core.Rejected(1000.0, "amend", "EOrder:Invalid arguments")]
    assert unknown == [core.OrderState(1000.0, False, D(0), None, "ocnever0000001")]
    # The core takes the refusal of a margin amend: cancel and replace for the rest of the chase.
    c, _ = core.step(c, core.Placed(1000.0))
    c = __import__("dataclasses").replace(c, phase="amending", pending=D("62418.0"))
    c, out = core.step(c, amended[0])
    assert c.replace is True
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}, {"timeout": "0"}]


def test_kraken_not_answering_ends_the_chase_in_a_clear_state_keeps_the_timer_and_the_queue_goes_on(live, fake, monkeypatch):
    """READ-RETRY-STALLS-QUEUE: a read that gets no answer for 60 s (chase clock) ends the chase; the safety timer
    stays as the backstop (no "0"), and the command queue still sends the commands of the next chase."""
    from order_chaser import kraken
    monkeypatch.setattr(kraken, "RETRY_EVERY", 0.05)
    eng, f, clock, call, _ = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    wait_for(lambda: eng.chase.phase == "resting")
    fake.rest_down = True
    assert call(eng.user, "stop") is None                                    # Cancel: no answer, then reads by cl_ord_id
    wait_for(lambda: len(fake.calls_of("AddOrder")) == 1 and eng.chase.phase == "cancelling")

    def later():                                                             # 60 s with no answer (each read try)
        clock.t += 61
        return eng.chase.phase == "done"
    wait_for(later)
    assert eng.chase.outcome == "noanswer"
    assert texts(eng)[-1] == ("Kraken did not answer for 60 s. The chase ended. The safety timer cancels the order "
                              "on Kraken within 60 s. Check Kraken Pro for fills.")
    wait_for(lambda: eng.kgw.task is None)
    fake.rest_down = False
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}]     # the timer stays: no "0"
    assert not eng.draining and not eng.queue
    book(f, call, "update", [], [])
    assert call(eng.start, "BTC/USD", "buy", D("0.01"), None, 60) == []      # the next chase (a dry run) places its order
    wait_for(lambda: eng.chase.phase == "resting")


def test_a_stop_of_the_tool_during_a_live_chase_says_that_the_timer_cancels_all_orders(tmp_path, fake, caplog):
    """STOP-KEEPS-TIMER-UNSAID: the shutdown log line and the next start say it."""
    with live_app(tmp_path, fake) as (eng, f, clock, call, _):
        book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
        assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        wait_for(lambda: eng.chase.phase == "resting")
        cid = eng.chase.id
    assert ("The tool stopped during a live chase. The safety timer stays on: within 60 s Kraken cancels ALL open "
            "orders on this account, also stop-loss and take-profit orders.") in caplog.messages
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}]     # no "0": the timer is the backstop
    with live_app(tmp_path, fake) as (eng, *_):
        events = [e["text"] for e in eng.db.events(cid)]
    assert any("The safety timer of the live chase stayed on" in t for t in events), events


# ---------- U6: Setup (Q10, G19), the tested key, the live check (Q6, Q7, Q9) and the live start ----------

def post(client, path, body=None):
    from test_server import ORIGIN, token_of
    return client.post(path, json=body or {}, headers={**ORIGIN, "X-Session-Token": token_of(client)})


LIVE = {"pair": "BTC/USD", "what": "buy", "qty": "0.00005", "timeout": 60, "mode": "live"}


def test_a_live_start_is_refused_until_setup_is_complete_and_sends_nothing_to_kraken(live, fake):
    eng, f, clock, call, client = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    r = post(client, "/api/chase", LIVE)
    assert (r.status_code, r.json()["errors"]) == (400, [
        "Answer Setup, step 2: does another bot or API tool use this Kraken account?",
        "Save a Kraken API key in Setup, step 3."])
    post(client, "/api/setup", {"account": "yes"})                           # G19 P1: another tool, no sub-account
    assert post(client, "/api/chase", LIVE).json()["errors"][0] == (
        "Setup, step 2: use a Kraken sub-account for this tool. Live chases stay off until you confirm it.")
    assert post(client, "/api/setup", {"account": "maybe"}).status_code == 400
    assert fake.calls == []                                                   # no call reached Kraken
    assert client.get("/api/setup").json()["host"] == "127.0.0.1:5180"


def test_setup_then_the_live_check_lists_other_orders_and_the_start_needs_its_ticks(live, fake, memory_keyring):
    eng, f, clock, call, client = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    fake.add_other("limit", "XBTUSD", "sell 1 XBTUSD @ limit 70000")
    fake.add_other("stop-loss", "XBTUSD", "sell 0.1 XBTUSD @ stop loss 58000")
    memory_keyring.reads = 0
    assert post(client, "/api/setup", {"account": "sub"}).json()["why"] == ["Save a Kraken API key in Setup, step 3."]
    assert post(client, "/api/key", {"api_key": API, "private_key": SECRET}).json() == {"saved": True}
    assert post(client, "/api/chase", LIVE).json()["errors"] == ["Test the Kraken API key in Setup, step 3. It must pass."]
    t = post(client, "/api/key/test").json()
    assert (t["verdict"], t["permissions"]["Withdraw Funds"]) == ("ok", False)
    assert client.get("/api/setup").json()["ready"] is True

    chk = post(client, "/api/live/check", {"pair": "BTC/USD"}).json()
    assert (chk["errors"], chk["first"], chk["min_qty"]) == ([], True, "0.00005")
    assert [(o["type"], o["protect"]) for o in chk["others"]] == [("stop-loss", True), ("limit", False)]  # stop-loss first

    assert post(client, "/api/chase", {**LIVE, "qty": "0.01"}).json()["errors"] == [
        "Your first live order uses the Kraken minimum: 0.00005 BTC."]
    assert post(client, "/api/chase", LIVE).json()["errors"] == ["Tick the box for your first live order."]
    assert post(client, "/api/chase", {**LIVE, "first_ok": True}).json()["errors"] == [
        "Tick the box to start: the safety timer can cancel your stop-loss and take-profit orders."]
    assert fake.calls_of("AddOrder") == [fake.calls_of("AddOrder")[0]] and "validate" in fake.calls_of("AddOrder")[0]
    r = post(client, "/api/chase", {**LIVE, "first_ok": True, "others_ok": True})
    assert r.status_code == 200, r.text
    wait_for(lambda: eng.chase.phase == "resting")
    assert (eng.db.get(r.json()["id"])[1], eng.chase.qty) == ("live", D("0.00005"))
    assert memory_keyring.reads == 0                                          # saved this start: no Keychain read
    assert client.get("/api/setup").json()["first"] is False                  # the tick is one time only (Q6)


def test_the_keychain_is_read_one_time_at_each_start_of_the_tool(tmp_path, fake, memory_keyring):
    with live_app(tmp_path, fake) as (eng, f, clock, call, client):           # the key is in the Keychain from before
        post(client, "/api/setup", {"account": "no"})
        memory_keyring.reads = 0
        assert post(client, "/api/key/test").json()["verdict"] == "ok"
        book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
        assert post(client, "/api/live/check", {"pair": "BTC/USD"}).json()["errors"] == []
        assert post(client, "/api/live/check", {"pair": "BTC/USD"}).json()["errors"] == []
        assert memory_keyring.reads == 1
    with live_app(tmp_path, fake) as (eng, f, clock, call, client):           # the next start asks again, one time
        assert client.get("/api/setup").json()["ready"] is True                # the answers and the test stay
        book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
        post(client, "/api/live/check", {"pair": "BTC/USD"})
        assert memory_keyring.reads == 2


# ---------- U6: the restart reconcile (Q5, C8) ----------

def stopped_during_a_chase(tmp_path, fake, fill: str | None = None) -> tuple[str, "Clock"]:
    """A live chase rests, then the tool stops (no Stop, no timer 0)."""
    with live_app(tmp_path, fake) as (eng, f, clock, call, _):
        book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
        assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
        wait_for(lambda: eng.chase.phase == "resting")
        if fill:
            fake.run(fake.fill, eng.chase.id, fill, None, False)               # a fill the tool did not see
        return eng.chase.id, clock


def test_at_restart_an_order_still_open_is_cancelled_and_its_fills_are_recorded(tmp_path, fake):
    cid, _ = stopped_during_a_chase(tmp_path, fake, "0.018")
    with live_app(tmp_path, fake) as (eng, f, clock, call, client):
        wait_for(lambda: eng.restart is None)
        c = eng.db.get(cid)[0]
        assert client.get("/", follow_redirects=False).headers["location"] == "/new"
        assert eng.setup_state()["why"][-1:] != [eng.blocked]
    assert (c.phase, c.outcome, c.found, c.filled) == ("done", "ended", "open", D("0.018"))
    assert fake.calls_of("CancelOrder") == [{"cl_ord_id": cid}]
    assert fake.by_cl(cid)[1]["status"] == "canceled"
    assert fake.calls_of("CancelAllOrdersAfter")[-1] == {"timeout": "0"} and fake.timer is None   # the other orders stay
    assert [e["text"] for e in eng.db.events(cid)][-2:] == [
        "Kraken reports 0.0180 BTC more filled than the tool saw. Recorded it at 62,417.90 (maker).",
        "The order was still open on Kraken. The tool cancelled it. The safety timer had not fired: your other orders "
        "are untouched. Filled 0.0180 of 0.0500 BTC. Chase ended. The tool sends no order after a restart without you."]
    assert len(fake.calls_of("AddOrder")) == 1                               # no IOC, no new order


def test_at_restart_after_the_timer_fired_the_page_lists_the_other_orders_it_cancelled(tmp_path, fake):
    fake.add_other("limit", "XBTUSD", "sell 1 XBTUSD @ limit 70000")
    fake.add_other("stop-loss", "XBTUSD", "sell 0.1 XBTUSD @ stop loss 58000")
    cid, clock = stopped_during_a_chase(tmp_path, fake)
    clock.t += 50
    fake.run(fake.fire_timer)                                                # Kraken's cancel-all, the tool is off
    with live_app(tmp_path, fake) as (eng, f, clock2, call, client):
        wait_for(lambda: eng.restart is None)
        got = client.get(f"/api/chase/{cid}").json()
    assert (got["found"], got["outcome"], got["others_cancelled"]) == (
        "timer", "ended", [["stop-loss", "XBTUSD", "sell 0.1 XBTUSD @ stop loss 58000"],
                           ["limit", "XBTUSD", "sell 1 XBTUSD @ limit 70000"]])
    assert fake.calls_of("CancelOrder") == []
    assert len(fake.calls_of("AddOrder")) == 1                               # the others are not placed again (G19 P3)


def test_at_restart_kraken_not_answering_blocks_new_live_chases_and_says_why(tmp_path, fake):
    cid, _ = stopped_during_a_chase(tmp_path, fake)
    fake.rest_down = True
    with live_app(tmp_path, fake) as (eng, f, clock, call, client):
        wait_for(lambda: eng.reading["error"] is not None)
        assert client.get("/", follow_redirects=False).headers["location"] == "/reconcile"
        state = client.get("/api/state").json()
        assert state["restart"]["id"] == cid and state["restart"]["reading"]["error"] == (
            "Kraken did not answer. Check the connection.")
        blocked = ("The tool cannot read Kraken to check the live chase of the last stop. Kraken did not answer. "
                   "Check the connection. New live chases wait until it can.")
        post(client, "/api/setup", {"account": "no"})
        book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
        assert blocked in post(client, "/api/chase", LIVE).json()["errors"]
        fake.rest_down = False                                               # the next try (2 s later) reads Kraken
        wait_for(lambda: eng.restart is None)
        assert blocked not in eng.setup_state()["why"]
        assert eng.db.get(cid)[0].found == "open"


def test_fill_now_and_stop_that_the_tool_ignores_answer_ok_false_with_the_reason(live, fake):
    eng, f, clock, call, client = live
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    wait_for(lambda: eng.chase.phase == "resting")
    call(eng.on_link, False, 1, 2)                                            # the public feed is lost
    r = post(client, "/api/chase/fillnow")
    assert (r.status_code, r.json()) == (409, {"ok": False, "reason":
        "The price feed is lost, so there is no valid price for the IOC. Stop the chase, or wait for the feed."})
    assert post(client, "/api/chase/stop").json() == {"ok": True, "reason": None}
    assert post(client, "/api/chase/stop").json() == {"ok": False, "reason":
        "Stop is already under way: the tool is cancelling the order."} or eng.chase.phase == "done"
    wait_for(lambda: eng.chase.phase == "done")
    got = client.get(f"/api/chase/{eng.chase.id}").json()
    assert got["txids"] == {eng.chase.id: "OFAKE01-AAAAA-BBBBBB"}            # Kraken's order id for the result page


# ---------- the repair of the T4 reviews: the chase slot, the timer, Kraken's errors, the txid, the nonce ----------

def ready(client, f, call, fake):
    post(client, "/api/setup", {"account": "no"})
    assert post(client, "/api/key/test").json()["verdict"] == "ok"
    book(f, call, "snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")])
    fake.calls.clear()


def test_two_live_starts_at_once_one_runs_and_the_refused_one_leaves_its_timer_and_feed_alone(live, fake, monkeypatch):
    """LIVE-START-RACE-DISARMS-TIMER: the second start is refused before its first wait and touches no gateway."""
    import threading
    from test_server import ORIGIN, token_of
    eng, f, clock, call, client = live
    ready(client, f, call, fake)
    slow = type(fake).r_OpenOrders
    monkeypatch.setattr(type(fake), "r_OpenOrders", lambda self, p: time.sleep(0.3) or slow(self, p))   # the live check waits
    h, got = {**ORIGIN, "X-Session-Token": token_of(client)}, []
    starts = [threading.Thread(target=lambda: got.append(client.post("/api/chase", json={**LIVE, "first_ok": True}, headers=h)))
              for _ in range(2)]
    for t in starts:
        t.start()
    for t in starts:
        t.join()
    assert sorted((r.status_code, r.json().get("errors")) for r in got) == [
        (200, None), (400, ["A chase runs now. You can start a new chase when it ends."])]
    wait_for(lambda: eng.chase.phase == "resting")
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}] and fake.timer is not None
    assert eng.kgw.timer["on"] and eng.kgw.task is not None and not eng.kgw.task.done() and eng.kgw.awake is not None
    assert len(fake.calls_of("subscribe")) == 1 and len(fake.calls_of("OpenOrders")) == 1


def test_a_lost_answer_to_the_first_timer_still_sets_it_to_0_and_the_start_says_when_0_is_lost_too(live, fake):
    """TIMER-ARMED-AFTER-FAILED-OPEN: the timer counts as on from the send of 60."""
    eng, f, clock, call, client = live
    ready(client, f, call, fake)
    fake.lose = {"CancelAllOrdersAfter"}                                     # Kraken sets 60, the answer is lost
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == [
        "Nothing was placed. Kraken did not answer. Check the connection.",
        "The safety timer can still be on: within 60 s Kraken cancels ALL open orders on this account, "
        "also stop-loss and take-profit orders."]
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}, {"timeout": "0"}]
    assert fake.timer is None and fake.calls_of("AddOrder") == []          # the 0 reached Kraken; no order
    fake.lose = set()
    fake.errors["CancelAllOrdersAfter"] = ["EService:Unavailable"]          # 60 refused: 0 goes out and is answered
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == [
        'Nothing was placed. Kraken answered "EService:Unavailable".']
    assert fake.calls_of("CancelAllOrdersAfter")[2:] == [{"timeout": "60"}, {"timeout": "0"}]


def gateway(fake, http, clock=lambda: 1000.0):
    from order_chaser.kraken import KrakenGateway
    gw = KrakenGateway(rest.KrakenRest(KEY, http, url=fake.url), lambda: KEY, fake.ws_url, clock, AWAKE)
    gw.chase = chase()
    return gw


def test_a_transient_kraken_error_on_a_read_is_tried_again_and_a_permanent_one_ends_the_chase(fake, monkeypatch):
    """NOANSWER-ON-ANY-KRAKEN-ERROR: Busy, Unavailable, the rate limit, a nonce and an internal error are retried."""
    from order_chaser import kraken
    monkeypatch.setattr(kraken, "RETRY_EVERY", 0.01)

    async def run():
        async with httpx.AsyncClient() as http:
            gw = gateway(fake, http)
            await gw.open()
            await gw.send(core.Place(ID, "buy", D("62417.9"), D("0.05")), 1000.0)
            fake.errors["QueryOrders"] = ["EService:Busy", "EService:Unavailable", "EAPI:Rate limit exceeded",
                                          "EAPI:Invalid nonce", "EGeneral:Internal error"]
            again = await gw.send(core.Query(ID), 1001.0)
            fake.errors["QueryOrders"] = ["EGeneral:Permission denied"]
            permanent = await gw.send(core.Query(ID), 1002.0)
            await gw.close(noanswer=True)
            return again, permanent
    again, permanent = asyncio.run(run())
    assert again == [core.OrderState(1000.0, True, D(0), D("62417.9"), ID)]
    assert len(fake.calls_of("QueryOrders")) == 7
    assert permanent == [core.NoAnswer(1000.0, "EGeneral:Permission denied")]


def test_a_chase_with_no_answer_cancels_its_leg_and_sets_the_timer_to_0_when_kraken_answers_again(fake):
    async def run(down: bool):
        async with httpx.AsyncClient() as http:
            gw = gateway(fake, http)
            await gw.open()
            await gw.send(core.Place(gw.chase.leg, "buy", D("62417.9"), D("0.05")), 1000.0)
            fake.rest_down = down
            ok = await gw.close(noanswer=True)
            fake.rest_down = False
            return ok
    assert asyncio.run(run(False)) is True
    assert fake.calls_of("CancelOrder") == [{"cl_ord_id": ID}] and fake.by_cl(ID)[1]["status"] == "canceled"
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}, {"timeout": "0"}] and fake.timer is None
    fake.calls.clear()
    fake.orders.clear()
    assert asyncio.run(run(True)) is False                                   # no answer: the timer stays and cancels it
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}] and fake.timer is not None


def test_after_the_ioc_a_chase_with_no_answer_never_keeps_the_timer(fake, monkeypatch):
    from order_chaser import kraken
    monkeypatch.setattr(kraken, "RETRY_EVERY", 0.01)

    async def run():
        async with httpx.AsyncClient() as http:
            gw = gateway(fake, http)
            await gw.open()
            fake.errors["QueryOrders"] = ["EGeneral:Permission denied"]
            got = await gw.send(core.Ioc("ocioc000000001", "buy", D("62418.5"), D("0.01")), 1000.0)
            await gw.close(noanswer=True)
            return got
    assert asyncio.run(run()) == [core.NoAnswer(1000.0, "EGeneral:Permission denied")]
    assert fake.calls_of("CancelOrder") == []                                # the IOC rests nothing
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}, {"timeout": "0"}] and fake.timer is None


def test_reads_send_the_txid_a_refused_leg_is_closed_without_a_read_and_a_lost_add_is_found_by_cl_ord_id(fake):
    """QUERYORDERS-TXID-REQUIRED: the fake refuses a QueryOrders with no txid, as Kraken's docs say."""
    async def run():
        async with httpx.AsyncClient() as http:
            gw = gateway(fake, http)
            await gw.open()
            out = [await gw.send(core.Place("ocok0000000001", "buy", D("62417.9"), D("0.05")), 1000.0),
                   await gw.send(core.Query("ocok0000000001"), 1000.0),
                   await gw.send(core.Place("occross00000001", "buy", D("62418.5"), D("0.05")), 1000.0),   # crosses
                   await gw.send(core.Query("occross00000001"), 1000.0)]
            fake.lose = {"AddOrder"}
            out.append(await gw.send(core.Place("oclost00000001", "buy", D("62417.9"), D("0.05")), 1000.0))
            fake.lose = set()
            out.append(await gw.send(core.Query("oclost00000001"), 1000.0))
            await gw.close()
            return out
    placed, read, crossed, refused, lost, found = asyncio.run(run())
    assert (placed, crossed, lost) == ([core.Placed(1000.0)], [core.Rejected(1000.0, "place", "would_cross")],
                                       [core.Placed(1000.0)])
    assert read == [core.OrderState(1000.0, True, D(0), D("62417.9"), "ocok0000000001")]
    assert refused == [core.OrderState(1000.0, False, D(0), None, "occross00000001")]
    assert found == [core.OrderState(1000.0, True, D(0), D("62417.9"), "oclost00000001")]
    assert fake.calls_of("QueryOrders") == [{"txid": "OFAKE01-AAAAA-BBBBBB"}, {"txid": "OFAKE02-AAAAA-BBBBBB"}]
    assert len(fake.calls_of("OpenOrders")) == 1                             # the lost add, by its cl_ord_id


class SlowFirst(httpx.AsyncBaseTransport):
    """The first request waits 0.2 s on its way to Kraken: a later call can overtake it."""
    def __init__(self):
        self.inner, self.n = httpx.AsyncHTTPTransport(), 0

    async def handle_async_request(self, req):
        self.n += 1
        if self.n == 1:
            await asyncio.sleep(0.2)
        return await self.inner.handle_async_request(req)


def test_signed_calls_at_once_reach_kraken_with_their_nonces_in_order(fake):
    """NONCE-RACE-CONCURRENT-CALLS: Kraken refuses a nonce below the last one; the client sends one call at a time."""
    async def run():
        async with httpx.AsyncClient(transport=SlowFirst()) as http:
            r = rest.KrakenRest(KEY, http, url=fake.url)
            return await asyncio.gather(*(r.call("OpenOrders") for _ in range(3)), return_exceptions=True)
    assert asyncio.run(run()) == [{"open": {}}] * 3


def test_while_a_read_retries_no_second_read_of_the_leg_waits_in_the_queue(live, fake, monkeypatch):
    """QUERY-BACKLOG-WHILE-READ-RETRIES: each 5 s read of a lost private feed joins the queue only when none waits."""
    from order_chaser import kraken
    monkeypatch.setattr(kraken, "RETRY_EVERY", 0.05)
    eng, f, clock, call, client = live
    ready(client, f, call, fake)
    assert call(eng.start_live, "BTC/USD", "buy", D("0.05"), None, 120) == []
    wait_for(lambda: eng.chase.phase == "resting")
    fake.ws_down = True
    fake.run(fake.drop_ws)
    wait_for(lambda: not eng.chase.pfeed_ok)
    fake.errors["QueryOrders"] = ["EService:Busy"] * 10_000
    for _ in range(4):                                                       # 4 reads due, 24 s: within 60 s
        clock.t += 6
        call(eng.tick)
        wait_for(lambda: len(fake.calls_of("QueryOrders")) >= 2)
    assert eng.sending == core.Query(eng.chase.leg) and list(eng.queue) == []
    fake.errors["QueryOrders"] = []
    wait_for(lambda: eng.sending is None)
    assert eng.chase.phase == "resting"
