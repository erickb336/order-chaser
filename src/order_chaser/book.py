"""Order book of depth 10 from the Kraken WebSocket v2 book channel, with its CRC32 checksum.

Checksum (https://docs.kraken.com/api/docs/websocket-v2/book): for the top 10 asks
(low to high), then the top 10 bids (high to low), write price and qty with the
pair's precision, remove the "." and the leading zeros, join all, and take CRC32.
"""
from __future__ import annotations

import zlib
from decimal import Decimal

DEPTH = 10


def _fmt(v: Decimal, decimals: int) -> str:
    return f"{v:.{decimals}f}".replace(".", "").lstrip("0")


class OrderBook:
    def __init__(self, price_decimals: int, qty_decimals: int) -> None:
        self.pd, self.qd = price_decimals, qty_decimals
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}

    def top_bids(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.bids.items(), reverse=True)[:DEPTH]

    def top_asks(self) -> list[tuple[Decimal, Decimal]]:
        return sorted(self.asks.items())[:DEPTH]

    def apply(self, data: dict, snapshot: bool) -> bool:
        """Apply one book message (its data[0]). Return True when the checksum matches."""
        if snapshot:
            self.bids, self.asks = {}, {}
        for side, levels in ((self.bids, data.get("bids", [])), (self.asks, data.get("asks", []))):
            for lv in levels:
                price, qty = Decimal(str(lv["price"])), Decimal(str(lv["qty"]))
                if qty == 0:
                    side.pop(price, None)
                else:
                    side[price] = qty
        # Keep only the subscribed depth: levels below it are no longer updated.
        self.bids = dict(self.top_bids())
        self.asks = dict(self.top_asks())
        return self.checksum() == int(data["checksum"])

    def checksum(self) -> int:
        s = "".join(_fmt(p, self.pd) + _fmt(q, self.qd) for p, q in self.top_asks())
        s += "".join(_fmt(p, self.pd) + _fmt(q, self.qd) for p, q in self.top_bids())
        return zlib.crc32(s.encode())

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None
