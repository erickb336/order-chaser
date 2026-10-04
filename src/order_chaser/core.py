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
CANCEL_TRIES = 3
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
    status: str          # Kraken pair status; a chase needs "online"


@dataclass(frozen=True)
class Fill:
    qty: Decimal
    price: Decimal
    maker: bool
    t: float             # seconds since the chase started
    order: str           # "chase" or "ioc"


@dataclass(frozen=True)
class Chase:
    id: str              # cl_ord_id of the chase order
    pair: Pair
    side: str            # "buy" or "sell"
    qty: Decimal
    limit: Decimal       # cap (buy) or floor (sell)
    start_bid: Decimal
    start_ask: Decimal
    timeout: int
    started: float
    venue: str           # "the simulation" in a dry run, "Kraken" in live
    phase: str = "placing"
    price: Decimal | None = None      # price of the resting order
    pending: Decimal | None = None    # price of a place or amend in flight
    placed_at: float | None = None
    last_amend_at: float | None = None
    fills: tuple[Fill, ...] = ()      # the venue's filled qty is the truth: see _venue_cum()
    exit: str | None = None           # "timeout", "fillnow" or "stop", once asked for
    bid: Decimal | None = None
    ask: Decimal | None = None
    book_ok: bool = True
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

    @property
    def buy(self) -> bool:
        return self.side == "buy"

    @property
    def filled(self) -> Decimal:
        return sum((f.qty for f in self.fills), ZERO)

    @property
    def chase_filled(self) -> Decimal:
        return sum((f.qty for f in self.fills if f.order == "chase"), ZERO)

    @property
    def remainder(self) -> Decimal:
        return max(self.qty - self.filled, ZERO)


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
    now: float
    qty: Decimal
    price: Decimal
    maker: bool
    order: str = "chase"
    cum: Decimal | None = None   # the venue's filled qty of the order after this fill, when it reports it

@dataclass(frozen=True)
class Canceled:
    now: float

@dataclass(frozen=True)
class OrderState:
    now: float
    open: bool
    cum_qty: Decimal
    price: Decimal | None    # the order's limit price, also when it is closed; None for an unknown order

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
             bid: Decimal | None, ask: Decimal | None, book_ok: bool, timeout: int) -> list[str]:
    """The reasons that block a start. An empty list means the chase can start."""
    errors = []
    if pair.status != "online":
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


def begin(id: str, pair: Pair, side: str, qty: Decimal, bid: Decimal, ask: Decimal,
          timeout: int, now: float, venue: str, limit: Decimal | None = None,
          rate: float = 0.0) -> tuple[Chase, list]:
    """Start a chase that passed validate(): record the cap and place the post-only order."""
    buy = side == "buy"
    cap = limit if limit is not None else (ask if buy else bid)
    price = bid if buy else ask
    c = Chase(id=id, pair=pair, side=side, qty=qty, limit=cap, start_bid=bid, start_ask=ask,
              timeout=timeout, started=now, venue=venue, pending=price, bid=bid, ask=ask,
              rate=min(float(RATE_MAX), rate + 1), rate_at=now)
    word = "cap" if buy else "floor"
    if limit is None:
        first = f"Recorded the start {'ask' if buy else 'bid'}, {fmt_price(pair, cap)}, as the {word}."
    else:
        first = f"Recorded your limit, {fmt_price(pair, cap)}, as the {word}."
    return c, [Log(0, first),
               Place(id, side, price, qty),
               Log(0, f"Sending a post-only {side}, {fmt_qty(qty)} {pair.base} at {fmt_price(pair, price)}…", "you")]


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


