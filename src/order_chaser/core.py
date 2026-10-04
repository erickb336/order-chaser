"""The pure core of a chase: step(chase, event) -> (chase, commands).

No I/O and no clock: every event carries `now` (seconds). The shell sends the
commands to a gateway (the dry-run simulator now, the Kraken gateway in T4) and
feeds the gateway's answers back as events.

Phases (the chase states):
  placing -> resting <-> amending
  resting -> cancelling (timeout, "fill the rest now" or stop)
  cancelling -> reread -> ioc -> done        (timeout and "fill the rest now")
  cancelling -> done                         (stop; or the cancel is rejected 3 times)
  resting -> feed_lost -> reconcile -> resting
  feed_lost -> cancelling -> reread -> done  (timeout while the feed is lost: no IOC)
  amend rejected -> reconcile -> resting     (no blind retry)
  any phase -> done (outcome "ended") on a tool restart

Margin (dry run first; the T4 gateway maps the same commands):
  amend refused -> cancel and replace for the rest of the chase:
  resting -> cancelling -> (Canceled) -> reread -> (OrderState: the leg's cum) -> placing (a new leg) -> resting
  liquidated, or no position left for a reduce-only order -> cancel -> done

Each order of a chase is a leg with its own cl_ord_id: the chase id for the first leg, then
"<id>-1", "<id>-2", ... for the legs of a cancel and replace, and "<id>-i" for the IOC.
Filled = the sum of the venue's cum of each leg. All ids have at most 18 characters (Kraken's limit).
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, replace
from decimal import Decimal

MAKER_FEE = Decimal("0.004")
TAKER_FEE = Decimal("0.008")
RATE_MAX = 60
RATE_SLOW_ABOVE = 40
AMEND_EVERY = 5
AMEND_EVERY_SLOW = 15
TIMEOUTS = (30, 60, 120, 300, 600, 900)
OPEN_FEE = Decimal("0.0005")     # Kraken margin opening fee: 0.01% to 0.05% of the cost; the tool counts the maximum
ROLLOVER = Decimal("0.0005")     # rollover for each started 4 h: 0.01% to 0.05%; the tool counts the maximum
ROLL_EVERY = 4 * 3600
LEVERAGE = (2, 3, 4, 5)          # the owner's range; the pair's AssetPairs lists limit it further
ID_MAX = 18                      # Kraken: a free-text cl_ord_id has at most 18 characters
CANCEL_TRIES = 3
STALE_AFTER = 10      # seconds: a book older than this is no valid price (the feed sends a heartbeat each second)
SIM_VENUE = "the simulated exchange"
LIVE_VENUE = "Kraken"
ZERO = Decimal(0)


# ---------- Data ----------

@dataclass(frozen=True)
class Pair:
    symbol: str          # "BTC/USD", the WebSocket v2 name
    base: str
    quote: str
    tick: Decimal
    ordermin: Decimal
    costmin: Decimal
    price_decimals: int
    qty_decimals: int
    status: str          # Kraken pair status; a chase needs "online" (a margin close: "online" or "reduce_only")
    leverage_buy: tuple[int, ...] = ()    # AssetPairs leverage_buy: the levels of a margin long
    leverage_sell: tuple[int, ...] = ()   # AssetPairs leverage_sell: the levels of a margin short
    margin_call: int = 0                  # AssetPairs margin_call: margin level (%) of a margin call
    margin_stop: int = 0                  # AssetPairs margin_stop: margin level (%) of a liquidation
    long_limit: Decimal | None = None     # AssetPairs long_position_limit (base units)
    short_limit: Decimal | None = None    # AssetPairs short_position_limit (base units)

    def leverage(self, side: str) -> tuple[int, ...]:
        """The levels that a margin open on this side can use: the pair's list, within the owner's 2x to 5x."""
        return tuple(x for x in (self.leverage_buy if side == "buy" else self.leverage_sell) if x in LEVERAGE)


@dataclass(frozen=True)
class Fill:
    qty: Decimal
    price: Decimal
    maker: bool
    t: float             # seconds since the chase started
    order: str           # "chase" or "ioc"
    leg: str = ""        # cl_ord_id of the order that filled


@dataclass(frozen=True)
class Margin:
    """A margin chase: an open (long = buy, short = sell) or a reduce-only close of a position."""
    leverage: int
    close: bool = False
    pos_qty: Decimal = ZERO      # close: the size of the position at the start
    entry: Decimal = ZERO        # close: the average entry price of the position
    rollover: Decimal = ZERO     # close: rollover so far at the start (estimate)


@dataclass(frozen=True)
class Position:
    """An open margin position, one per pair and direction (read from the account; simulated in a dry run)."""
    pair: str
    dir: str             # "long" or "short"
    qty: Decimal
    entry: Decimal
    leverage: int


@dataclass(frozen=True)
class Chase:
    id: str              # the chase id; also the cl_ord_id of the first leg
    pair: Pair
    side: str            # "buy" or "sell"
    qty: Decimal
    limit: Decimal       # cap (buy) or floor (sell)
    start_bid: Decimal
    start_ask: Decimal
    timeout: int
    started: float
    venue: str           # SIM_VENUE in a dry run, LIVE_VENUE in live
    phase: str = "placing"
    price: Decimal | None = None      # price of the resting order; None when no order rests (also when done)
    pending: Decimal | None = None    # price of a place or amend in flight
    placed_at: float | None = None
    last_amend_at: float | None = None
    fills: tuple[Fill, ...] = ()      # the venue's filled qty is the truth: see _venue_cum()
    exit: str | None = None           # "timeout", "fillnow" or "stop", once asked for
    bid: Decimal | None = None
    ask: Decimal | None = None
    book_ok: bool = True
    book_at: float | None = None      # time of the last valid book (a heartbeat re-sends the book)
    feed_ok: bool = True
    rate: float = 0.0                 # estimated Kraken rate counter for this pair
    rate_at: float = 0.0
    slow: bool = False                # amends every 15 s, not 5 s
    reject: str | None = None         # reason of the last amend reject
    reject_at: float | None = None
    reject_price: Decimal | None = None
    reconcile_for: str | None = None  # "feed" or "reject"
    cancel_tries: int = 0             # cancels sent for the chase order
    cancel_wait: bool = False         # a cancel was rejected and the order is open: retry when the rate allows
    outcome: str | None = None        # filled, notfilled, belowmin, stopped, ended, refused, cancelfail
    ended_at: float | None = None
    end_ask: Decimal | None = None    # the ask (buy) or bid (sell) when the chase ended; None without a valid price
    off_from: float | None = None     # last event before the tool stopped (outcome "ended")
    legs: tuple[str, ...] = ()        # cl_ord_ids of the chase legs, oldest first; the last one is the current leg
    margin: Margin | None = None
    replace: bool = False             # margin: the venue refused an amend, so moves are cancel and replace

    @property
    def buy(self) -> bool:
        return self.side == "buy"

    @property
    def filled(self) -> Decimal:
        return sum((f.qty for f in self.fills), ZERO)

    @property
    def leg(self) -> str:
        """cl_ord_id of the current chase leg."""
        return self.legs[-1] if self.legs else self.id

    @property
    def ioc_id(self) -> str:
        return self.id + "-i"

    def leg_cum(self, leg: str) -> Decimal:
        """What the tool counted for one leg: the venue's cum of that leg."""
        return sum((f.qty for f in self.fills if f.leg == leg), ZERO)

    @property
    def dir(self) -> str | None:
        """Margin: the direction of the position ("long" or "short"); None for spot."""
        if self.margin is None:
            return None
        return "long" if self.buy != self.margin.close else "short"

    @property
    def remainder(self) -> Decimal:
        return max(self.qty - self.filled, ZERO)

    @property
    def rest_below_min(self) -> bool:
        """The rest is below a Kraken minimum: no order for it is possible."""
        rest = self.remainder
        return rest > 0 and (rest < self.pair.ordermin or rest * self.limit < self.pair.costmin)

    @property
    def dry(self) -> bool:
        return self.venue != LIVE_VENUE

    def fresh(self, now: float) -> bool:
        """A valid price: the feed is up, the book checksum matched, and the book is at most STALE_AFTER s old."""
        return self.feed_ok and self.book_ok and self.book_at is not None and now - self.book_at <= STALE_AFTER


