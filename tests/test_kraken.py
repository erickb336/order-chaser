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