def step(c: Chase, ev) -> tuple[Chase, list]:
    if c.phase == "done":
        return c, []
    c = _decay(c, ev.now)
    out: list = []
    t = ev.now - c.started
    p = lambda x: fmt_price(c.pair, x)
    base = c.pair.base
    venue = c.venue[0].upper() + c.venue[1:]

    if isinstance(ev, Book):
        if c.book_ok and not ev.ok:
            out.append(Log(t, "The order book checksum did not match. Reading the book again. No amend until the book is valid.", "warn"))
        c = replace(c, bid=ev.bid, ask=ev.ask, book_ok=ev.ok)
        if c.phase == "resting":
            c, more = _maybe_amend(c, ev.now)
            out += more

    elif isinstance(ev, Tick):
        if c.phase in ("resting", "feed_lost") and c.exit is None and t >= c.timeout:
            c = replace(c, exit="timeout")
            out.append(Log(t, "Timeout. Cancelling the resting order." if c.phase == "resting"
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
        if c.phase == "feed_lost":
            c = replace(c, phase="reconcile", reconcile_for="feed")
            out += [Log(t, "The price feed is back. Reading the order again."), Query(c.id)]

    elif isinstance(ev, Placed) and c.phase == "placing":
        c = replace(c, price=c.pending, pending=None, placed_at=ev.now)
        out.append(Log(t, f"Placed a post-only {c.side}, {fmt_qty(c.qty)} {base} at {p(c.price)}.", "you"))
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
        qty = ev.qty
        if ev.order == "chase" and ev.cum is not None:
            qty = ev.cum - c.chase_filled          # a fill that a reread already counted adds nothing
        if qty <= 0:
            return c, out
        c = replace(c, fills=c.fills + (Fill(qty, ev.price, ev.maker, t, ev.order),))
        complete = c.filled >= c.qty
        kind = "maker" if ev.maker else "taker, IOC"
        out.append(Log(t, f"Filled {fmt_qty(qty)} {base} at {p(ev.price)} ({kind})." + (" Order complete." if complete else ""), "fill"))
        if complete and c.phase != "ioc":
            c, more = _end(c, ev.now, "filled")
            out += more

    elif isinstance(ev, Canceled) and c.phase == "cancelling":
        c, more = _after_cancel(c, ev.now, t, venue)
        out += more

    elif isinstance(ev, OrderState):
        c, more = _order_state(c, ev, t)
        out += more

    elif isinstance(ev, IocDone) and c.phase == "ioc":
        got = sum((f.qty for f in c.fills if f.order == "ioc"), ZERO)
        if c.filled >= c.qty:
            c, more = _end(c, ev.now, "filled")
        else:
            side_px = "ask" if c.buy else "bid"
            now_px = c.ask if c.buy else c.bid
            what = "nothing filled" if got == 0 else f"filled {fmt_qty(got)} {base}"
            tail = f" The {side_px} is {p(now_px)}." if now_px is not None else ""
            out.append(Log(t, f"IOC {c.side}, {fmt_qty(c.qty - c.filled + got)} {base} at {p(c.limit)}: {what}.{tail}", "bad"))
            c, more = _end(c, ev.now, "notfilled")
        out += more

    elif isinstance(ev, UserStop) and c.phase in ACTIVE and c.exit != "stop":
        c = replace(c, exit="stop")
        out.append(Log(t, "You pressed Stop. Cancelling the order."))
        c, more = _settle(c, ev.now)
        out += more

    elif isinstance(ev, UserFillNow) and c.phase in ("placing", "resting", "amending", "reconcile") and c.exit is None:
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
    return c, out + [Cancel(c.id)]


def _maybe_amend(c: Chase, now: float) -> tuple[Chase, list]:
    out: list = []
    slow = c.rate > RATE_SLOW_ABOVE
    if slow != c.slow:
        c = replace(c, slow=slow)
        if slow:
            out.append(Log(now - c.started, f"Estimated rate counter at {int(c.rate)} of {RATE_MAX}. Next amend in {AMEND_EVERY_SLOW} s, not {AMEND_EVERY} s.", "warn"))
    if not (c.book_ok and c.feed_ok) or c.price is None or c.exit:
        return c, out
    best = c.bid if c.buy else c.ask
    if best is None:
        return c, out
    target = min(best, c.limit) if c.buy else max(best, c.limit)
    if not (target > c.price if c.buy else target < c.price):
        return c, out
    other = c.ask if c.buy else c.bid
    if other is not None and (target >= other if c.buy else target <= other):
        return c, out          # a post-only order there would cross: wait for the book
    every = AMEND_EVERY_SLOW if c.slow else AMEND_EVERY
    if now - max(c.placed_at or 0, c.last_amend_at or 0) < every:
        return c, out
    age = now - (c.placed_at or now)
    c = _add_rate(replace(c, phase="amending", pending=target, last_amend_at=now), amend_cost(age))
    return c, out + [Amend(c.id, target)]


def _rejected(c: Chase, ev: Rejected, t: float) -> tuple[Chase, list]:
    p = lambda x: fmt_price(c.pair, x)
    why = {"would_cross": f"would cross the {'ask' if c.buy else 'bid'} (post-only)",
           "rate_limit": "EOrder:Rate limit exceeded",
           "not_open": "the order is not open"}.get(ev.reason, ev.reason)
    if ev.op == "place" and c.phase == "placing":
        c = replace(c, pending=None)
        out = [Log(t, f"{c.venue[0].upper() + c.venue[1:]} rejected the order: {why}.", "bad")]
        c, more = _end(c, ev.now, "refused")
        return c, out + more
    if ev.op == "amend" and c.phase == "amending":
        out = [Log(t, f"Amend to {p(c.pending)} rejected: {why}.", "warn"), Query(c.id)]
        c = replace(c, phase="reconcile", reconcile_for="reject", pending=None, reject=ev.reason, reject_at=ev.now,
                    reject_price=c.pending)
        if ev.reason == "rate_limit":
            # The estimate was too low: trust Kraken and start again from the maximum.
            c = replace(c, rate=float(RATE_MAX), slow=True)
        return c, out
    if ev.op == "cancel" and c.phase == "cancelling":
        return c, [Log(t, f"Cancel rejected: {why}. Reading the order again.", "warn"), Query(c.id)]
    if ev.op == "ioc" and c.phase == "ioc":
        out = [Log(t, f"The IOC was rejected: {why}.", "bad")]
        c, more = _end(c, ev.now, "notfilled")
        return c, out + more
    return c, []


def _after_cancel(c: Chase, now: float, t: float, venue: str) -> tuple[Chase, list]:
    if c.exit == "stop":
        out = [Log(t, f"{venue} confirmed the cancel. Filled: {fmt_qty(c.filled)} {c.pair.base}.")]
        c, more = _end(c, now, "filled" if c.filled >= c.qty else "stopped")
        return c, out + more
    c = replace(c, phase="reread", price=None)
    return c, [Log(t, f"{venue} confirmed the cancel."), Query(c.id)]


def _order_state(c: Chase, ev: OrderState, t: float) -> tuple[Chase, list]:
    p = lambda x: fmt_price(c.pair, x)
    base = c.pair.base
    venue = c.venue[0].upper() + c.venue[1:]
    c, out = _venue_cum(c, ev, t)
    if c.phase == "cancelling" and not ev.open:
        c, more = _after_cancel(c, ev.now, t, venue)
        return c, out + more
    if c.phase == "cancelling":            # the cancel was rejected and the order is still open
        if c.cancel_tries >= CANCEL_TRIES:
            out.append(Log(t, f"{venue} rejected the cancel {CANCEL_TRIES} times and the order is still open. The tool "
                              "stopped the chase. Check Kraken Pro for an open order and cancel it there.", "bad"))
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
        if not c.feed_ok:
            out.append(Log(t, "No IOC: the price feed is lost, so there is no valid price. The rest counts as not filled.", "bad"))
            c, more = _end(c, ev.now, "notfilled")
            return c, out + more
        if rest < c.pair.ordermin or rest * c.limit < c.pair.costmin:
            out.append(Log(t, f"The rest, {fmt_qty(rest)} {base}, is below the Kraken minimum for {c.pair.symbol} "
                              f"({fmt_qty(c.pair.ordermin)} {base} or {c.pair.costmin} {c.pair.quote}). It counts as not filled.", "bad"))
            c, more = _end(c, ev.now, "belowmin")
            return c, out + more
        c = _add_rate(replace(c, phase="ioc"), 1)
        out += [Ioc(c.id + "-ioc", c.side, c.limit, rest),
                Log(t, f"Sending an IOC {c.side}, {fmt_qty(rest)} {base} at {p(c.limit)}…", "warn")]
        return c, out
    return c, out


def _venue_cum(c: Chase, ev: OrderState, t: float) -> tuple[Chase, list]:
    """The venue's filled qty is the truth. Record a fill that the tool did not see, at the order's price."""
    missing = ev.cum_qty - c.chase_filled
    if missing <= 0:
        return c, []
    price = ev.price if ev.price is not None else c.limit   # no price from the venue: count the worst price
    venue = c.venue[0].upper() + c.venue[1:]
    c = replace(c, fills=c.fills + (Fill(missing, price, True, t, "chase"),))
    return c, [Log(t, f"{venue} reports {fmt_qty(missing)} {c.pair.base} more filled than the tool saw. "
                      f"Recorded it at {fmt_price(c.pair, price)} (maker).", "fill")]


def _restarted(c: Chase, ev: Restarted, t: float) -> tuple[Chase, list]:
    base = c.pair.base
    off = int(ev.now - ev.last_seen)
    if c.venue == "the simulation":
        lines = [Log(t, f"Tool started again after {off} s off. Ended the simulated order. No order was on Kraken.", "bad"),
                 Log(t, f"Recorded the simulated fills: {fmt_qty(c.filled)} of {fmt_qty(c.qty)} {base}."),
                 Log(t, "Did not continue the dry run.")]
    else:  # T4 replaces this with a reconcile against Kraken before it ends the chase.
        lines = [Log(t, f"Tool started again after {off} s off. Chase ended, not continued.", "bad")]
    c, more = _end(replace(c, off_from=ev.last_seen), ev.now, "ended")
    return c, lines + more


def _end(c: Chase, now: float, outcome: str) -> tuple[Chase, list]:
    end_px = (c.ask if c.buy else c.bid) if c.feed_ok else None   # no valid price while the feed is lost
    return replace(c, phase="done", outcome=outcome, ended_at=now, pending=None, end_ask=end_px), []


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


def worst_case(side: str, qty: Decimal, limit: Decimal) -> Decimal:
    """Most a buy can cost (least a sell can bring): everything as taker at the limit."""
    return qty * limit * (1 + TAKER_FEE) if side == "buy" else qty * limit * (1 - TAKER_FEE)


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
    d = json.loads(s)
    pr = d.pop("pair")
    pair = Pair(**{**pr, "tick": Decimal(pr["tick"]), "ordermin": Decimal(pr["ordermin"]), "costmin": Decimal(pr["costmin"])})
    fills = tuple(Fill(Decimal(f["qty"]), Decimal(f["price"]), f["maker"], f["t"], f["order"]) for f in d.pop("fills"))
    d.pop("order_cum", None)   # a field of the first build; the fills hold the venue's qty now
    for k in ("qty", "limit", "start_bid", "start_ask"):
        d[k] = Decimal(d[k])
    for k in ("price", "pending", "bid", "ask", "end_ask", "reject_price"):
        d[k] = _dec(d[k])
    return Chase(pair=pair, fills=fills, **d)