# ---------- Events (input) ----------

@dataclass(frozen=True)
class Tick:
    now: float

@dataclass(frozen=True)
class Book:
    now: float
    bid: Decimal | None
    ask: Decimal | None
    ok: bool             # False while the checksum is wrong (stale book)

@dataclass(frozen=True)
class FeedLost:
    now: float

@dataclass(frozen=True)
class FeedBack:
    now: float

@dataclass(frozen=True)
class Placed:
    now: float

@dataclass(frozen=True)
class Amended:
    now: float

@dataclass(frozen=True)
class Rejected:
    now: float
    op: str              # place, amend, cancel, ioc
    reason: str          # would_cross, rate_limit, not_open, or the venue's text

@dataclass(frozen=True)
class Filled:
    """A fill report of the gateway.

    Gateway contract (the simulator now, the Kraken gateway in T4): a fill of the chase order
    MUST carry `cum`, the venue's filled qty of that order after the fill (Kraken: cum_qty of the
    executions channel), and `id`, the cl_ord_id of the leg. The core counts `cum` minus what it
    already counted for that leg, so a report that a reread already counted adds nothing, and a late
    fill of an old leg counts once, for its own leg. The core logs and ignores the qty of a chase fill
    without `cum`: it cannot know whether that qty is already counted. An IOC fill counts its qty.
    No `id`: the current leg.
    """
    now: float
    qty: Decimal
    price: Decimal
    maker: bool
    order: str = "chase"
    cum: Decimal | None = None
    id: str | None = None

@dataclass(frozen=True)
class Canceled:
    now: float

@dataclass(frozen=True)
class OrderState:
    now: float
    open: bool
    cum_qty: Decimal
    price: Decimal | None    # the order's limit price, also when it is closed; None for an unknown order
    id: str | None = None    # cl_ord_id of the leg; None: the current leg

@dataclass(frozen=True)
class PositionGone:
    """Margin: the position of the chase is gone. reason "liquidated": the venue liquidated it (the margin
    level fell to margin_stop); "nopos": it closed another way, so a reduce-only order has nothing left."""
    now: float
    reason: str

@dataclass(frozen=True)
class IocDone:
    now: float

@dataclass(frozen=True)
class UserStop:
    now: float

@dataclass(frozen=True)
class UserFillNow:
    now: float

@dataclass(frozen=True)
class Restarted:
    now: float
    last_seen: float     # time of the last event before the tool stopped


# ---------- Commands (output) ----------

@dataclass(frozen=True)
class Place:
    id: str
    side: str
    price: Decimal
    qty: Decimal

@dataclass(frozen=True)
class Amend:
    id: str
    price: Decimal

@dataclass(frozen=True)
class Cancel:
    id: str

@dataclass(frozen=True)
class Query:
    id: str

@dataclass(frozen=True)
class Ioc:
    id: str
    side: str
    price: Decimal
    qty: Decimal

@dataclass(frozen=True)
class MarginPlace(Place):
    """A post-only margin order: the first leg, or a new leg of a cancel and replace.
    Live (T4): REST AddOrder with leverage and reduce_only; its amend and cancel go by cl_ord_id on WS v2."""
    leverage: int
    reduce_only: bool

