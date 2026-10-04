"""The dry-run gateway: a simulated exchange on live public prices.

It takes the core's commands and answers with the same events that the Kraken
gateway gives in T4. It sends nothing to Kraken.

Rules:
- A simulated buy at P fills only when a public sell trade prints below P
  (sell: a buy trade above P). Fill qty = min(remainder, trade qty), as maker, at P.
- A post-only place or amend is rejected when the price is at or above the
  best ask (sell: at or below the best bid).
- The IOC fills against the book, level by level, up to its limit, as taker.
"""
from __future__ import annotations

from decimal import Decimal

from . import core


class SimGateway:
    def __init__(self) -> None:
        self.order: dict | None = None    # {id, side, price, qty, cum, open}
        self.bids: list[tuple[Decimal, Decimal]] = []   # best first
        self.asks: list[tuple[Decimal, Decimal]] = []

    def on_book(self, bids, asks) -> None:
        self.bids, self.asks = list(bids), list(asks)

    def _crosses(self, side: str, price: Decimal) -> bool:
        if side == "buy":
            return bool(self.asks) and price >= self.asks[0][0]
        return bool(self.bids) and price <= self.bids[0][0]

    def send(self, cmd, now: float) -> list:
        o = self.order
        if isinstance(cmd, core.Place):
            if self._crosses(cmd.side, cmd.price):
                return [core.Rejected(now, "place", "would_cross")]
            self.order = {"id": cmd.id, "side": cmd.side, "price": cmd.price, "qty": cmd.qty,
                          "cum": Decimal(0), "open": True}
            return [core.Placed(now)]
        if isinstance(cmd, core.Amend):
            if not o or not o["open"] or o["id"] != cmd.id:
                return [core.Rejected(now, "amend", "not_open")]
            if self._crosses(o["side"], cmd.price):
                return [core.Rejected(now, "amend", "would_cross")]
            o["price"] = cmd.price
            return [core.Amended(now)]
        if isinstance(cmd, core.Cancel):
            if not o or not o["open"] or o["id"] != cmd.id:
                return [core.Rejected(now, "cancel", "not_open")]
            o["open"] = False
            return [core.Canceled(now)]
        if isinstance(cmd, core.Query):
            if not o or o["id"] != cmd.id:
                return [core.OrderState(now, False, Decimal(0), None)]
            return [core.OrderState(now, o["open"], o["cum"], o["price"] if o["open"] else None)]
        if isinstance(cmd, core.Ioc):
            return self._ioc(cmd, now)
        raise TypeError(cmd)

    def _ioc(self, cmd: core.Ioc, now: float) -> list:
        left, out = cmd.qty, []
        levels = self.asks if cmd.side == "buy" else self.bids
        for price, qty in levels:
            if left <= 0 or (price > cmd.price if cmd.side == "buy" else price < cmd.price):
                break
            take = min(left, qty)
            out.append(core.Filled(now, take, price, maker=False, order="ioc"))
            left -= take
        return out + [core.IocDone(now)]

    def on_trade(self, side: str, price: Decimal, qty: Decimal, now: float) -> list:
        o = self.order
        if not o or not o["open"]:
            return []
        through = (side == "sell" and price < o["price"]) if o["side"] == "buy" else (side == "buy" and price > o["price"])
        if not through:
            return []
        take = min(o["qty"] - o["cum"], qty)
        o["cum"] += take
        if o["cum"] >= o["qty"]:
            o["open"] = False
        return [core.Filled(now, take, o["price"], maker=True, order="chase")]
