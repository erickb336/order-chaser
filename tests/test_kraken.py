"""Tests of the live side (T4 U4, U5) against a LOCAL fake Kraken (tests/fake_kraken.py): REST and WebSocket v2 on
127.0.0.1. Sample data only: no test sends a request to Kraken, and no test uses a real key."""
import asyncio
import json
import time
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

import base64
from types import SimpleNamespace

import signed_client as rest
from fake_kraken import API, SECRET, FakeKraken
from order_chaser import core, feed
from order_chaser.kraken import PrivateFeed, exec_events

FIXTURE = Path(__file__).parent / "fixtures" / "kraken-executions.json"
PAIR = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5", "pair_decimals": 1,
                                     "lot_decimals": 8, "status": "online", "altname": "XBTUSD"}})["BTC/USD"]
KEY = SimpleNamespace(api_key=API, secret=base64.b64decode(SECRET))   # a dummy key of the fake Kraken
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


def test_the_gateway_sends_a_spot_order_and_refuses_a_margin_order_with_no_call_to_kraken(fake, tmp_path):
    # R7: live is spot only. A margin command never reaches Kraken, also not as a spot order.
    from order_chaser.kraken import KrakenGateway
    fake.refuse_amend = "EOrder:Invalid arguments"

    async def run():
        async with httpx.AsyncClient() as http:
            gw = KrakenGateway(rest.KrakenRest(KEY, http, url=fake.url), fake.ws_url, lambda: 1000.0, AWAKE)
            gw.chase = chase()
            await gw.open()
            out = {}
            for name, cmd in (("margin place", core.MarginPlace(ID, "buy", D("62417.9"), D("0.05"), 3, False)),
                              ("margin ioc", core.MarginIoc(ID + "-i", "buy", D("62418.5"), D("0.05"), 3, True))):
                try:
                    out[name] = await gw.send(cmd, 1000.0)
                except TypeError:
                    out[name] = "TypeError"
            out["spot place"] = await gw.send(core.Place(ID, "buy", D("62417.9"), D("0.05")), 1000.0)
            out["amend"] = await gw.send(core.Amend(ID, D("62418.0")), 1001.0)
            out["unknown"] = await gw.send(core.Query("ocnever0000001"), 1002.0)
            await gw.send(core.Cancel(ID), 1003.0)
            await gw.close()
            return out
    out = asyncio.run(run())
    assert out == {"margin place": "TypeError", "margin ioc": "TypeError", "spot place": [core.Placed(1000.0)],
                   "amend": [core.Rejected(1000.0, "amend", "EOrder:Invalid arguments")],
                   "unknown": [core.OrderState(1000.0, False, D(0), None, "ocnever0000001")]}
    assert fake.calls_of("AddOrder") == [{"ordertype": "limit", "type": "buy", "volume": "0.05", "price": "62417.9",
                                          "pair": "XBTUSD", "cl_ord_id": ID, "oflags": "post"}]


def gateway(fake, http, clock=lambda: 1000.0):
    from order_chaser.kraken import KrakenGateway
    gw = KrakenGateway(rest.KrakenRest(KEY, http, url=fake.url), fake.ws_url, clock, AWAKE)
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


def test_an_account_rate_limit_on_a_new_order_is_a_rate_limit_refusal_so_the_core_waits_and_places_it_again(fake):
    """EAPI-RATE-LIMIT-ON-PLACE: Kraken did not take the order. Another transient error stays "Kraken did not answer"."""
    async def run():
        async with httpx.AsyncClient() as http:
            gw = gateway(fake, http)
            await gw.open()
            fake.errors["AddOrder"] = ["EAPI:Rate limit exceeded", "EService:Busy"]
            out = [await gw.send(core.Place("oc-rate", "buy", D("62417.9"), D("0.05")), 1000.0),
                   await gw.send(core.Place("oc-busy", "buy", D("62417.9"), D("0.05")), 1000.0)]
            await gw.close()
            return out
    rate, busy = asyncio.run(run())
    assert rate == [core.Rejected(1000.0, "place", "rate_limit")]
    assert busy == [core.Rejected(1000.0, "place", "Kraken did not answer")]
    # The core's answer to it: the first order waits and goes out again, it does not end as refused.
    c, out = core.step(chase(), rate[0])
    assert (c.phase, c.outcome, c.rate) == ("resting", None, float(core.RATE_MAX))


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


def test_a_renewal_on_its_way_when_the_chase_ends_reaches_kraken_before_the_0(fake):
    """A renewal's 60 that came after the 0 left the account's cancel-all armed after the chase (CI, test above)."""
    async def run():
        async with httpx.AsyncClient() as http:
            t = [1000.0]
            gw = gateway(fake, http, clock=lambda: t[0])
            await gw.open()
            t[0] += 21
            gw.tick(t[0])                                                    # the renewal is due: its task starts
            await gw.close()                                                 # at once, before it sent anything
    asyncio.run(run())
    assert fake.calls_of("CancelAllOrdersAfter") == [{"timeout": "60"}, {"timeout": "60"}, {"timeout": "0"}]
    assert fake.timer is None


