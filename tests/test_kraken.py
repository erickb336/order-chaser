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


def test_the_safety_timer_fires_when_it_is_not_renewed_and_the_chase_ends_with_no_ioc(live, fake):
    eng, f, clock, call, _ = live
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
    fake.rest_down = False
    wait_for(lambda: eng.kgw.task is None)
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}]     # no "0" after the timer fired
    assert len(fake.calls_of("AddOrder")) == 1                               # no IOC


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
    call(eng.tick)                                                           # 5 s: QueryOrders by cl_ord_id
    wait_for(lambda: eng.chase.filled == D("0.02"))
    assert fake.calls_of("amend_order") == []
    assert fake.calls_of("QueryOrders") == [{"cl_ord_id": cid}]
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
