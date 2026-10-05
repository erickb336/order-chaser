"""The signed REST client and the permission test (T4 U3). Offline: Kraken's published example vector, and fake
Kraken replies through an httpx MockTransport. No test sends a request to Kraken."""
import asyncio
import base64
from types import SimpleNamespace
import re
import urllib.parse

import httpx
import pytest

import signed_client as rest

# https://docs.kraken.com/api/docs/guides/spot-rest-auth (read 4 Oct 2026): the published example.
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
API = "DUMMYapiKEYforTESTSonly" + "A" * 33
KEY = SimpleNamespace(api_key=API, secret=base64.b64decode(DOC_SECRET))   # a dummy key: Kraken's published example


def test_the_signature_matches_krakens_published_example():
    assert rest.sign(base64.b64decode(DOC_SECRET), "/0/private/AddOrder", 1616492376594,
                     "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25") == \
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


class FakeKraken:
    """Kraken's private REST, faked: it checks the signature of each request and answers from a table."""

    def __init__(self, replies: dict[str, list[str] | dict]):
        self.replies, self.calls, self.nonces = replies, [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, data = request.url.path, request.content.decode()
        params = dict(urllib.parse.parse_qsl(data))
        assert request.headers["API-Key"] == API
        assert request.headers["API-Sign"] == rest.sign(base64.b64decode(DOC_SECRET), path, int(params["nonce"]), data)
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        method = path.removeprefix("/0/private/")
        self.calls.append((method, {k: v for k, v in params.items() if k != "nonce"}))
        self.nonces.append(int(params["nonce"]))
        r = self.replies.get(method, {})
        return httpx.Response(200, json={"error": r, "result": {}} if isinstance(r, list) else {"error": [], "result": r})


def client_for(fake, clock=lambda: 1_700_000_000.0, rate=None, sleep=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return rest.KrakenRest(KEY, http, clock=clock, rate=rate, sleep=sleep or asyncio.sleep)


def test_the_nonce_always_increases_also_when_the_clock_goes_back():
    t = [1000.0]
    c = rest.KrakenRest(KEY, None, clock=lambda: t[0])
    a, b = c.next_nonce(), c.next_nonce()
    t[0] = 999.0
    assert [a, b, c.next_nonce()] == [1_000_000, 1_000_001, 1_000_002]
    t[0] = 2000.0
    assert c.next_nonce() == 2_000_000


def test_the_rate_counter_waits_when_the_next_call_would_pass_15():
    t, slept = [0.0], []

    async def sleep(s):
        slept.append(round(s, 2))
        t[0] += s
    fake = FakeKraken({"QueryOrders": {}})
    c = client_for(fake, clock=lambda: 1e9 + t[0], rate=rest.RateCounter(lambda: t[0]), sleep=sleep)

    async def run():
        for _ in range(16):
            await c.call("QueryOrders", txid="X")
        await c.call("CancelOrder", cl_ord_id="x")            # an order call: not on this counter
    asyncio.run(run())
    assert slept == [3.03]                                    # (15 + 1 - 15) / 0.33 s
    assert round(c.rate.level, 2) == 15.0


def test_the_counter_falls_033_each_second():
    t = [0.0]
    r = rest.RateCounter(lambda: t[0])
    r.spend(10)
    t[0] = 3.0
    assert round(r.now(), 2) == 9.01
    t[0] = 100.0
    assert r.now() == 0.0


def test_a_rate_limit_reply_sets_the_counter_to_the_top():
    fake = FakeKraken({"Balance": ["EAPI:Rate limit exceeded"]})
    c = client_for(fake, rate=rest.RateCounter(lambda: 0.0))
    with pytest.raises(rest.KrakenError):
        asyncio.run(c.call("Balance"))
    assert c.rate.level == 15.0


# ---------- the test endpoint ----------

