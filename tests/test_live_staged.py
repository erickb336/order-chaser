"""T4 after the owner's R1, R2, R7 and R8: this tool only stages live orders. The signed helper (coming) holds the key
and places them. These tests send no request to Kraken and use no key."""
import hashlib
import json

import httpx
import pytest
import websockets

from test_server import MARGIN_BTC, ORIGIN, FakeBook, setup, token_of  # noqa: F401  (setup is a fixture)

SPOT = {"pair": "BTC/USD", "what": "buy", "qty": "0.00005", "timeout": 60, "mode": "live", "first_ok": True}


@pytest.fixture
def outbound(monkeypatch):
    """Every request that the tool sends out (HTTP or WebSocket) is recorded here and fails: a stand-in for Kraken."""
    calls = []

    async def send(self, request, **kw):
        calls.append(str(request.url))
        raise httpx.ConnectError("no network in this test")

    def connect(url, *a, **kw):
        calls.append(url)
        raise OSError("no network in this test")
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    monkeypatch.setattr(websockets, "connect", connect)
    return calls


def ready(setup):
    client, app, eng, f, clock, call = setup
    eng.pairs = MARGIN_BTC
    eng.db.set_setting("account", json.dumps({"answer": "no", "at": clock()}))
    call(f._handle, FakeBook().msg("snapshot", [("62417.9", "1.0")], [("62418.5", "0.01"), ("62419.0", "3.0")]))
    return client, eng, {**ORIGIN, "X-Session-Token": token_of(client)}


def test_a_live_request_with_the_page_token_stages_the_order_and_places_nothing(setup, outbound):
    # R2: the page token can at most stage an order. Nothing is placed, and no private call goes out.
    client, eng, h = ready(setup)
    r = client.post("/api/chase", headers=h, json=SPOT)
    assert r.status_code == 200
    got = r.json()
    order = got["staged"]
    assert got["state"] == "staged: waiting for the helper"
    assert {k: order[k] for k in ("pair", "side", "qty", "limit", "post_only", "timeout")} == {
        "pair": "BTC/USD", "side": "buy", "qty": "0.00005", "limit": "62418.5", "post_only": True, "timeout": 60}
    assert [(s["id"], s["json"], s["sha256"], s["state"]) for s in eng.db.staged()] == [
        (order["id"], got["json"], hashlib.sha256(got["json"].encode()).hexdigest(), "staged: waiting for the helper")]
    assert json.loads(got["json"]) == order
    assert eng.chase is None and eng.db.history() == [] and outbound == []
    # The same request in dry run starts a chase: the live request was not refused for another reason.
    assert client.post("/api/chase", headers=h, json={**SPOT, "mode": "dry"}).status_code == 200
    assert eng.chase is not None and eng.chase.dry and outbound == []


def test_a_live_request_runs_the_start_checks_before_it_stages(setup, outbound):
    client, eng, h = ready(setup)
    assert client.post("/api/chase", headers=h, json={**SPOT, "first_ok": False}).json() == {
        "errors": ["Tick the box for your first live order."]}
    assert client.post("/api/chase", headers=h, json={**SPOT, "qty": "0.05"}).json() == {
        "errors": ["Your first live order uses the Kraken minimum: 0.00005 BTC."]}
    eng.db.set_setting("account", None)
    assert client.post("/api/chase", headers=h, json=SPOT).json() == {
        "errors": ["Answer Setup, step 2: does another bot or API tool use this Kraken account?"]}
    assert eng.db.staged() == [] and outbound == []


@pytest.mark.parametrize("body", [
    {"what": "long", "leverage": 3}, {"what": "short", "leverage": 2}, {"what": "short"},
    {"what": "close-long"}, {"what": "close-short"},
    {"what": "buy", "leverage": 2}, {"what": "sell", "reduce_only": True}, {"what": "buy", "margin": True},
])
def test_a_live_margin_short_or_leverage_request_is_refused_before_staging(setup, outbound, body):
    # R7: live is spot only. The same request in dry run is not refused for this reason.
    client, eng, h = ready(setup)
    r = client.post("/api/chase", headers=h, json={**SPOT, **body})
    assert (r.status_code, r.json()) == (400, {"errors": [
        "A live order is spot only: buy or sell, with no leverage and no margin field. Use a dry run for margin."]})
    assert eng.db.staged() == [] and outbound == []


def test_the_live_check_endpoint_is_gone(setup):
    client, eng, h = ready(setup)
    assert client.post("/api/live/check", headers=h, json={"pair": "BTC/USD"}).status_code in (404, 405)