@dataclass(frozen=True)
class MarginIoc(Ioc):
    """The IOC of a margin chase. Live (T4): REST AddOrder with leverage and reduce_only."""
    leverage: int
    reduce_only: bool

@dataclass(frozen=True)
class Log:
    t: float             # seconds since the chase started
    text: str
    kind: str = ""       # "", you, fill, warn, bad


# ---------- Formatting ----------

def fmt_price(pair: Pair, p: Decimal) -> str:
    return f"{p:,.{max(2, pair.price_decimals)}f}"


def fmt_qty(q: Decimal) -> str:
    s = f"{q:.8f}".rstrip("0")
    whole, frac = s.split(".")
    return f"{whole}.{frac.ljust(4, '0')}"


# ---------- Validation and start ----------

def validate(pair: Pair, side: str, qty: Decimal, limit: Decimal | None,
             bid: Decimal | None, ask: Decimal | None, book_ok: bool, timeout: int, close: bool = False) -> list[str]:
    """The reasons that block a start. An empty list means the chase can start.
    close: a margin close, which Kraken also accepts when the pair is "reduce_only"."""
    errors = []
    if pair.status != "online" and not (close and pair.status == "reduce_only"):
        errors.append(f"Kraken accepts no new chase for {pair.symbol} now. Pair status: {pair.status}.")
    if side not in ("buy", "sell"):
        errors.append("Pick buy or sell.")
    if bid is None or ask is None or not book_ok:
        errors.append("Start needs live prices.")
    if timeout not in TIMEOUTS:
        errors.append("Pick a timeout from the list: 30 s to 15 min.")
    if qty <= 0 or qty.normalize().as_tuple().exponent < -pair.qty_decimals:
        errors.append(f"Enter an amount above 0 with at most {pair.qty_decimals} decimals.")
    elif ask is not None and bid is not None:
        ref = ask if side == "buy" else bid
        if qty < pair.ordermin or qty * ref < pair.costmin:
            errors.append(f"The amount is below the Kraken minimums for {pair.symbol}: "
                          f"at least {fmt_qty(pair.ordermin)} {pair.base} and {pair.costmin} {pair.quote}.")
    if limit is not None and limit <= 0:
        errors.append(f"The {'cap' if side == 'buy' else 'floor'} must be above 0.")
    elif limit is not None and bid is not None and ask is not None:
        if limit % pair.tick != 0:
            errors.append(f"The limit must be a multiple of {pair.tick}.")
        elif side == "buy" and limit < ask:
            errors.append("A higher limit must be at or above the ask now.")
        elif side == "sell" and limit > bid:
            errors.append("A lower limit must be at or below the bid now.")
    return errors


def validate_margin(pair: Pair, what: str, qty: Decimal, leverage: int | None, ref: Decimal | None,
                    positions: list[Position], free_orders: Decimal) -> list[str]:
    """The margin reasons that block a start, beside validate(). what: "long", "short", "close-long" or
    "close-short". ref: the ask (long) or bid (short) now. free_orders: the account's free margin for
    new orders (Kraken TradeBalance mfo)."""
    pos = {p.dir: p for p in positions if p.pair == pair.symbol}
    if what.startswith("close-"):
        d = what[6:]
        p = pos.get(d)
        if p is None:
            return [f"You have no open {d} position on {pair.symbol}."]
        if qty > p.qty:
            return [f"A close cannot be larger than the position, {fmt_qty(p.qty)} {pair.base}. Reduce-only orders "
                    f"never grow or flip a position. Enter {fmt_qty(p.qty)} or less."]
        return []
    side = "buy" if what == "long" else "sell"
    allowed = pair.leverage(side)
    if not allowed:
        return [f"Kraken allows no {what} on {pair.symbol}."]
    if leverage not in allowed:
        return [f"Pick a leverage that {pair.symbol} allows: {', '.join(f'{x}x' for x in allowed)}."]
    other = "short" if what == "long" else "long"
    if other in pos:
        return [f"Close the {other} first. You have an open {other} position on {pair.symbol}."]
    if ref is not None and qty > 0:
        need = qty * ref / leverage
        if need > free_orders:
            return [f"This open needs {need:,.2f} {pair.quote} of collateral. Your free margin for new orders is "
                    f"{free_orders:,.2f} {pair.quote}. Kraken refuses an open without enough margin. Make the amount smaller."]
    return []


def _order(c: Chase, id: str, price: Decimal, qty: Decimal, ioc: bool = False):
    """The command for a new leg (or the IOC): spot, or margin with leverage and reduce_only."""
    if c.margin is None:
        return (Ioc if ioc else Place)(id, c.side, price, qty)
    return (MarginIoc if ioc else MarginPlace)(id, c.side, price, qty, c.margin.leverage, c.margin.close)


def begin(id: str, pair: Pair, side: str, qty: Decimal, bid: Decimal, ask: Decimal,
          timeout: int, now: float, venue: str, limit: Decimal | None = None,
          rate: float = 0.0, margin: Margin | None = None) -> tuple[Chase, list]:
    """Start a chase that passed validate() (and validate_margin()): record the cap and place the post-only order."""
    assert len(id) + 4 <= ID_MAX, id   # room for the legs "<id>-999" and the IOC "<id>-i"
    buy = side == "buy"
    cap = limit if limit is not None else (ask if buy else bid)
    price = bid if buy else ask
    c = Chase(id=id, pair=pair, side=side, qty=qty, limit=cap, start_bid=bid, start_ask=ask,
              timeout=timeout, started=now, venue=venue, pending=price, bid=bid, ask=ask, book_at=now,
              rate=min(float(RATE_MAX), rate + 1), rate_at=now, legs=(id,), margin=margin)
    word = "cap" if buy else "floor"
    out = []
    if margin is not None and margin.close:
        out.append(Log(0, f"Read the position: {fmt_qty(margin.pos_qty)} {pair.base} {c.dir}, {margin.leverage}x."))
    if limit is None:
        out.append(Log(0, f"Recorded the start {'ask' if buy else 'bid'}, {fmt_price(pair, cap)}, as the {word}."))
    else:
        out.append(Log(0, f"Recorded your limit, {fmt_price(pair, cap)}, as the {word}."))
    return c, out + [_order(c, id, price, qty),
                     Log(0, f"Sending a {_order_text(c, qty, price)}…", "you")]


