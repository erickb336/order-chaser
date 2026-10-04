"""The dry-run gateway: a simulated exchange on live public prices, and a simulated margin account.

It takes the core's commands and answers with the same events that the Kraken
gateway gives in T4. It sends nothing to Kraken and reads no key.

Rules:
- A simulated buy at P fills when a public sell trade prints below P
  (sell: a buy trade above P). Fill qty = min(remainder, trade qty), as maker, at P.
- A resting buy at P also fills when the public ask comes to or below P (sell: the
  bid to or above P), as maker, at P, up to the size at those levels. Kraken fills a
  resting order in the same way. A price level fills the order at most up to the
  largest size seen there: a quote that leaves and comes back does not fill again
  (on Kraken most such quotes are post-only and would be rejected, not filled).
- A post-only place or amend is rejected when the price is at or above the
  best ask (sell: at or below the best bid).
- The IOC fills against the book, level by level, up to its limit, as taker.

Margin (the simulated account: 5,000 USD at the start, labelled "simulated account" on the pages):
- One position per pair and direction: qty, average entry, collateral (cost / leverage), opened at.
- Mark = the mid of the public book. Unrealized P/L at the mark. Equity = cash + P/L - rollover so far.
  Margin level = equity / used margin x 100. At or below the pair's margin_stop the account is liquidated:
  every position closes at its mark.
- An open fill pays the trading fee and the opening fee (0.05% of the cost). Rollover: 0.05% of the cost
  for each started 4 h, paid when the position (or a part of it) closes.
- A reduce-only order fills at most the position (it never grows or flips it). With no position it is
  refused; when the position is gone while it is open, the gateway reports PositionGone("nopos").
- Refusals, with Kraken's texts: a leverage that the pair does not allow, a position against an open
  position of the other direction, "Margin position size exceeded" (AssetPairs position limit),
  "Margin allowance exceeded" (the most the account may borrow), "Insufficient margin" (free margin for orders).
- refuse_margin_amends: a switch (demo and tests) that refuses each amend of a margin order, so that the
  core cancels and replaces.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import core

ZERO = Decimal(0)
START_CASH = Decimal(5000)
ALLOWANCE = Decimal(100000)   # the most the simulated account may borrow (Kraken sets an allowance for each account)
AMEND_REFUSED = "EOrder:Invalid arguments"   # the refusal of the refuse_margin_amends switch (simulated)


class SimAccount:
    """The simulated margin account of the dry run."""

    def __init__(self, cash: Decimal = START_CASH, allowance: Decimal = ALLOWANCE) -> None:
        self.cash, self.allowance = cash, allowance
        self.positions: dict[tuple[str, str], dict] = {}   # (pair, dir) -> {qty, entry, margin, opened, stop}
        self.marks: dict[str, Decimal] = {}
        self.changed = False                               # cash or positions changed since the last save

    # ----- numbers -----
    def position(self, pair: str, d: str) -> dict | None:
        return self.positions.get((pair, d))

    def used(self) -> Decimal:
        return sum((p["margin"] for p in self.positions.values()), ZERO)

    def borrowed(self) -> Decimal:
        return sum((p["qty"] * p["entry"] - p["margin"] for p in self.positions.values()), ZERO)

    def _upl(self, key, p) -> Decimal:
        mark = self.marks.get(key[0], p["entry"])
        return (mark - p["entry"]) * p["qty"] * (1 if key[1] == "long" else -1)

    @staticmethod
    def _roll(p, now: float) -> Decimal:
        return p["qty"] * p["entry"] * core.ROLLOVER * core.rollover_periods(now - p["opened"])

    def equity(self, now: float) -> Decimal:
        return self.cash + sum((self._upl(k, p) - self._roll(p, now) for k, p in self.positions.items()), ZERO)

    def level(self, now: float) -> Decimal | None:
        used = self.used()
        return self.equity(now) / used * 100 if used else None

    def read(self, now: float, reserved: Decimal = ZERO) -> dict:
        """What Kraken's TradeBalance and OpenPositions give (simulated): the pages and validate_margin use it."""
        free = self.equity(now) - self.used()
        return {
            "simulated": True, "cash": self.cash, "equity": self.equity(now), "used": self.used(), "free": free,
            "free_orders": free - reserved, "level": self.level(now), "at": now,
            "positions": [{"pair": k[0], "dir": k[1], "qty": p["qty"], "entry": p["entry"], "margin": p["margin"],
                           "leverage": round(p["qty"] * p["entry"] / p["margin"]), "mark": self.marks.get(k[0], p["entry"]),
                           "upl": self._upl(k, p), "rollover": self._roll(p, now), "opened": p["opened"]}
                          for k, p in sorted(self.positions.items())],
        }

    def list(self) -> list[core.Position]:
        return [core.Position(k[0], k[1], p["qty"], p["entry"], round(p["qty"] * p["entry"] / p["margin"]))
                for k, p in sorted(self.positions.items())]

    # ----- changes -----
    def fill(self, pair: core.Pair, side: str, leverage: int, reduce_only: bool, qty: Decimal, price: Decimal,
             maker: bool, now: float) -> Decimal:
        """Apply a margin fill. Returns the qty that filled: a reduce-only fill is cut to the position."""
        fee = core.MAKER_FEE if maker else core.TAKER_FEE
        if reduce_only:
            key = (pair.symbol, "short" if side == "buy" else "long")
            p = self.positions.get(key)
            qty = min(qty, p["qty"]) if p else ZERO
            if qty > 0:
                part = qty / p["qty"]
                pl = (price - p["entry"]) * qty * (1 if key[1] == "long" else -1)
                self.cash += pl - qty * price * fee - self._roll(p, now) * part
                p["margin"] -= p["margin"] * part
                p["qty"] -= qty
                if p["qty"] == 0:
                    del self.positions[key]
        else:
            key = (pair.symbol, "long" if side == "buy" else "short")
            p = self.positions.setdefault(key, {"qty": ZERO, "entry": price, "margin": ZERO, "opened": now,
                                                "stop": pair.margin_stop})
            cost = qty * price
            p["entry"] = (p["qty"] * p["entry"] + cost) / (p["qty"] + qty)
            p["qty"] += qty
            p["margin"] += cost / leverage
            self.cash -= cost * (fee + core.OPEN_FEE)
        self.changed = self.changed or qty > 0
        return qty

    def mark(self, pair: str, bid: Decimal, ask: Decimal, now: float) -> bool:
        """A new mark (the mid). True when the account is liquidated: every position closes at its mark."""
        self.marks[pair] = (bid + ask) / 2
        level = self.level(now)
        if level is None or level > max(p["stop"] for p in self.positions.values()):
            return False
        for k, p in self.positions.items():
            self.cash += self._upl(k, p) - self._roll(p, now)
        self.positions = {}
        self.changed = True
        return True

    # ----- the SQLite copy -----
    def to_json(self) -> str:
        return json.dumps({"cash": str(self.cash), "allowance": str(self.allowance),
                           "positions": [[k[0], k[1], {**{f: str(p[f]) for f in ("qty", "entry", "margin")},
                                                       "opened": p["opened"], "stop": p["stop"]}]
                                         for k, p in self.positions.items()]})

    @classmethod
    def from_json(cls, s: str) -> SimAccount:
        d = json.loads(s)
        a = cls(Decimal(d["cash"]), Decimal(d["allowance"]))
        for pair, dr, p in d["positions"]:
            a.positions[(pair, dr)] = {"qty": Decimal(p["qty"]), "entry": Decimal(p["entry"]), "margin": Decimal(p["margin"]),
                                       "opened": p["opened"], "stop": p["stop"]}
        return a


