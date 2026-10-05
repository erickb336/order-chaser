"""The signed REST client and the permission test (T4 U3). Offline: Kraken's published example vector, and fake
Kraken replies through an httpx MockTransport. No test sends a request to Kraken."""
import asyncio
import base64
import re
import urllib.parse

import httpx
import pytest
from starlette.testclient import TestClient

from order_chaser import keys, rest
from order_chaser.server import create_app

# https://docs.kraken.com/api/docs/guides/spot-rest-auth (read 4 Oct 2026): the published example.
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
API = "DUMMYapiKEYforTESTSonly" + "A" * 33
KEY = keys.Key(API, DOC_SECRET)


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


def check(replies):
    fake = FakeKraken(replies)
    return asyncio.run(rest.check(client_for(fake))), fake


OK = {"CancelOrder": ["EOrder:Unknown order"], "WithdrawMethods": [rest.DENIED],
      "AddOrder": {"descr": {"order": "buy 0.0001 XBTUSD @ limit 1"}}}


def test_a_good_key_is_ok_and_each_call_changes_nothing():
    p, fake = check(OK)
    assert p.verdict == "ok"
    assert p.on == {"Query Funds": True, "Modify Orders": True, "Cancel/Close Orders": True,
                    "Query Open Orders & Trades": True, "Query Closed Orders & Trades": True,
                    "Access WebSockets API": True, "Withdraw Funds": False}
    sent = dict(fake.calls)
    assert sent["AddOrder"]["validate"] == "true"                         # Kraken checks it and places nothing
    assert sent["CancelOrder"] == {"cl_ord_id": "oc-permission-test"}     # an order that does not exist
    assert [m for m, _ in fake.calls] == ["Balance", "AddOrder", "CancelOrder", "OpenOrders", "ClosedOrders",
                                          "GetWebSocketsToken", "WithdrawMethods"]
    assert fake.nonces == sorted(set(fake.nonces))                        # each nonce above the last


def test_c2_cancel_permission_denied_means_off():
    p, _ = check({**OK, "CancelOrder": [rest.DENIED]})
    assert (p.verdict, p.on["Cancel/Close Orders"]) == ("missing", False)


def test_c2_another_cancel_error_is_not_read_as_on():
    p, _ = check({**OK, "CancelOrder": ["EGeneral:Internal error"]})
    assert (p.verdict, p.error, p.on["Cancel/Close Orders"]) == ("error", "EGeneral:Internal error", None)


@pytest.mark.parametrize("error", ["EService:Unavailable", "EService:Busy", "EGeneral:Internal error",
                                   "EGeneral:Invalid arguments"])
def test_a_service_error_in_the_order_permission_test_means_could_not_check_not_on(error):
    """PERM-TEST-SERVICE-ERROR-MEANS-ON: only an order error (EOrder:...) comes after Kraken's permission check."""
    p, _ = check({**OK, "AddOrder": [error]})
    assert (p.verdict, p.error, p.on["Modify Orders"]) == ("error", error, None)
    p, _ = check({**OK, "AddOrder": ["EOrder:Insufficient funds"]})
    assert (p.verdict, p.on["Modify Orders"]) == ("ok", True)


def test_c1_without_query_funds_the_withdraw_test_is_not_run_and_the_key_is_not_usable():
    p, fake = check({**OK, "Balance": [rest.DENIED], "WithdrawMethods": {"x": 1}})
    assert (p.verdict, p.on["Query Funds"], p.on["Withdraw Funds"]) == ("nofunds", False, None)
    assert "WithdrawMethods" not in [m for m, _ in fake.calls]


def test_q4_a_key_that_can_withdraw_is_refused():
    p, _ = check({**OK, "WithdrawMethods": {"0": {"method": "Bitcoin"}}})
    assert (p.verdict, p.on["Withdraw Funds"]) == ("withdraw", True)


@pytest.mark.parametrize("method, label", [("AddOrder", "Modify Orders"), ("OpenOrders", "Query Open Orders & Trades"),
                                           ("ClosedOrders", "Query Closed Orders & Trades"),
                                           ("GetWebSocketsToken", "Access WebSockets API")])
def test_a_needed_permission_that_is_off_makes_the_key_not_usable(method, label):
    p, _ = check({**OK, method: [rest.DENIED]})
    assert (p.verdict, p.on[label]) == ("missing", False)


def test_an_order_error_of_addorder_validate_means_modify_orders_is_on():
    p, _ = check({**OK, "AddOrder": ["EOrder:Insufficient funds"]})
    assert (p.verdict, p.on["Modify Orders"]) == ("ok", True)


def test_an_invalid_key_is_an_error_not_a_permission():
    p, _ = check({**OK, "Balance": ["EAPI:Invalid key"]})
    assert (p.verdict, p.error) == ("error", "EAPI:Invalid key")


def test_no_answer_from_kraken_is_an_error():
    def down(request):
        raise httpx.ConnectError("down")
    p = asyncio.run(rest.check(client_for(down)))
    assert (p.verdict, p.error) == ("error", "Kraken did not answer. Check the connection and test again.")


# ---------- nonce and rate counter ----------

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

@pytest.fixture
def app(tmp_path, memory_keyring):
    fake = FakeKraken(dict(OK))
    guard = create_app(tmp_path, connect=False, latency=0, kraken_transport=httpx.MockTransport(fake))
    with TestClient(guard, base_url="http://127.0.0.1:5180") as client:
        tok = re.search(r'name="oc-token" content="([^"]+)"', client.get("/setup").text).group(1)
        h = {"Origin": "http://127.0.0.1:5180", "X-Session-Token": tok}
        yield client, h, fake, memory_keyring


def test_the_endpoint_tests_the_saved_key(app):
    client, h, fake, kr = app
    assert client.post("/api/key/test", headers=h).status_code == 409          # no key yet
    client.post("/api/key", headers=h, json={"api_key": API, "private_key": DOC_SECRET})
    r = client.post("/api/key/test", headers=h).json()
    assert (r["verdict"], r["removed"], r["permissions"]["Withdraw Funds"]) == ("ok", False, False)
    assert list(kr.items) == [(keys.SERVICE, keys.ACCOUNT)]
    assert API not in str(r) and DOC_SECRET not in str(r)


def test_q4_the_endpoint_removes_a_key_that_can_withdraw_from_the_keychain(app):
    client, h, fake, kr = app
    fake.replies["WithdrawMethods"] = {"0": {"method": "Bitcoin"}}
    client.post("/api/key", headers=h, json={"api_key": API, "private_key": DOC_SECRET})
    r = client.post("/api/key/test", headers=h).json()
    assert (r["verdict"], r["removed"]) == ("withdraw", True)
    assert kr.items == {}
    assert client.post("/api/key/test", headers=h).status_code == 409          # the key is gone from memory too


def test_the_test_endpoint_needs_the_session_token(app):
    client, h, fake, kr = app
    assert client.post("/api/key/test", headers={"Origin": h["Origin"]}).status_code == 403
    assert fake.calls == []
