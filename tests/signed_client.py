"""A REFERENCE signed Kraken REST client, for the tests only (R1: no code of the tool signs or holds a key).
The tests sign their calls to the local fake Kraken with it; the vector test checks it against Kraken's example.

Signature (https://docs.kraken.com/api/docs/guides/spot-rest-auth):
  API-Sign = base64(HMAC-SHA512(base64decode(private key), uri path + SHA256(nonce + POST data)))
The nonce always increases for a key: Unix time in ms, at least one above the last nonce of this process.

REST rate counter (Kraken Starter tier): at most 15, it falls 0.33 each second. Most private calls cost 1;
Ledgers, QueryLedgers and TradesHistory cost 2; order calls (AddOrder, CancelOrder, ...) use the trading counter,
not this one, and cost 0 here. The client waits until the call fits, so that Kraken never refuses it for the rate.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx


URL = "https://api.kraken.com"
RATE_MAX, RATE_FALL = 15.0, 0.33            # Starter tier
COST = {"Ledgers": 2, "QueryLedgers": 2, "TradesHistory": 2,
        **dict.fromkeys(("AddOrder", "AddOrderBatch", "AmendOrder", "EditOrder", "CancelOrder", "CancelAll",
                         "CancelAllOrdersAfter", "CancelOrderBatch"), 0)}


def sign(secret: bytes, path: str, nonce: int, data: str) -> str:
    """Kraken's API-Sign of one request. data: the url-encoded POST body, with the nonce in it."""
    digest = hashlib.sha256((str(nonce) + data).encode()).digest()
    return base64.b64encode(hmac.new(secret, path.encode() + digest, hashlib.sha512).digest()).decode()


class RateCounter:
    """An estimate of Kraken's REST counter for this key."""

    def __init__(self, clock=time.monotonic) -> None:
        self.clock, self.level, self.at = clock, 0.0, clock()

    def now(self) -> float:
        t = self.clock()
        self.level, self.at = max(0.0, self.level - (t - self.at) * RATE_FALL), t
        return self.level

    def wait(self, cost: float) -> float:
        """Seconds until a call of this cost fits under the maximum (0: now)."""
        return max(0.0, (self.now() + cost - RATE_MAX) / RATE_FALL)

    def spend(self, cost: float) -> None:
        self.level = self.now() + cost

    def full(self) -> None:
        """Kraken said "EAPI:Rate limit exceeded": its counter is at the top."""
        self.now()
        self.level = RATE_MAX


from order_chaser.kraken import KrakenError  # noqa: E402  (the error of a refused call)


class KrakenRest:
    """One for each process: the nonce and the rate counter belong to the key. Set .key before a call."""

    def __init__(self, key, http: httpx.AsyncClient, clock=time.time, rate: RateCounter | None = None,
                 sleep=asyncio.sleep, url: str = URL) -> None:
        self.key, self.http, self.clock, self.sleep, self.url = key, http, clock, sleep, url
        self.rate = rate or RateCounter()
        self.nonce = 0
        self.lock = asyncio.Lock()   # one signed call at a time: Kraken gets the nonces in order, the counter is exact

    def next_nonce(self) -> int:
        self.nonce = max(self.nonce + 1, int(self.clock() * 1000))
        return self.nonce

    async def call(self, method: str, **params) -> dict:
        """One private call. Returns Kraken's "result"; raises KrakenError on Kraken's errors."""
        cost = COST.get(method, 1)
        async with self.lock:
            if wait := self.rate.wait(cost):
                await self.sleep(wait)
            self.rate.spend(cost)
            path = f"/0/private/{method}"
            nonce = self.next_nonce()
            data = urllib.parse.urlencode({"nonce": nonce, **params})
            r = await self.http.post(self.url + path, content=data, headers={
                "API-Key": self.key.api_key, "API-Sign": sign(self.key.secret, path, nonce, data),
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        r.raise_for_status()   # a 5xx (or a proxy page) is no answer from Kraken: httpx.HTTPStatusError
        body = r.json()
        if body.get("error"):
            if "EAPI:Rate limit exceeded" in body["error"]:
                self.rate.full()
            raise KrakenError(body["error"])
        return body.get("result") or {}