class SimGateway:
    def __init__(self, account: SimAccount | None = None) -> None:
        self.order: dict | None = None    # the last order placed: {id, side, price, qty, cum, open, leverage, reduce_only}
        self.orders: dict[str, dict] = {} # every order by its cl_ord_id: a Query of an old leg still answers
        self.bids: list[tuple[Decimal, Decimal]] = []   # best first
        self.asks: list[tuple[Decimal, Decimal]] = []
        self.taken: dict[Decimal, Decimal] = {}         # price level -> qty our order took from it
        self.account = account or SimAccount()
        self.pair: core.Pair | None = None              # the pair of the chase: margin orders use its AssetPairs values
        self.refuse_margin_amends = False

    def on_book(self, bids, asks, now: float, symbol: str | None = None) -> list:
        """A new public book. symbol: the pair of the book (default: the pair of the chase); it sets the mark."""
        self.bids, self.asks = list(bids), list(asks)
        out = []
        o = self.order
        if o and o["open"]:
            buy = o["side"] == "buy"
            crossing = [(p, q) for p, q in (self.asks if buy else self.bids) if (p <= o["price"] if buy else p >= o["price"])]
            left = o["qty"] - o["cum"]
            take_total = Decimal(0)
            for p, q in crossing:
                take = min(left - take_total, max(q - self.taken.get(p, Decimal(0)), Decimal(0)))
                if take > 0:
                    self.taken[p] = self.taken.get(p, Decimal(0)) + take
                    take_total += take
            out = self._fill(take_total, now) if take_total > 0 else []
        symbol = symbol or (self.pair.symbol if self.pair else None)
        if symbol and self.bids and self.asks and self.account.mark(symbol, self.bids[0][0], self.asks[0][0], now):
            out.append(core.PositionGone(now, "liquidated"))
        return out

    def _crosses(self, side: str, price: Decimal) -> bool:
        if side == "buy":
            return bool(self.asks) and price >= self.asks[0][0]
        return bool(self.bids) and price <= self.bids[0][0]

    def reserved(self) -> Decimal:
        """Collateral that the open margin orders hold back (Kraken: free margin - mfo)."""
        return sum(((o["qty"] - o["cum"]) * o["price"] / o["leverage"] for o in self.orders.values()
                    if o["open"] and o["leverage"] and not o["reduce_only"]), ZERO)

    def read(self, now: float) -> dict:
        return self.account.read(now, self.reserved())

    def _margin_refusal(self, cmd, now: float) -> str | None:
        """Kraken's refusal of a margin order, or None."""
        p, acc, buy = self.pair, self.account, cmd.side == "buy"
        if cmd.reduce_only:
            return None if acc.position(p.symbol, "short" if buy else "long") else "EOrder:Reduce only:No position exists"
        if cmd.leverage not in (p.leverage_buy if buy else p.leverage_sell):
            return "EGeneral:Invalid arguments:leverage"
        if acc.position(p.symbol, "short" if buy else "long"):
            return "EOrder:Cannot open opposing position"
        pos = acc.position(p.symbol, "long" if buy else "short")
        limit = p.long_limit if buy else p.short_limit
        if limit is not None and (pos["qty"] if pos else ZERO) + cmd.qty > limit:
            return "EOrder:Margin position size exceeded"
        cost = cmd.qty * cmd.price
        if acc.borrowed() + cost - cost / cmd.leverage > acc.allowance:
            return "EOrder:Margin allowance exceeded"
        if cost / cmd.leverage > acc.read(now, self.reserved())["free_orders"]:
            return "EOrder:Insufficient margin"
        return None

    def send(self, cmd, now: float) -> list:
        o = self.order
        margin = isinstance(cmd, (core.MarginPlace, core.MarginIoc))
        if margin and (why := self._margin_refusal(cmd, now)):
            return [core.Rejected(now, "ioc" if isinstance(cmd, core.Ioc) else "place", why)]
        if isinstance(cmd, core.Place):
            if self._crosses(cmd.side, cmd.price):
                return [core.Rejected(now, "place", "would_cross")]
            self.order = self.orders[cmd.id] = {"id": cmd.id, "side": cmd.side, "price": cmd.price, "qty": cmd.qty,
                                                "cum": Decimal(0), "open": True,
                                                "leverage": cmd.leverage if margin else None,
                                                "reduce_only": margin and cmd.reduce_only}
            self.taken = {}
            return [core.Placed(now)]
        if isinstance(cmd, core.Amend):
            if not o or not o["open"] or o["id"] != cmd.id:
                return [core.Rejected(now, "amend", "not_open")]
            if o["leverage"] and self.refuse_margin_amends:
                return [core.Rejected(now, "amend", AMEND_REFUSED)]
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
            q = self.orders.get(cmd.id)
            if not q:
                return [core.OrderState(now, False, Decimal(0), None, cmd.id)]
            return [core.OrderState(now, q["open"], q["cum"], q["price"], cmd.id)]
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
            if isinstance(cmd, core.MarginIoc):   # reduce-only: at most the position
                take = self.account.fill(self.pair, cmd.side, cmd.leverage, cmd.reduce_only, take, price, False, now)
                if take == 0:
                    break
            out.append(core.Filled(now, take, price, maker=False, order="ioc", id=cmd.id))
            left -= take
        return out + [core.IocDone(now)]

    def on_trade(self, side: str, price: Decimal, qty: Decimal, now: float) -> list:
        o = self.order
        if not o or not o["open"]:
            return []
        through = (side == "sell" and price < o["price"]) if o["side"] == "buy" else (side == "buy" and price > o["price"])
        if not through:
            return []
        return self._fill(min(o["qty"] - o["cum"], qty), now)

    def _fill(self, take: Decimal, now: float) -> list:
        o = self.order
        if o["leverage"]:
            take = self.account.fill(self.pair, o["side"], o["leverage"], o["reduce_only"], take, o["price"], True, now)
        out = []
        if take > 0:
            o["cum"] += take
            if o["cum"] >= o["qty"]:
                o["open"] = False
            out.append(core.Filled(now, take, o["price"], maker=True, order="chase", cum=o["cum"], id=o["id"]))
        if o["open"] and o["reduce_only"] and not self.account.position(self.pair.symbol, "short" if o["side"] == "buy" else "long"):
            out.append(core.PositionGone(now, "nopos"))
        return out