# ---------- Rate counter (Kraken Starter tier, estimated) ----------

def amend_cost(age: float) -> int:
    return 1 + (3 if age < 5 else 2 if age < 10 else 1 if age < 15 else 0)


def cancel_cost(age: float) -> int:
    for limit, cost in ((5, 8), (10, 6), (15, 5), (45, 4), (90, 2), (300, 1)):
        if age < limit:
            return cost
    return 0


def _decay(c: Chase, now: float) -> Chase:
    if now <= c.rate_at:
        return c
    return replace(c, rate=max(0.0, c.rate - (now - c.rate_at)), rate_at=now)


def _add_rate(c: Chase, cost: int) -> Chase:
    return replace(c, rate=min(float(RATE_MAX), c.rate + cost))


# ---------- The step function ----------

ACTIVE = ("placing", "resting", "amending", "feed_lost", "reconcile")
GONE = ("liquidated", "nopos")   # exits of a margin chase whose position is gone; each is also the outcome
REPLACE_IN_FLIGHT = ("cancelling", "reread")   # with exit None: a cancel and replace


def _venue(c: Chase) -> str:
    return c.venue[0].upper() + c.venue[1:]


def _order_text(c: Chase, qty: Decimal, price: Decimal) -> str:
    """ "post-only buy, reduce-only, 0.0300 BTC at 62,417.90" or "post-only buy, 0.0500 BTC at 62,417.90, leverage 3x"."""
    m = c.margin
    ro = ", reduce-only" if m and m.close else ""
    lev = f", leverage {m.leverage}x" if m and not m.close else ""
    return f"post-only {c.side}{ro}, {fmt_qty(qty)} {c.pair.base} at {fmt_price(c.pair, price)}{lev}"


def _position_text(c: Chase) -> str:
    """After a margin fill: what the position of this chase is now."""
    m, base = c.margin, c.pair.base
    if not m.close:
        return f" Position opened: {fmt_qty(c.filled)} {base} {c.dir}, {m.leverage}x."
    rest = m.pos_qty - c.filled
    return f" Position partly closed: {fmt_qty(rest)} {base} {c.dir} stays open." if rest > 0 else " Position closed."


