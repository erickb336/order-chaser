"""Public Kraken data: WebSocket v2 book (depth 10, checksum) and trades, and REST AssetPairs.

Only public endpoints. Nothing here signs a request or sends an order.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import websockets

from .book import OrderBook
from .core import Pair

WS_URL = "wss://ws.kraken.com/v2"
REST_PAIRS = "https://api.kraken.com/0/public/AssetPairs"
PAIRS = ("BTC/USD", "ETH/USD", "SOL/USD")
SILENCE_LIMIT = 10.0   # seconds without a message (Kraken sends a heartbeat each second)


def parse_pairs(result: dict) -> dict[str, Pair]:
    out = {}
    for symbol in PAIRS:
        r = result.get(symbol)
        if not r:
            continue
        base, quote = symbol.split("/")
        out[symbol] = Pair(symbol=symbol, base=base, quote=quote, tick=Decimal(r["tick_size"]),
                           ordermin=Decimal(r["ordermin"]), costmin=Decimal(r["costmin"]),
                           price_decimals=int(r["pair_decimals"]), qty_decimals=int(r["lot_decimals"]),
                           status=r["status"])
    return out


async def fetch_pairs(client: httpx.AsyncClient) -> dict[str, Pair]:
    r = await client.get(REST_PAIRS, params={"pair": ",".join(PAIRS)}, timeout=10)
    body = r.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return parse_pairs(body["result"])


class PublicFeed:
    """One WebSocket connection, subscribed to the book and trades of one pair.

    Callbacks: on_book(book, ok), on_trade(side, price, qty), on_link(up, attempt, next_in).
    """

    def __init__(self, on_book, on_trade, on_link, url: str = WS_URL) -> None:
        self.on_book, self.on_trade, self.on_link, self.url = on_book, on_trade, on_link, url
        self.pair: Pair | None = None
        self.book: OrderBook | None = None
        self.ws = None
        self.attempt = 0
        self.resync = False

    async def watch(self, pair: Pair) -> None:
        if self.pair and self.pair.symbol == pair.symbol:
            self.pair = pair
            return
        old, self.pair = self.pair, pair
        self.book = OrderBook(pair.price_decimals, pair.qty_decimals)
        self.on_book(self.book, False)
        if self.ws is not None:
            try:
                if old:
                    await self._send("unsubscribe", "book", old.symbol, depth=10)
                    await self._send("unsubscribe", "trade", old.symbol)
                await self._subscribe()
            except Exception:
                pass  # the run loop reconnects and subscribes again

    async def _send(self, method: str, channel: str, symbol: str, **extra) -> None:
        await self.ws.send(json.dumps({"method": method, "params": {"channel": channel, "symbol": [symbol], **extra}}))

    async def _subscribe(self) -> None:
        await self._send("subscribe", "book", self.pair.symbol, depth=10)
        await self._send("subscribe", "trade", self.pair.symbol)

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=10, ping_interval=20) as ws:
                    self.ws = ws
                    if self.pair:
                        self.book = OrderBook(self.pair.price_decimals, self.pair.qty_decimals)
                        await self._subscribe()
                    self.attempt = 0
                    self.on_link(True, 0, 0)
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), SILENCE_LIMIT)
                        await self._handle(json.loads(raw, parse_float=Decimal))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            self.ws = None
            self.attempt += 1
            wait = min(2 ** min(self.attempt, 4), 15)
            self.on_link(False, self.attempt, wait)
            await asyncio.sleep(wait)

    async def _handle(self, m: dict) -> None:
        ch = m.get("channel")
        if ch == "book" and self.pair and self.book is not None:
            data = m["data"][0]
            snapshot = m.get("type") == "snapshot"
            if data.get("symbol") != self.pair.symbol or (self.resync and not snapshot):
                return
            ok = self.book.apply(data, snapshot)
            self.resync = not ok
            self.on_book(self.book, ok)
            if not ok:
                # Stale book: drop it and ask for a new snapshot. Updates are ignored until it comes.
                await self._send("unsubscribe", "book", self.pair.symbol, depth=10)
                self.book = OrderBook(self.pair.price_decimals, self.pair.qty_decimals)
                await self._send("subscribe", "book", self.pair.symbol, depth=10)
        elif ch == "trade" and m.get("type") == "update" and self.pair:
            for t in m["data"]:
                if t.get("symbol") == self.pair.symbol:
                    self.on_trade(t["side"], Decimal(str(t["price"])), Decimal(str(t["qty"])))