def step(c: Chase, ev) -> tuple[Chase, list]:
    if c.phase == "done":
        return c, []
    c = _decay(c, ev.now)
    out: list = []
    t = ev.now - c.started
    p = lambda x: fmt_price(c.pair, x)
    base = c.pair.base
    venue = _venue(c)

    if isinstance(ev, Book):
        if c.book_ok and not ev.ok:
            out.append(Log(t, "The order book checksum did not match. Reading the book again. No amend until the book is valid.", "warn"))
        c = replace(c, bid=ev.bid, ask=ev.ask, book_ok=ev.ok, book_at=ev.now if ev.ok else c.book_at)
        if c.phase == "resting":
            c, more = _maybe_amend(c, ev.now)
            out += more

    elif isinstance(ev, Tick):
        if c.exit is None and t >= c.timeout and c.phase in ("resting", "feed_lost") + REPLACE_IN_FLIGHT:
            c = replace(c, exit="timeout")
            out.append(Log(t, "Timeout. The tool places no new order and fills the rest with one IOC." if c.phase in REPLACE_IN_FLIGHT
                           else "Timeout. Cancelling the resting order." if c.phase == "resting"
                           else "Timeout while the price feed is lost. Cancelling the order.", "warn"))
        if c.phase in ("resting", "feed_lost"):
            c, more = _settle(c, ev.now)
            out += more
        elif c.phase == "cancelling" and c.cancel_wait:
            c, more = _cancel(c, ev.now)
            out += more

    elif isinstance(ev, FeedLost):
        if c.feed_ok:
            frozen = f" Order frozen at {p(c.price)}." if c.price is not None and c.phase in ACTIVE else ""
            out.append(Log(t, f"Lost the public Kraken price feed.{frozen}", "bad"))
        c = replace(c, feed_ok=False, book_ok=False)
        if c.phase == "resting":
            c = replace(c, phase="feed_lost")

    elif isinstance(ev, FeedBack):
        c = replace(c, feed_ok=True)
        if c.phase == "feed_lost" and c.price is None and c.replace:   # no open leg: nothing to read again
            c = replace(c, phase="resting")
            out.append(Log(t, "The price feed is back. The tool places the new order when the book is valid."))
        elif c.phase == "feed_lost":
            c = replace(c, phase="reconcile", reconcile_for="feed")
            out += [Log(t, "The price feed is back. Reading the order again."), Query(c.leg)]

    elif isinstance(ev, Placed) and c.phase == "placing":
        c = replace(c, price=c.pending, pending=None, placed_at=ev.now)
        out.append(Log(t, f"Placed a {_order_text(c, c.remainder, c.price)}.", "you"))
        c, more = _resume(c, ev.now)
        out += more

    elif isinstance(ev, Amended) and c.phase == "amending":
        word = "Best bid rose" if c.buy else "Best ask fell"
        c = replace(c, price=c.pending, pending=None, reject=None, reject_at=None)
        out.append(Log(t, f"{word} to {p(c.price)}. Amended the order to {p(c.price)}.", "you"))
        c, more = _resume(c, ev.now)
        out += more

    elif isinstance(ev, Rejected):
        c, more = _rejected(c, ev, t)
        out += more

    elif isinstance(ev, Filled):
        leg = ev.id or (c.ioc_id if ev.order == "ioc" else c.leg)
        if ev.order == "chase" and ev.cum is None:
            out.append(Log(t, f"{venue} reported a fill of {fmt_qty(ev.qty)} {base} with no filled total. "
                              "The tool did not count it. It counts the filled total that the venue reports.", "warn"))
            return c, out
        # A chase leg counts the venue's cum of that leg: a fill that a reread already counted adds nothing.
        qty = ev.cum - c.leg_cum(leg) if ev.order == "chase" else ev.qty
        if qty <= 0:
            return c, out
        c = replace(c, fills=c.fills + (Fill(qty, ev.price, ev.maker, t, ev.order, leg),))
        complete = c.filled >= c.qty
        kind = "maker" if ev.maker else "taker, IOC"
        pos = _position_text(c) if c.margin else ""
        done = " Order complete." if complete and pos != " Position closed." else ""
        out.append(Log(t, f"Filled {fmt_qty(qty)} {base} at {p(ev.price)} ({kind}).{done}{pos}", "fill"))
        if complete and c.phase != "ioc":
            c, more = _end(c, ev.now, "filled")
            out += more

    elif isinstance(ev, Canceled) and c.phase == "cancelling":
        c, more = _after_cancel(c, ev.now)
        out += more

    elif isinstance(ev, OrderState):
        c, more = _order_state(c, ev, t)
        out += more

    elif isinstance(ev, PositionGone) and c.margin is not None and c.exit not in GONE:
        out.append(Log(t, f"{venue} liquidated the position: the account margin level fell to {c.pair.margin_stop}%."
                          if ev.reason == "liquidated" else
                          f"The {c.dir} position on {c.pair.symbol} is closed. The reduce-only order has nothing left to close.", "bad"))
        c = replace(c, exit=ev.reason)
        if c.phase in ("resting", "feed_lost"):
            c, more = _settle(c, ev.now)
            out += more
        # In the other phases the answer in flight leads to _settle, _after_cancel, the reread or IocDone: each ends the chase.

    elif isinstance(ev, IocDone) and c.phase == "ioc":
        got = sum((f.qty for f in c.fills if f.order == "ioc"), ZERO)
        if c.filled >= c.qty:
            c, more = _end(c, ev.now, "filled")
        else:
            side_px = "ask" if c.buy else "bid"
            now_px = c.ask if c.buy else c.bid
            what = "nothing filled" if got == 0 else f"filled {fmt_qty(got)} {base}"
            tail = f" The {side_px} is {p(now_px)}." if now_px is not None else ""
            ro = ", reduce-only" if c.margin and c.margin.close else ""
            out.append(Log(t, f"IOC {c.side}{ro}, {fmt_qty(c.qty - c.filled + got)} {base} at {p(c.limit)}: {what}.{tail}", "bad"))
            c, more = _end(c, ev.now, "notfilled")
        out += more

    elif isinstance(ev, UserStop) and ((c.phase in ACTIVE and c.exit not in ("stop",) + GONE)
                                       or (c.phase in REPLACE_IN_FLIGHT and c.exit is None)):
        c = replace(c, exit="stop")
        out.append(Log(t, "You pressed Stop. Cancelling the order."))
        c, more = _settle(c, ev.now)
        out += more

    elif isinstance(ev, UserFillNow) and c.exit is None and c.phase in ("placing", "resting", "amending", "reconcile") + REPLACE_IN_FLIGHT:
        c = replace(c, exit="fillnow")
        out.append(Log(t, "You pressed \"Fill the rest now\". Cancelling the resting order.", "warn"))
        c, more = _settle(c, ev.now)
        out += more

    elif isinstance(ev, Restarted):
        c, more = _restarted(c, ev, t)
        out += more

    return c, out


def _resume(c: Chase, now: float) -> tuple[Chase, list]:
    return _settle(replace(c, phase="resting"), now)


def _settle(c: Chase, now: float) -> tuple[Chase, list]:
    """Decide the next move of a resting (or frozen) order."""
    if c.filled >= c.qty:
        return _end(c, now, "filled")
    if c.phase not in ("resting", "feed_lost"):
        return c, []
    if c.exit:
        if c.price is None and c.replace:   # a replace waits for a price: no leg is open, nothing to cancel
            if c.exit == "stop" or c.exit in GONE:
                return _end(c, now, "stopped")
            return replace(c, phase="reread"), [Query(c.leg)]
        return _cancel(replace(c, phase="cancelling"), now)
    if not c.feed_ok:
        return replace(c, phase="feed_lost"), []
    return _maybe_amend(c, now)


def _cancel(c: Chase, now: float) -> tuple[Chase, list]:
    """Send a cancel of the chase order when the estimated rate counter has room for it; else wait for a Tick."""
    cost = cancel_cost(now - (c.placed_at or now))
    if c.cancel_tries and c.rate + cost > RATE_MAX:
        return replace(c, cancel_wait=True), []
    c = _add_rate(replace(c, cancel_wait=False, cancel_tries=c.cancel_tries + 1), cost)
    out = [Log(now - c.started, f"Sending the cancel again (try {c.cancel_tries} of {CANCEL_TRIES}).", "warn")] if c.cancel_tries > 1 else []
    return c, out + [Cancel(c.leg)]


def _maybe_amend(c: Chase, now: float) -> tuple[Chase, list]:
    """Move the order to the best price: an amend, or a cancel and replace after the venue refused an amend."""
    out: list = []
    slow = c.rate > RATE_SLOW_ABOVE
    if slow != c.slow:
        c = replace(c, slow=slow)
        if slow:
            out.append(Log(now - c.started, f"Estimated rate counter at {int(c.rate)} of {RATE_MAX}. Next amend in {AMEND_EVERY_SLOW} s, not {AMEND_EVERY} s.", "warn"))
    if not c.fresh(now) or c.exit:
        return c, out
    target = _target(c)
    every = AMEND_EVERY_SLOW if c.slow else AMEND_EVERY
    if c.price is None:
        # The replace waits for a valid price: the next leg, at most one new leg each `every` s.
        if c.replace and target is not None and now - (c.last_amend_at or 0) >= every:
            c, more = _place_leg(c, now, target)
            out += more
        return c, out
    if target is None or not (target > c.price if c.buy else target < c.price):
        return c, out
    if now - max(c.placed_at or 0, c.last_amend_at or 0) < every:
        return c, out
    age = now - (c.placed_at or now)
    word = "Best bid rose" if c.buy else "Best ask fell"
    if c.replace:
        cost = cancel_cost(age)
        if c.rate + cost + 1 + cancel_cost(0) > RATE_MAX:   # room for the cancel, the new leg and its cancel, or wait
            return c, out
        c = _add_rate(replace(c, phase="cancelling", pending=target, cancel_tries=1, cancel_wait=False), cost)
        return c, out + [Log(now - c.started, f"{word} to {fmt_price(c.pair, target)}. Cancelling the order at "
                                              f"{fmt_price(c.pair, c.price)} to place a new one (cancel and replace).", "you"),
                         Cancel(c.leg)]
    c = _add_rate(replace(c, phase="amending", pending=target, last_amend_at=now), amend_cost(age))
    return c, out + [Amend(c.leg, target)]


def _target(c: Chase) -> Decimal | None:
    """The best price within the cap (floor) that a post-only order can take now; None if there is none."""
    best = c.bid if c.buy else c.ask
    if best is None:
        return None
    target = min(best, c.limit) if c.buy else max(best, c.limit)
    other = c.ask if c.buy else c.bid
    if other is not None and (target >= other if c.buy else target <= other):
        return None          # a post-only order there would cross: wait for the book
    return target


def _place_leg(c: Chase, now: float, price: Decimal) -> tuple[Chase, list]:
    """Cancel and replace: place a new leg for the rest, qty - the sum of the legs' cum, when the rate counter has
    room for it and for its cancel. A new leg is a move: it starts the wait for the next move (last_amend_at)."""
    if c.rate + 1 + cancel_cost(0) > RATE_MAX:
        return c, []
    leg = f"{c.id}-{len(c.legs)}"
    rest = c.remainder
    c = _add_rate(replace(c, phase="placing", pending=price, legs=c.legs + (leg,), cancel_tries=0, last_amend_at=now), 1)
    return c, [_order(c, leg, price, rest), Log(now - c.started, f"Placing a {_order_text(c, rest, price)}…", "you")]


def _rejected(c: Chase, ev: Rejected, t: float) -> tuple[Chase, list]:
    p = lambda x: fmt_price(c.pair, x)
    why = {"would_cross": f"would cross the {'ask' if c.buy else 'bid'} (post-only)",
           "rate_limit": "EOrder:Rate limit exceeded",
           "not_open": "the order is not open"}.get(ev.reason, ev.reason)
    if ev.op == "place" and c.phase == "placing":
        if len(c.legs) > 1 and ev.reason == "would_cross" and c.exit is None:   # a new leg: wait for the book
            return replace(c, phase="resting", pending=None), [
                Log(t, f"The new order would cross the {'ask' if c.buy else 'bid'} (post-only). The tool waits for the book.", "warn")]
        c = replace(c, pending=None)
        out = [Log(t, f"{_venue(c)} rejected the order: {why}.", "bad")]
        c, more = _end(c, ev.now, "refused" if len(c.legs) == 1 else "notfilled")
        return c, out + more
    if ev.op == "amend" and c.phase == "amending":
        if c.margin is not None and ev.reason not in ("would_cross", "rate_limit", "not_open"):
            # The venue refuses amends of this margin order: cancel and replace for the rest of the chase.
            out = [Log(t, f"Amend to {p(c.pending)} refused: \"{ev.reason}\". Switched to cancel and replace for this chase.", "warn")]
            c = replace(c, phase="resting", replace=True, pending=None, reject=ev.reason, reject_at=ev.now,
                        reject_price=c.pending, last_amend_at=None)
            c, more = _settle(c, ev.now)
            return c, out + more
        out = [Log(t, f"Amend to {p(c.pending)} rejected: {why}.", "warn"), Query(c.leg)]
        c = replace(c, phase="reconcile", reconcile_for="reject", pending=None, reject=ev.reason, reject_at=ev.now,
                    reject_price=c.pending)
        if ev.reason == "rate_limit":
            # The estimate was too low: trust Kraken and start again from the maximum.
            c = replace(c, rate=float(RATE_MAX), slow=True)
        return c, out
    if ev.op == "cancel" and c.phase == "cancelling":
        if ev.reason == "rate_limit":
            # As for an amend: trust Kraken. The next try waits until the counter has room (see _cancel).
            c = replace(c, rate=float(RATE_MAX), slow=True)
        return c, [Log(t, f"Cancel rejected: {why}. Reading the order again.", "warn"), Query(c.leg)]
    if ev.op == "ioc" and c.phase == "ioc":
        out = [Log(t, f"The IOC was rejected: {why}.", "bad")]
        c, more = _end(c, ev.now, "notfilled")
        return c, out + more
    return c, []


def _after_cancel(c: Chase, now: float) -> tuple[Chase, list]:
    t, venue = now - c.started, _venue(c)
    if c.exit == "stop" or c.exit in GONE:
        out = [Log(t, f"{venue} confirmed the cancel. Filled: {fmt_qty(c.filled)} {c.pair.base}.")]
        c, more = _end(c, now, "filled" if c.filled >= c.qty else "stopped")
        return c, out + more
    c = replace(c, phase="reread", price=None)
    return c, [Log(t, f"{venue} confirmed the cancel."), Query(c.leg)]


def _order_state(c: Chase, ev: OrderState, t: float) -> tuple[Chase, list]:
    p = lambda x: fmt_price(c.pair, x)
    base = c.pair.base
    venue = _venue(c)
    c, out = _venue_cum(c, ev, t)
    if c.phase == "cancelling" and not ev.open:
        c, more = _after_cancel(c, ev.now)
        return c, out + more
    if c.phase == "cancelling":            # the cancel was rejected and the order is still open
        if c.cancel_tries >= CANCEL_TRIES:
            todo = ("Nothing to check in Kraken Pro: a dry run sends no orders." if c.dry
                    else "Check Kraken Pro for an open order and cancel it there.")
            out.append(Log(t, f"{venue} rejected the cancel {CANCEL_TRIES} times and the order is still open. The tool "
                              f"stopped the chase. {todo}", "bad"))
            c, more = _end(c, ev.now, "cancelfail")
        else:
            c, more = _cancel(c, ev.now)
        return c, out + more
    if c.phase == "reconcile":
        state = "open" if ev.open else "closed"
        at = f", at {p(ev.price)}" if ev.open and ev.price is not None else ""
        out.append(Log(t, f"Read the order again: {state}, {fmt_qty(ev.cum_qty)} {base} filled{at}."))
        if not ev.open:
            c, more = _end(c, ev.now, "filled" if c.remainder == 0 else "notfilled")
            return c, out + more
        c, more = _resume(replace(c, price=ev.price, reconcile_for=None), ev.now)
        return c, out + more
    if c.phase == "reread":
        rest = c.remainder
        out.append(Log(t, f"Read the filled quantity again: {fmt_qty(c.qty - rest)} {base}."))
        if rest == 0:
            c, more = _end(c, ev.now, "filled")
            return c, out + more
        if c.exit == "stop" or c.exit in GONE:     # during a cancel and replace
            c, more = _end(c, ev.now, "stopped")
            return c, out + more
        if c.exit is None and ev.now - c.started >= c.timeout:
            c = replace(c, exit="timeout")
            out.append(Log(t, "Timeout. The tool places no new order and fills the rest with one IOC.", "warn"))
        below = Log(t, f"The rest, {fmt_qty(rest)} {base}, is below the Kraken minimum for {c.pair.symbol} "
                       f"({fmt_qty(c.pair.ordermin)} {base} or {c.pair.costmin} {c.pair.quote}). It counts as not filled.", "bad")
        if c.exit is None:                          # cancel and replace: the next leg, for the rest
            if c.rest_below_min:
                c, more = _end(c, ev.now, "belowmin")
                return c, out + [below] + more
            c, more = _resume(replace(c, cancel_tries=0), ev.now)
            return c, out + more
        if not c.fresh(ev.now):
            why = "the price feed is lost" if not c.feed_ok else "the order book is not valid now"
            out.append(Log(t, f"No IOC: {why}, so there is no valid price. The rest counts as not filled.", "bad"))
            c, more = _end(c, ev.now, "notfilled")
            return c, out + more
        if c.rest_below_min:
            c, more = _end(c, ev.now, "belowmin")
            return c, out + [below] + more
        c = _add_rate(replace(c, phase="ioc"), 1)
        ro = ", reduce-only" if c.margin and c.margin.close else ""
        lev = f", leverage {c.margin.leverage}x" if c.margin and not c.margin.close else ""
        out += [_order(c, c.ioc_id, c.limit, rest, ioc=True),
                Log(t, f"Sending an IOC {c.side}{ro}, {fmt_qty(rest)} {base} at {p(c.limit)}{lev}…", "warn")]
        return c, out
    return c, out


def _venue_cum(c: Chase, ev: OrderState, t: float) -> tuple[Chase, list]:
    """The venue's filled qty of each leg is the truth. Record a fill that the tool did not see, at the order's price."""
    leg = ev.id or c.leg
    missing = ev.cum_qty - c.leg_cum(leg)
    if missing <= 0:
        return c, []
    price = ev.price if ev.price is not None else c.limit   # no price from the venue: count the worst price
    c = replace(c, fills=c.fills + (Fill(missing, price, True, t, "chase", leg),))
    return c, [Log(t, f"{_venue(c)} reports {fmt_qty(missing)} {c.pair.base} more filled than the tool saw. "
                      f"Recorded it at {fmt_price(c.pair, price)} (maker).", "fill")]


def _restarted(c: Chase, ev: Restarted, t: float) -> tuple[Chase, list]:
    base = c.pair.base
    off = int(ev.now - ev.last_seen)
    if c.dry:
        lines = [Log(t, f"Tool started again after {off} s off. Ended the simulated order. No order was on Kraken.", "bad"),
                 Log(t, f"Recorded the simulated fills: {fmt_qty(c.filled)} of {fmt_qty(c.qty)} {base}."),
                 Log(t, "Did not continue the dry run.")]
    else:  # T4 replaces this with a reconcile against Kraken before it ends the chase.
        lines = [Log(t, f"Tool started again after {off} s off. Chase ended, not continued.", "bad")]
    c, more = _end(replace(c, off_from=ev.last_seen), ev.now, "ended")
    return c, lines + more


def _end(c: Chase, now: float, outcome: str) -> tuple[Chase, list]:
    if c.exit in GONE and outcome != "filled":
        outcome = c.exit         # the position is gone: that ends the chase, whatever the path to the end
    end_px = (c.ask if c.buy else c.bid) if c.fresh(now) else None   # no valid price: feed lost or book stale
    return replace(c, phase="done", outcome=outcome, ended_at=now, price=None, pending=None, end_ask=end_px), []   # no order rests


# ---------- Result numbers ----------

def summary(c: Chase) -> dict:
    """Filled qty, average price, fees and the saving against a market order at the start.

    Market order at the start, for the filled qty: buy = q x start ask x (1 + taker);
    sell = q x start bid x (1 - taker), received. Saving = market - chase cost (buy),
    chase proceeds - market (sell).
    """
    q = c.filled
    gross = sum((f.qty * f.price for f in c.fills), ZERO)
    fee = sum((f.qty * f.price * (MAKER_FEE if f.maker else TAKER_FEE) for f in c.fills), ZERO)
    mq = sum((f.qty for f in c.fills if f.maker), ZERO)
    ref = c.start_ask if c.buy else c.start_bid

    def saving(fills) -> Decimal:
        fq = sum((f.qty for f in fills), ZERO)
        g = sum((f.qty * f.price for f in fills), ZERO)
        fe = sum((f.qty * f.price * (MAKER_FEE if f.maker else TAKER_FEE) for f in fills), ZERO)
        if c.buy:
            return fq * ref * (1 + TAKER_FEE) - (g + fe)
        return (g - fe) - fq * ref * (1 - TAKER_FEE)

    market = q * ref * (1 + TAKER_FEE) if c.buy else q * ref * (1 - TAKER_FEE)
    return {
        "filled": q, "avg": gross / q if q else ZERO, "gross": gross, "fee": fee,
        "maker_qty": mq, "taker_qty": q - mq, "market": market,
        "ours": gross + fee if c.buy else gross - fee,
        "saving": saving(c.fills),
        "saving_maker": saving([f for f in c.fills if f.maker]),
        "saving_taker": saving([f for f in c.fills if not f.maker]),
        "ref": ref,
    }


def worst_case(side: str, qty: Decimal, limit: Decimal, margin: Margin | None = None) -> Decimal:
    """Most a buy can cost (least a sell can bring): everything as taker at the limit.
    A margin open also pays the opening fee, counted at its maximum (an estimate: Kraken can change it)."""
    fee = TAKER_FEE + (OPEN_FEE if margin is not None and not margin.close else ZERO)
    return qty * limit * (1 + fee) if side == "buy" else qty * limit * (1 - fee)


def rollover_periods(seconds: float) -> int:
    """The started 4 h periods of a position open for this time."""
    return int(max(seconds, 0) // ROLL_EVERY) + 1


def margin_summary(c: Chase) -> dict | None:
    """Margin estimates for the pages. Open: collateral, opening fee and rollover per 4 h of the part that
    filled. Close: the P/L of the part that closed, from the chase fills and the entry price, less the
    trading fees of the close."""
    m = c.margin
    if m is None:
        return None
    gross = sum((f.qty * f.price for f in c.fills), ZERO)
    fee = sum((f.qty * f.price * (MAKER_FEE if f.maker else TAKER_FEE) for f in c.fills), ZERO)
    if not m.close:
        return {"collateral": gross / m.leverage, "open_fee": gross * OPEN_FEE, "rollover_4h": gross * ROLLOVER}
    sign = 1 if c.dir == "long" else -1
    pl = sum((sign * (f.price - m.entry) * f.qty for f in c.fills), ZERO) - fee
    return {"pl": pl, "close_fee": fee, "rest": max(m.pos_qty - c.filled, ZERO)}


# ---------- Serialisation (SQLite keeps the chase as JSON) ----------

def _enc(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def to_json(c: Chase) -> str:
    return json.dumps(dataclasses.asdict(c), default=_enc)


def _dec(v):
    return None if v is None else Decimal(v)


def from_json(s: str) -> Chase:
    """A chase from its JSON. Rows of earlier builds have no legs, no leg in a fill and no margin fields."""
    d = json.loads(s)
    pr = d.pop("pair")
    pair = Pair(**{**pr, "tick": Decimal(pr["tick"]), "ordermin": Decimal(pr["ordermin"]), "costmin": Decimal(pr["costmin"]),
                   "leverage_buy": tuple(pr.get("leverage_buy", ())), "leverage_sell": tuple(pr.get("leverage_sell", ())),
                   "long_limit": _dec(pr.get("long_limit")), "short_limit": _dec(pr.get("short_limit"))})
    d["legs"] = tuple(d.get("legs") or (d["id"],))
    ioc_id = d["id"] + "-i"
    fills = tuple(Fill(Decimal(f["qty"]), Decimal(f["price"]), f["maker"], f["t"], f["order"],
                       f.get("leg") or (ioc_id if f["order"] == "ioc" else d["id"])) for f in d.pop("fills"))
    m = d.pop("margin", None)
    if m is not None:
        d["margin"] = Margin(m["leverage"], m["close"], Decimal(m["pos_qty"]), Decimal(m["entry"]), Decimal(m["rollover"]))
    d.pop("order_cum", None)   # a field of the first build; the fills hold the venue's qty now
    for k in ("qty", "limit", "start_bid", "start_ask"):
        d[k] = Decimal(d[k])
    for k in ("price", "pending", "bid", "ask", "end_ask", "reject_price"):
        d[k] = _dec(d[k])
    return Chase(pair=pair, fills=fills, **d)
