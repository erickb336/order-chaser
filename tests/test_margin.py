"""Margin in the dry run: the core and the simulated exchange and account, with no network."""
import json
import random
import tempfile
import time
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from order_chaser import core, server
from order_chaser.db import Db
from order_chaser.core import Book, Margin, Tick, UserStop
from order_chaser.sim import SimAccount, SimGateway

BTC = core.Pair("BTC/USD", "BTC", "USD", D("0.1"), D("0.00005"), D("0.5"), 1, 8, "online",
                leverage_buy=tuple(range(2, 11)), leverage_sell=tuple(range(2, 11)), margin_call=80, margin_stop=40,
                long_limit=D("350"), short_limit=D("250"))
T0 = 1000.0
BID, ASK = D("62417.9"), D("62418.5")


class Run:
    """A chase on the simulated exchange: commands go to the gateway, its answers go back to the core."""

    def __init__(self, gw=None, bid=BID, ask=ASK):
        self.gw = gw or SimGateway()
        self.gw.pair = BTC
        self.sent: list = []
        self.logs: list[str] = []
        self.c = None
        self.book(bid, ask, T0)

    def start(self, side, qty, margin, timeout=120, now=T0, rate=0.0, limit=None):
        self.c, cmds = core.begin("oc0123456789ab", BTC, side, D(qty), self.gw.bids[0][0], self.gw.asks[0][0],
                                  timeout, now, core.SIM_VENUE, limit, rate, margin)
        self._do(cmds, now)
        return self

    def _do(self, cmds, now):
        queue = list(cmds)
        while queue:
            x = queue.pop(0)
            if isinstance(x, core.Log):
                self.logs.append(x.text)
                continue
            self.sent.append(x)
            for ev in self.gw.send(x, now):
                queue += self._step(ev)

    def _step(self, ev):
        if self.c is None:
            return []
        self.c, cmds = core.step(self.c, ev)
        return cmds

    def ev(self, ev):
        self._do(self._step(ev), ev.now)
        return self

    def book(self, bid, ask, now, size="1"):
        evs = self.gw.on_book([(D(bid), D(size))], [(D(ask), D(size))], now)
        for e in evs:
            self.ev(e)
        if self.c is not None:
            self.ev(Book(now, D(bid), D(ask), True))
        return self

    def trade(self, side, price, qty, now):
        for e in self.gw.on_trade(side, D(price), D(qty), now):
            self.ev(e)
        return self


def opened(qty="0.05", lev=3):
    """A simulated account with a BTC/USD long of qty at 62,417.90 (the bid), opened by a chase."""
    r = Run().start("buy", qty, Margin(lev)).trade("sell", "62417.0", qty, T0 + 1)
    assert r.c.outcome == "filled"
    return r.gw.account


def closing(acc, pair="BTC/USD", d="long"):
    """The Margin of a close of the account's positions, as the server builds it at the start."""
    parts = acc.parts(pair, d)
    return Margin(core.average(parts)[2], True, parts)


def sim_with(account):
    gw = SimGateway(account)
    return gw


# ---------- ids and legs ----------

def test_every_cl_ord_id_has_at_most_18_characters():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3), timeout=900, limit=D("63000"))
    bid = BID
    for s in range(1, 900, 3):                   # the bid rises every 3 s for 15 min: many legs
        bid += D("0.1")
        r.book(bid, bid + D("0.6"), T0 + s)
        r.ev(Tick(T0 + s))
    r.ev(Tick(T0 + 901))
    ids = {x.id for x in r.sent}
    assert len(r.c.legs) > 20 and max(len(i) for i in ids) <= 18
    assert "oc0123456789ab-i" in ids             # the IOC


def test_cancel_and_replace_cancels_waits_reads_the_cum_then_places_the_rest_at_the_bid():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3))
    r.trade("sell", "62417.0", "0.018", T0 + 2)        # leg 0 fills 0.018
    n = len(r.sent)
    r.book("62418.3", "62418.9", T0 + 6)                # the bid rises: the amend is refused
    assert [type(x).__name__ for x in r.sent[n:]] == ["Amend", "Cancel", "Query", "MarginPlace"]
    place = r.sent[-1]
    assert (place.id, place.price, place.qty, place.leverage, place.reduce_only) == (
        "oc0123456789ab-1", D("62418.3"), D("0.032"), 3, False)
    assert r.c.filled == D("0.018") and r.c.replace


def test_a_late_fill_report_of_an_old_leg_never_counts_twice_and_the_legs_sum():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3))
    r.trade("sell", "62417.0", "0.018", T0 + 2)
    r.book("62418.3", "62418.9", T0 + 6)                # replace: leg 1 rests for 0.032
    r.trade("sell", "62418.0", "0.010", T0 + 8)         # leg 1 fills 0.010
    # The report of leg 0 arrives again, late (a WebSocket replay): it adds nothing.
    r.ev(core.Filled(T0 + 9, D("0.018"), D("62417.9"), True, cum=D("0.018"), id="oc0123456789ab"))
    assert r.c.filled == D("0.028")
    assert (r.c.leg_cum("oc0123456789ab"), r.c.leg_cum("oc0123456789ab-1")) == (D("0.018"), D("0.010"))
    assert r.gw.account.position("BTC/USD", "long")["qty"] == D("0.028")


def test_cancel_and_replace_keeps_5_s_between_moves_and_room_on_the_rate_counter():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3), timeout=300, rate=30, limit=D("63000"))
    bid, cancels = BID, []
    for s in range(1, 300):
        bid += D("0.1")                             # the bid rises each second
        n = len(r.sent)
        r.book(bid, bid + D("0.6"), T0 + s)
        cancels += [s for x in r.sent[n:] if isinstance(x, core.Cancel)]
        assert r.c.rate <= core.RATE_MAX
    gaps = [b - a for a, b in zip(cancels, cancels[1:])]
    assert len(cancels) > 10 and min(gaps) >= 5


def test_cancel_and_replace_waits_15_s_above_40_on_the_rate_counter():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3), rate=55, limit=D("63000"))
    r.book("62418.0", "62418.6", T0 + 5)            # the amend is refused at 5 s; the counter is near 60
    first = None
    for s in range(6, 40):
        n = len(r.sent)
        r.book(D("62418.0") + D("0.1") * s, D("62418.6") + D("0.1") * s, T0 + s)
        if first is None and any(isinstance(x, core.Cancel) for x in r.sent[n:]):
            first = s
    assert first == 15


def replacing(rate=0.0):
    """A margin chase whose amend was refused: the first leg is cancelled and its rest waits for a new leg."""
    c, _ = core.begin("oc0123456789ab", BTC, "buy", D("1"), BID, ASK, 900, T0, core.SIM_VENUE, rate=rate, margin=Margin(3))
    c, _ = core.step(c, core.Placed(T0))
    c, _ = core.step(c, Book(T0 + 6, BID + D("0.1"), ASK + D("0.1"), True))
    c, _ = core.step(c, core.Rejected(T0 + 6, "amend", "EOrder:Invalid arguments"))
    c, _ = core.step(c, core.Canceled(T0 + 6))
    c, out = core.step(c, core.OrderState(T0 + 6, False, D(0), BID, c.leg))
    return c, out


def test_after_a_would_cross_refusal_new_legs_go_out_at_most_every_5_s():
    # NEW-LEG-NO-SPACING: each new leg is refused (the book moved in flight); a book comes every 0.2 s for 60 s.
    c, out = replacing()
    places, now = [T0 + 6 for x in out if isinstance(x, core.MarginPlace)], T0 + 6
    for _ in range(300):
        if c.phase == "placing":
            c, _ = core.step(c, core.Rejected(now, "place", "would_cross"))
        now += 0.2
        c, out = core.step(c, Book(now, BID + D("0.1"), ASK + D("0.1"), True))
        places += [now for x in out if isinstance(x, core.MarginPlace)]
    gaps = [round(b - a, 1) for a, b in zip(places, places[1:])]
    assert (len(places), min(gaps)) == (13, 5.0)


def test_a_new_leg_waits_while_the_rate_counter_has_no_room_for_its_cancel():
    c, out = replacing()
    assert [x.id for x in out if isinstance(x, core.MarginPlace)] == ["oc0123456789ab-1"]
    c, _ = core.step(c, core.Rejected(T0 + 6, "place", "would_cross"))
    # The counter is near 60 and the last move is long ago: only the room on the counter holds the leg back.
    c = replace(c, rate=55.0, rate_at=T0 + 6, last_amend_at=T0 - 100)
    placed = []
    for s in range(7, 14):
        c, out = core.step(c, Book(T0 + s, BID + D("0.1"), ASK + D("0.1"), True))
        placed += [s for x in out if isinstance(x, core.MarginPlace)]
    assert placed == [10]             # 1 for the leg + 8 for its cancel fit under 60 at 51: T0 + 10


def in_flight(*user):
    """A new leg is in flight (phase placing); the user events come; then the venue refuses the leg with would_cross."""
    c, _ = replacing()
    assert (c.phase, c.leg) == ("placing", "oc0123456789ab-1")
    for ev in user:
        c, _ = core.step(c, ev)
    return core.step(c, core.Rejected(T0 + 6.4, "place", "would_cross"))


def test_fill_now_while_a_new_leg_is_in_flight_reads_the_fills_then_sends_one_ioc_at_the_cap():
    # NEW-LEG-CROSS-DROPS-FILL-NOW
    c, out = in_flight(core.UserFillNow(T0 + 6.2))
    assert (c.phase, out[-1]) == ("reread", core.Query("oc0123456789ab-1"))
    c, out = core.step(c, core.OrderState(T0 + 6.5, False, D(0), None, "oc0123456789ab-1"))
    assert (c.phase, [x for x in out if not isinstance(x, core.Log)]) == (
        "ioc", [core.MarginIoc("oc0123456789ab-i", "buy", ASK, D("1"), 3, False)])


def test_stop_while_a_new_leg_is_in_flight_ends_as_stopped():
    c, out = in_flight(UserStop(T0 + 6.2))
    assert (c.phase, c.outcome, [x for x in out if not isinstance(x, core.Log)]) == ("done", "stopped", [])


def test_a_timeout_while_a_new_leg_is_in_flight_reads_the_fills_then_sends_the_ioc():
    c, _ = replacing()
    c = replace(c, timeout=10)
    c, _ = core.step(c, Tick(T0 + 10.2))                 # the timeout comes while the leg is in flight
    c, out = core.step(c, core.Rejected(T0 + 10.3, "place", "would_cross"))
    c, out = core.step(c, Tick(T0 + 10.4))
    assert (c.exit, out[-1]) == ("timeout", core.Query("oc0123456789ab-1"))
    c, out = core.step(c, core.OrderState(T0 + 10.5, False, D(0), None, "oc0123456789ab-1"))
    assert [type(x).__name__ for x in out if not isinstance(x, core.Log)] == ["MarginIoc"]


def test_a_new_leg_refused_for_the_rate_limit_waits_then_goes_out_again():
    # NEW-LEG-REFUSAL-ENDS-EARLY: the counter goes to 60, so the next leg waits 15 s and for room for its cancel.
    c, _ = replacing()
    c, out = core.step(c, core.Rejected(T0 + 6, "place", "rate_limit"))
    assert (c.phase, c.rate, out[0].text) == ("resting", 60.0, "The simulated exchange rejected the new order: "
                                              "EOrder:Rate limit exceeded. The tool places it again at the best bid after the wait.")
    placed = []
    for s in range(7, 30):
        c, out = core.step(c, Book(T0 + s, BID + D("0.1"), ASK + D("0.1"), True))
        placed += [(s, x.id) for x in out if isinstance(x, core.MarginPlace)]
    assert placed == [(21, "oc0123456789ab-2")]


def test_a_new_leg_refused_with_another_reason_ends_the_chase_with_the_venues_text():
    c, _ = replacing()
    c, out = core.step(c, core.Rejected(T0 + 6, "place", "EOrder:Insufficient margin"))
    assert (c.outcome, out[0].text) == ("notfilled", "The simulated exchange rejected the order: EOrder:Insufficient margin.")


def test_no_order_price_after_the_order_is_gone():
    # UX-PRICE-AFTER-ORDER-GONE (its cause): a maker fill ends the order, so the chase has no order price.
    r = Run().start("buy", "0.05", Margin(3)).trade("sell", "62417.0", "0.05", T0 + 1)
    assert (r.c.outcome, r.c.price, r.c.pending) == ("filled", None, None)
    r = Run().start("buy", "0.05", None).ev(UserStop(T0 + 1)).ev(core.Canceled(T0 + 1))
    assert (r.c.outcome, r.c.price) == ("stopped", None)


# ---------- the account and the position ----------

def test_liquidation_ends_the_chase_and_cancels_its_order():
    acc = opened("0.05", 5)                         # 3,120.9 USD long at 5x: 624 USD collateral
    acc.cash = D("700")                             # a small account: the level is near 110%
    r = Run(SimGateway(acc)).start("sell", "0.05", closing(acc))
    r.book("52000.0", "52000.6", T0 + 10)           # the price falls 10,400: the level goes below 40%
    assert r.c.outcome == "liquidated"
    assert isinstance(r.sent[-1], core.Cancel) and not r.gw.orders["oc0123456789ab"]["open"]
    assert acc.positions == []
    assert "liquidated the position: the account margin level fell to 40%" in " ".join(r.logs)


def test_a_reduce_only_close_never_grows_or_flips_the_position():
    acc = opened("0.03")
    # A close of 0.05 against a 0.03 long (the form refuses it; the venue must hold too).
    r = Run(SimGateway(acc)).start("sell", "0.05", closing(acc))
    r.trade("buy", "62419.0", "1", T0 + 3)
    assert acc.positions == []                      # closed, and no short opened
    assert r.c.filled == D("0.03") and r.c.outcome == "nopos"
    assert "The long position on BTC/USD is closed. The reduce-only order has nothing left to close." in r.logs


def test_a_reduce_only_order_with_no_position_is_refused():
    gone = core.Part("oc1", T0, 3, D("62417.9"), D("0.01"), D("208.06"))     # read at the start, closed since
    r = Run().start("sell", "0.01", Margin(3, True, (gone,)))
    assert r.c.outcome == "refused"
    assert "The simulated exchange rejected the order: EOrder:Reduce only:No position exists." in r.logs


def test_an_open_against_the_other_direction_is_blocked_before_the_start_and_by_the_venue():
    acc = opened("0.03")
    errors = core.validate_margin(BTC, "short", D("0.01"), 3, BID, acc.list(), D("4000"))
    assert errors == ["Close the long first. You have an open long position on BTC/USD."]
    r = Run(SimGateway(acc)).start("sell", "0.01", Margin(3))
    assert "The simulated exchange rejected the order: EOrder:Cannot open opposing position." in r.logs


def test_leverage_is_the_pairs_list_within_2x_to_5x():
    sol = core.Pair(**{**BTC.__dict__, "symbol": "SOL/USD", "leverage_buy": (2, 3), "leverage_sell": ()})
    assert (BTC.leverage("buy"), sol.leverage("buy"), sol.leverage("sell")) == ((2, 3, 4, 5), (2, 3), ())
    assert core.validate_margin(sol, "long", D("1"), 4, D("140"), [], D("5000")) == ["Pick a leverage that SOL/USD allows: 2x, 3x."]
    assert core.validate_margin(sol, "short", D("1"), 2, D("140"), [], D("5000")) == ["Kraken allows no short on SOL/USD."]
    assert core.validate_margin(BTC, "long", D("0.05"), 6, BID, [], D("5000")) == ["Pick a leverage that BTC/USD allows: 2x, 3x, 4x, 5x."]
    r = Run()
    r.gw.pair = sol
    r.c, cmds = core.begin("oc1", sol, "buy", D("1"), D("140"), D("140.1"), 120, T0, core.SIM_VENUE, margin=Margin(4))
    assert r.gw.send(cmds[1], T0) == [core.Rejected(T0, "place", "EGeneral:Invalid arguments:leverage")]


def test_the_venue_refusals_show_krakens_text():
    def refused(account, qty, lev=5, pair=BTC):
        r = Run(SimGateway(account))
        r.gw.pair = pair
        r.c, cmds = core.begin("oc1", pair, "buy", D(qty), BID, ASK, 120, T0, core.SIM_VENUE, margin=Margin(lev))
        r._do(cmds, T0)
        return r.c.outcome, r.logs[-1]
    assert refused(SimAccount(), "0.5") == ("refused", "The simulated exchange rejected the order: EOrder:Insufficient margin.")
    assert refused(SimAccount(allowance=D("1000")), "0.05") == ("refused", "The simulated exchange rejected the order: EOrder:Margin allowance exceeded.")
    small = core.Pair(**{**BTC.__dict__, "long_limit": D("0.01")})
    assert refused(SimAccount(), "0.05", pair=small) == ("refused", "The simulated exchange rejected the order: EOrder:Margin position size exceeded.")


def test_free_margin_for_orders_decides_the_start():
    acc = opened("0.05", 2)                         # 1,560 USD collateral of 5,000
    free = acc.read(T0 + 2)["free_orders"]
    assert D("3420") < free < D("3430")             # 5,000 - 1,560 collateral - fees - one rollover
    assert core.validate_margin(BTC, "long", D("0.25"), 5, ASK, acc.list(), free) == []      # 15,604.63 / 5 = 3,120.93
    assert core.validate_margin(BTC, "long", D("0.25"), 2, ASK, acc.list(), free)[0].startswith(
        "This open needs 7,802.31 USD of collateral. Your free margin for new orders is 3,42")


def test_the_account_counts_fees_rollover_and_the_margin_level():
    acc = opened("0.05", 5)                         # maker at 62,417.90: cost 3,120.895
    cost = D("0.05") * D("62417.9")
    assert acc.cash == D(5000) - cost * (core.MAKER_FEE + core.OPEN_FEE)
    acc.mark("BTC/USD", D("62417.8"), D("62418.0"), T0 + 2)          # mark = entry: no P/L
    eq = acc.equity(T0 + 2)
    assert eq == acc.cash                                           # no rollover before 4 h
    assert acc.level(T0 + 2) == eq / (cost / 5) * 100
    assert acc.equity(T0 + 1 + 4 * 3600 - 1) == acc.cash             # 1 s before 4 h after the open: none yet
    assert acc.equity(T0 + 1 + 4 * 3600) == acc.cash - cost * core.ROLLOVER    # each full 4 h after the open
    assert acc.equity(T0 + 1 + 8 * 3600) == acc.cash - cost * core.ROLLOVER * 2


def test_a_close_reports_its_estimated_profit_less_the_close_fee():
    acc = opened("0.03")
    r = Run(SimGateway(acc), bid="62500.0", ask="62500.6").start("sell", "0.03", closing(acc))
    r.trade("buy", "62501.0", "0.03", T0 + 3)       # sold at 62,500.6 as maker
    m = core.margin_summary(r.c)
    assert m["pl"] == (D("62500.6") - D("62417.9")) * D("0.03") - D("0.03") * D("62500.6") * core.MAKER_FEE
    assert r.logs[-1] == "Filled 0.0300 BTC at 62,500.60 (maker). Position closed."


def test_stop_during_a_replace_ends_the_chase_and_keeps_what_filled():
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3))
    r.trade("sell", "62417.0", "0.018", T0 + 2)
    gw.send = (lambda send: lambda cmd, now: [] if isinstance(cmd, core.Cancel) else send(cmd, now))(gw.send)  # the cancel is in flight
    r.book("62418.3", "62418.9", T0 + 6)
    assert r.c.phase == "cancelling"
    r.ev(UserStop(T0 + 7)).ev(core.Canceled(T0 + 7))
    assert (r.c.outcome, r.c.filled) == ("stopped", D("0.018"))


# ---------- mixed leverage: each open is its own position (as Kraken) ----------

ETH = core.Pair(**{**BTC.__dict__, "symbol": "ETH/USD", "base": "ETH"})


def test_each_open_is_its_own_position_and_the_close_list_shows_the_count_and_the_average_leverage():
    acc = SimAccount()
    acc.fill(BTC, "buy", 3, False, D("0.01"), D("60000"), True, 0.0, "oc1")
    acc.fill(BTC, "buy", 3, False, D("0.01"), D("60000"), True, 5.0, "oc1")      # the same chase: the same position
    acc.fill(BTC, "buy", 5, False, D("0.02"), D("60000"), True, 60.0, "oc2")     # another open, another leverage
    assert [(p["qty"], p["leverage"], p["opened"]) for p in acc.positions] == [(D("0.02"), 3, 0.0), (D("0.02"), 5, 60.0)]
    row = acc.read(70.0)["positions"]
    assert [(r["pair"], r["dir"], r["qty"], r["count"], r["leverage"]) for r in row] == [("BTC/USD", "long", D("0.04"), 2, 4)]
    # 2,400 USD of cost on 400 + 240 USD of collateral: 3.75, "average 4x"


def test_a_close_takes_the_oldest_position_first_partly_if_needed():
    acc = SimAccount()
    acc.fill(BTC, "buy", 2, False, D("0.01"), D("60000"), True, 0.0, "oc1")
    acc.fill(BTC, "buy", 5, False, D("0.02"), D("61000"), True, 10.0, "oc2")
    cash = acc.cash
    assert acc.fill(BTC, "sell", 2, True, D("0.015"), D("62000"), True, 4 * 3600.0) == D("0.015")
    # The 2x position (the oldest) closes whole; the 5x position closes 0.005 of its 0.02.
    assert [(p["qty"], p["entry"], p["leverage"], p["margin"]) for p in acc.positions] == [
        (D("0.015"), D("61000"), 5, D("0.015") * 61000 / 5)]
    pl = D("0.01") * 2000 + D("0.005") * 1000
    fee = D("0.015") * 62000 * core.MAKER_FEE
    roll = D("0.01") * 60000 * core.ROLLOVER        # 4 h after the first open; the second: 10 s short of 4 h
    assert acc.cash - cash == pl - fee - roll


# ---------- the close plan (repair round 2): every number of a close follows the FIFO plan ----------

H = 3600.0
hm = lambda t: time.strftime("%H:%M", time.localtime(t))


def account_with(*opens, d="long"):
    """A simulated account with these positions, oldest first: (qty, entry, leverage, opened)."""
    acc = SimAccount()
    for k, (qty, entry, lev, at) in enumerate(opens):
        acc.fill(BTC, "buy" if d == "long" else "sell", lev, False, D(qty), D(entry), True, at, f"oc{k}")
    return acc


def cents(a, b):
    return abs(D(a) - D(b)) < D("0.005")


def test_a_part_fill_of_a_close_prices_its_profit_against_the_oldest_position():
    # CLOSE-PL-PARTIAL-FIFO: a close of 0.2 over entries 60,000 then 50,000 fills 0.1 at 55,000.
    acc = account_with(("0.1", "60000", 2, T0 - 60), ("0.1", "50000", 2, T0 - 30))
    cash = acc.cash
    r = Run(SimGateway(acc), bid="54999.4", ask="55000.0").start("sell", "0.2", closing(acc))
    r.trade("buy", "55001", "0.1", T0 + 2).ev(UserStop(T0 + 3))
    e = core.margin_summary(r.c)
    assert e["pl"] == D("-522.000")                  # (55,000 - 60,000) x 0.1 - 22.00 maker fee
    assert acc.cash - cash == e["pl"] - e["rollover"] == D("-522.000")
    assert (e["takes"], e["stays"]) == (f"the 2x position opened {hm(T0 - 60)} (0.1000 BTC)", "0.1000 BTC at 2x")


def test_the_rest_of_a_close_keeps_the_leverage_of_each_position_that_stays():
    # UX-CLOSE-REMAINDER-WRONG-LEVERAGE: close 0.015 of [0.01 at 2x, 0.02 at 4x]: one 4x position stays.
    acc = account_with(("0.01", "60000", 2, T0 - 60), ("0.02", "61000", 4, T0 - 30))
    r = Run(SimGateway(acc)).start("sell", "0.015", closing(acc))
    r.trade("buy", "62419.0", "0.015", T0 + 2)
    e = core.margin_summary(r.c)
    assert r.c.outcome == "filled" and [(p.qty, p.leverage) for p in acc.parts("BTC/USD", "long")] == [(D("0.015"), 4)]
    assert e["stays"] == "0.0150 BTC at 4x" and e["rest"] == D("0.015")
    assert r.logs[-1] == "Filled 0.0150 BTC at 62,418.50 (maker). Order complete. Stays open: 0.0150 BTC at 4x."


def test_the_level_after_a_close_releases_the_collateral_of_each_part_it_takes():
    # UX-CLOSE-AFTER-LEVEL-NOT-FIFO: the oldest position has the higher leverage, so FIFO releases less collateral
    # than a pro-rata close at the average leverage: the level after is lower (the risky direction).
    acc = account_with(("0.01", "60000", 5, T0 - 60), ("0.02", "60000", 2, T0 - 30))
    mark, now = D("60000"), T0
    acc.marks["BTC/USD"] = (mark, now)
    after = core.close_preview(acc.parts("BTC/USD", "long"), D("0.015"), acc.equity(now), acc.used(), mark)
    pro_rata = acc.equity(now) / (acc.used() * (1 - D("0.015") / D("0.03"))) * 100
    acc.fill(BTC, "sell", 2, True, D("0.015"), mark, True, now)     # the close fills at the mark as maker
    # FIFO releases 120 + 150 of 720 USD of collateral; pro rata would release 360.
    assert cents(after, acc.level(now)) and round(after, 2) == D("1108.51")       # 4,988.30 / 450
    assert round(pro_rata, 2) == D("1386.64")                                     # 4,991.90 / 360: too high


def test_the_close_names_the_positions_it_takes_and_what_stays_with_the_true_average_entry():
    # UX-CLOSE-WHICH-POSITION-UNSAID: the start line and the log say which positions the close takes.
    acc = account_with(("0.01", "60000", 2, T0 - 60), ("0.02", "61000", 4, T0 - 30))
    r = Run(SimGateway(acc)).start("sell", "0.015", closing(acc))
    assert r.logs[0] == (f"Read the positions: 0.0300 BTC in 2 positions · average 3x · average entry 60,666.67. The close takes "
                         f"the oldest first: the 2x position opened {hm(T0 - 60)} (0.0100 BTC) and 0.0050 BTC of the 4x position "
                         f"opened {hm(T0 - 30)}. Stays open: 0.0150 BTC at 4x.")
    assert core.margin_summary(r.c)["plan"] == f"the 2x position opened {hm(T0 - 60)} (0.0100 BTC) and 0.0050 BTC of the 4x position opened {hm(T0 - 30)}"


def agreement(before: SimAccount, acc: SimAccount, c, d, price, t_end) -> None:
    """The page numbers of a close (core.margin_summary, core.close_preview, core.plan_words) and the account agree
    to the cent: the P/L less the rollover is the cash change; what stays is the account's positions; the level
    that the form predicts (at a mark of the fill price) is the account's level."""
    e = core.margin_summary(c)
    assert cents(acc.cash - before.cash, e["pl"] - e["rollover"]), (acc.cash - before.cash, e["pl"], e["rollover"])
    _, stays = core.close_plan(before.parts("BTC/USD", d), c.filled)
    now = acc.parts("BTC/USD", d)
    assert [(p.id, p.leverage, p.entry) for p in stays] == [(p.id, p.leverage, p.entry) for p in now]
    assert all(cents(a.qty * 10**8, b.qty * 10**8) and cents(a.margin, b.margin) for a, b in zip(stays, now))
    assert e["stays"] == (core.plan_words(now, D(0), BTC, c.started)["stays"] if now else "")
    if c.filled:
        before.marks["BTC/USD"] = acc.marks["BTC/USD"] = (price, t_end)
        after = core.close_preview(before.parts("BTC/USD", d), c.filled, before.equity(t_end), before.used(), price)
        assert (after is None and acc.level(t_end) is None) or cents(after, acc.level(t_end)), (after, acc.level(t_end))


CASES = {   # positions (qty, entry, leverage, opened), the close, and how it fills
    "part fill over 2 positions": ([("0.01", "60000", 2, T0 - 5 * H), ("0.02", "61000", 4, T0 - 60)], "long", "0.02", "part"),
    "full fill": ([("0.01", "60000", 2, T0 - 9 * H), ("0.02", "61000", 4, T0 - 60)], "long", "0.03", "full"),
    "fill after a replace": ([("0.01", "60000", 5, T0 - 5 * H), ("0.02", "61000", 2, T0 - 60), ("0.03", "59000", 3, T0 - 30)],
                             "long", "0.05", "replace"),
    "short, part fill": ([("0.02", "63000", 3, T0 - 13 * H), ("0.01", "64000", 5, T0 - 60)], "short", "0.025", "part"),
}


def run_case(opens, d, qty, how):
    acc = account_with(*opens, d=d)
    before = SimAccount.from_json(acc.to_json())
    gw = SimGateway(acc)
    gw.refuse_margin_amends = how == "replace"
    r = Run(gw).start("sell" if d == "long" else "buy", qty, closing(acc, d=d))
    if how == "replace":                             # the ask falls: the amend is refused, the new leg fills
        r.book("62300.0", "62300.6", T0 + 6)
        assert len(r.c.legs) == 2 and r.c.price == D("62417.9")     # the new leg rests at the floor
    price = r.c.price
    fill = D(qty) if how != "part" else D(qty) * 3 / 4
    r.trade("buy" if d == "long" else "sell", price + (1 if d == "long" else -1), fill, T0 + 8)
    if how == "part":
        r.ev(UserStop(T0 + 9))
    return before, acc, r, price


def test_close_plan_numbers_and_the_account_agree_to_the_cent():
    for name, (opens, d, qty, how) in CASES.items():
        before, acc, r, price = run_case(opens, d, qty, how)
        assert r.c.filled == (D(qty) if how != "part" else D(qty) * 3 / 4), name
        agreement(before, acc, r.c, d, price, T0 + 8)


def test_rollover_runs_from_the_open_of_each_position():
    # ROLLOVER-FROM-FIRST-OPEN: a second open 20 h after the first pays rollover from its own open.
    acc = SimAccount()
    acc.fill(BTC, "buy", 2, False, D("0.01"), D("60000"), True, 0.0, "oc1")
    acc.fill(BTC, "buy", 2, False, D("1"), D("60000"), True, 4 * 3600 * 5 + 1, "oc2")
    roll = acc.read(4 * 3600 * 5 + 2)["positions"][0]["rollover"]
    assert roll == D("1.50")             # 0.01 BTC: 5 full 4 h (1.50); 1 BTC: open 1 s, no full 4 h (0)
    roll = acc.read(4 * 3600 * 6 + 1)["positions"][0]["rollover"]
    assert roll == D("31.80")            # 0.01 BTC: 6 full 4 h (1.80); 1 BTC: 1 full 4 h (30.00)


def test_each_pair_has_its_own_mark_with_its_time_and_no_price_after_a_restart():
    # UNWATCHED-PAIR-MARK-STALE
    acc = SimAccount()
    gw = SimGateway(acc)
    acc.fill(BTC, "buy", 5, False, D("0.3"), D("60000"), True, 0.0, "oc1")
    acc.fill(ETH, "sell", 2, False, D("1"), D("3000"), True, 0.0, "oc2")
    gw.on_book([(D("61000"), D(1))], [(D("61002"), D(1))], 1.0, "BTC/USD")
    gw.on_book([(D("2900"), D(1))], [(D("2902"), D(1))], 2.0, "ETH/USD")      # the owner watches ETH now
    rows = {r["pair"]: r for r in acc.read(3.0)["positions"]}
    assert (rows["BTC/USD"]["mark"], rows["BTC/USD"]["mark_at"], rows["BTC/USD"]["upl"]) == (D("61001"), 1.0, D("0.3") * 1001)
    assert (rows["ETH/USD"]["mark"], rows["ETH/USD"]["mark_at"], rows["ETH/USD"]["upl"]) == (D("2901"), 2.0, D("99"))
    again = SimAccount.from_json(acc.to_json()).read(4.0)
    assert [(r["pair"], r["mark"], r["upl"]) for r in again["positions"]] == [("BTC/USD", None, None), ("ETH/USD", None, None)]
    assert again["unpriced"] == ["BTC/USD", "ETH/USD"]


def test_liquidation_uses_the_mark_of_each_pair_when_its_book_arrives():
    acc = SimAccount(cash=D("2500"))
    gw = SimGateway(acc)
    acc.fill(BTC, "buy", 5, False, D("0.3"), D("60000"), True, 0.0, "oc1")    # 18,000 USD at 5x: 3,600 collateral
    gw.on_book([(D("60000"), D(1))], [(D("60002"), D(1))], 1.0, "BTC/USD")
    gw.on_book([(D("3000"), D(1))], [(D("3002"), D(1))], 2.0, "ETH/USD")     # ETH books never move the BTC mark
    assert acc.marks["BTC/USD"] == (D("60001"), 1.0) and len(acc.positions) == 1
    assert gw.on_book([(D("54000"), D(1))], [(D("54002"), D(1))], 3.0, "BTC/USD") == [core.PositionGone(3.0, "liquidated")]
    assert acc.positions == []


def test_an_old_account_file_with_one_merged_position_per_direction_still_loads():
    old = '{"cash": "4000", "allowance": "100000", "positions": [["BTC/USD", "long", {"qty": "0.05", "entry": "60000", "margin": "1000", "opened": 5.0, "stop": 40}]]}'
    acc = SimAccount.from_json(old)
    assert [(p["pair"], p["dir"], p["qty"], p["leverage"], p["opened"]) for p in acc.positions] == [("BTC/USD", "long", D("0.05"), 3, 5.0)]


# ---------- the SQLite file ----------

def test_the_db_maps_each_leg_to_its_chase_and_reads_rows_of_the_earlier_build(tmp_path):
    import json
    from order_chaser.db import Db
    gw = SimGateway()
    gw.refuse_margin_amends = True
    r = Run(gw).start("buy", "0.05", Margin(3))
    r.book("62418.3", "62418.9", T0 + 6)                   # leg 1
    db = Db(tmp_path)
    db.save(r.c, "dry", T0 + 7)
    assert [db.chase_of(x) for x in ("oc0123456789ab-1", "oc0123456789ab-i", "oc9")] == ["oc0123456789ab"] * 2 + [None]
    # A row of the earlier build: no legs, no leg in a fill, no margin fields, the old IOC id.
    old = json.loads(core.to_json(r.c))
    for k in ("legs", "margin", "replace"):
        old.pop(k)
    for f in old["fills"]:
        f.pop("leg")
    for k in ("leverage_buy", "leverage_sell", "margin_call", "margin_stop", "long_limit", "short_limit"):
        old["pair"].pop(k)
    old.update(id="oc-0123456789abcdef", fills=[{"qty": "0.018", "price": "62417.9", "maker": True, "t": 3.0, "order": "chase"},
                                                {"qty": "0.002", "price": "62418.5", "maker": False, "t": 9.0, "order": "ioc"}])
    db.cx.execute("insert into chase(id, created, mode, phase, outcome, updated, state) values (?,?,?,?,?,?,?)",
                  (old["id"], T0, "dry", "done", "filled", T0, json.dumps(old)))
    c, _ = db.get("oc-0123456789abcdef")
    assert (c.filled, c.legs, c.margin, db.chase_of("oc-0123456789abcdef")) == (D("0.020"), ("oc-0123456789abcdef",), None, "oc-0123456789abcdef")


def test_one_book_that_fills_the_rest_and_liquidates_ends_the_chase_as_liquidated():
    # LIQUIDATION-IN-FINAL-FILL-BOOK-HIDDEN: the fill ends the chase, the liquidation of the same book still reaches it
    r = Run(SimGateway(SimAccount(cash=D("700")))).start("buy", "0.05", Margin(5)).trade("sell", "62417.0", "0.01", T0 + 1)
    r.book("50000.0", "50000.5", T0 + 2)
    assert (r.c.filled, r.c.outcome, r.c.exit, r.gw.account.positions) == (D("0.05"), "liquidated", "liquidated", [])
    assert r.logs[-1] == "The simulated exchange liquidated the position on the same book: the account margin level fell to 40%."
    # A later liquidation (another book) does not change an ended chase.
    done = Run(SimGateway(SimAccount(cash=D("700")))).start("buy", "0.05", Margin(5)).trade("sell", "62417.0", "0.05", T0 + 1)
    assert done.c.outcome == "filled"
    done.book("50000.0", "50000.5", T0 + 2)
    assert (done.c.outcome, done.gw.account.positions) == ("filled", [])


def test_each_part_fill_of_an_open_says_position_opened_once_then_position_now():
    # EVENT-POSITION-OPENED-REPEATS
    r = Run().start("buy", "0.05", Margin(3)).trade("sell", "62417.0", "0.02", T0 + 1).trade("sell", "62417.0", "0.03", T0 + 2)
    fills = [x for x in r.logs if x.startswith("Filled")]
    assert fills == ["Filled 0.0200 BTC at 62,417.90 (maker). Position opened: 0.0200 BTC long, 3x.",
                     "Filled 0.0300 BTC at 62,417.90 (maker). Order complete. Position now: 0.0500 BTC long, 3x."]


# ---------- many seeded random runs ----------

class _Book:
    """A public book as the feed gives it to Engine.on_book: one bid, two ask levels."""

    def __init__(self, bid):
        self.best_bid, self.best_ask = bid, bid + D("0.1")
        self.bids, self.asks = [(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))]

    def top_bids(self):
        return self.bids

    def top_asks(self):
        return self.asks


def _plain(o):
    return json.loads(json.dumps(o, default=server._plain))


def probe(seed: int) -> dict:
    """One random margin chase on a random market, run by the server's Engine on the simulated exchange, then
    some books with no chase. Returns what happened. Raises on a broken rule. After each liquidation it checks
    what the pages read: the server's account (and so the close list) is the simulated account, and the
    result data of the chase says liquidated and offers no close when no position of the chase is left."""
    rnd = random.Random(seed)
    crash = rnd.random() < 0.6          # big moves on a small account: often a liquidation, also on the book of the last fill
    acc = SimAccount(cash=D(rnd.choice(["400", "150", "150"] if crash else ["5000", "900", "400", "150"])))
    tmp = tempfile.TemporaryDirectory()
    now = [0.0]
    eng = server.Engine(Db(Path(tmp.name)), clock=lambda: now[0], latency=0)
    eng.gw = gw = SimGateway(acc)
    gw.refuse_margin_amends = rnd.random() < 0.5
    cross = rnd.random() < 0.3          # orders are often refused would_cross (the book moved while they were in flight)
    bid = D("100.0")
    gw.pair = pair = core.Pair("X/USD", "X", "USD", D("0.1"), D("0.001"), D("0.01"), 1, 8, "online",
                               (2, 3, 4, 5), (2, 3, 4, 5), 80, 40)
    eng.pairs, eng.watched = {"X/USD": pair}, "X/USD"
    gw.on_book(_Book(bid).bids, _Book(bid).asks, -1.0)
    lev = rnd.choice([2, 3, 4, 5])
    close = rnd.random() < 0.5
    d = rnd.choice(["long", "short"])
    # Earlier opens of this direction, each with its own leverage, price and time (mixed leverage).
    for k in range(rnd.choice([0, 1, 2, 3]) if not close else rnd.choice([1, 2, 3])):
        acc.fill(pair, "buy" if d == "long" else "sell", rnd.choice([2, 3, 4, 5]), False, D(rnd.choice(["2", "3", "4"])),
                 bid + D(rnd.choice(["-2", "0", "2"])), True, -20000.0 * (3 - k), f"old{k}")
    before = [dict(p) for p in acc.positions]
    pos0 = sum((p["qty"] for p in before), D(0))
    if close:   # close all or part of the positions, or more (the venue must cut it)
        side = "sell" if d == "long" else "buy"
        qty = rnd.choice([pos0, D("5"), D("9"), pos0 + 1])
        margin = closing(acc, "X/USD", d)
    else:
        side, qty, margin = "buy" if d == "long" else "sell", D(rnd.choice(["1", "5", "20"])), Margin(lev)
    limit = rnd.choice([None, bid + D("3") if side == "buy" else bid - D("3")])    # a limit away from the start price
    cash0 = acc.cash
    eng.read_account(force=True)
    sent = []
    stats = {"replaces": 0, "rate_max": 0.0, "id_max": 0, "cross": 0, "first_refused": 0, "first_in_flight": 0,
             "legs_min_gap": None, "liquidated": False, "in_flight": 0,
             "liquidations": 0, "same_book": False, "no_chase_liquidations": 0}
    legs_at: list[float] = []
    late: list = []          # a refusal of a new leg that arrives after the next event
    asked = None             # what the user asked first and the chase took: "fillnow" or "stop"
    gw_refused = False       # the venue refused an order with its own text (no IOC can follow)
    send, on_book = gw.send, gw.on_book

    def gw_send(cmd, t):
        nonlocal gw_refused
        c = eng.chase
        sent.append(cmd)
        stats["id_max"] = max(stats["id_max"], len(cmd.id))
        place = isinstance(cmd, core.MarginPlace)
        new_leg = place and cmd.id != c.id
        if place:
            legs_at.append(t)                                                           # the first order is a move too
        if new_leg:
            stats["replaces"] += 1
            assert c.rate <= core.RATE_MAX - core.cancel_cost(0), seed                 # room for its cancel
        if place and cross and rnd.random() < 0.6:
            # The venue refuses the order (the first one or a new leg): at once, or after the next event
            # (Fill now or Stop can come first).
            stats["cross" if new_leg else "first_refused"] += 1
            refusal = core.Rejected(t, "place", rnd.choice(["would_cross", "would_cross", "rate_limit"]))
            if rnd.random() < 0.5:
                return [refusal]
            late.append(refusal)
            return []
        evs = send(cmd, t)
        gw_refused |= any(isinstance(ev, core.Rejected) and ev.op in ("place", "ioc") and ev.reason not in ("would_cross", "rate_limit")
                          for ev in evs)
        return evs

    def gw_book(bids, asks, t, symbol=None):
        evs = on_book(bids, asks, t, symbol)
        if core.PositionGone(t, "liquidated") in evs:
            stats["liquidations"] += 1
            stats["no_chase_liquidations"] += not eng.active
            stats["liquidated"] |= eng.active
        return evs

    gw.send, gw.on_book = gw_send, gw_book

    def check_pages_after_liquidation():
        # (a) the server's account is the simulated account: no closed position, no old level
        assert eng.account == _plain(gw.read(now[0])) and eng.account["positions"] == [] and eng.account["level"] is None, seed
        # (c) the close list (the account of the state the pages read) has no row
        assert eng.snapshot()["account"]["positions"] == [], seed

    def book(new_bid):
        n = stats["liquidations"]
        eng.on_book(_Book(new_bid), True)
        if stats["liquidations"] > n:
            check_pages_after_liquidation()

    eng._apply(*core.begin("oc" + f"{seed:012x}", pair, side, qty, bid, bid + D("0.1"), rnd.choice([30, 60, 120]), 0.0,
                           core.SIM_VENUE, limit, margin=margin))
    def user(action):
        nonlocal asked
        exit0 = eng.chase.exit
        eng.user(action)
        if asked is None and exit0 is None and eng.chase.exit:
            asked = eng.chase.exit

    moves = ["-12", "-3", "-0.3", "-0.1", "0.1", "0.2", "0.3", "2", "12"] + (["-30", "30", "-30", "30"] if crash else [])
    while eng.active and now[0] < 400:
        now[0] += rnd.choice([0.2, 0.5, 1, 3])
        r = rnd.random()
        c, pending = eng.chase, late[:]     # the refusals in flight from the last step arrive after this event
        late.clear()
        if pending and rnd.random() < 0.5:     # Fill now or Stop while the new leg is in flight
            stats["in_flight"] += c.phase == "placing" and len(c.legs) > 1
            stats["first_in_flight"] += c.phase == "placing" and len(c.legs) == 1
            user(rnd.choice(["fillnow", "stop"]))
        elif r < 0.45:
            bid = max(D("20"), bid + D(rnd.choice(moves)))
            book(bid)
        elif r < 0.8:
            eng.on_trade(rnd.choice(["sell", "buy"]), bid + D(rnd.choice(["-0.1", "0.2"])), D(rnd.choice(["0.1", "0.7", "3"])))
        elif r < 0.82:
            user("fillnow")
        elif r < 0.83:
            user("stop")
        else:
            eng.tick()
        for ev in pending:
            if eng.active:
                eng.handle(ev)
        stats["rate_max"] = max(stats["rate_max"], eng.chase.rate)
        assert not (acc.of("X/USD", "long") and acc.of("X/USD", "short")), seed       # never long and short at once
        assert sum(o["open"] for o in gw.orders.values()) <= 1, seed                  # one open order per chase at most
        mine = acc.position("X/USD", d)
        assert (mine["qty"] if mine else D(0)) <= pos0 + (0 if close else eng.chase.filled), seed   # a close never grows
    c = eng.chase
    assert c.phase == "done", seed
    # (b) the result data: a liquidation that took the position of the chase ends it as liquidated, and the result
    # offers a close only when a position of the chase is open in the account.
    view = eng.view(c)
    ids = {p.id for p in c.margin.positions} if close else {c.id}
    assert view["open_now"] == any(p["ref"] in ids for p in acc.positions), seed
    if stats["liquidated"]:
        assert view["outcome"] == "liquidated" or view["open_now"], (seed, view["outcome"])
        assert view["outcome"] != "liquidated" or not view["open_now"], seed
        stats["same_book"] = any("on the same book" in e["text"] for e in view["events"])
    assert c.filled <= c.qty, seed                                                  # never more than asked
    # A first order refused as would-cross or for the rate limit waits and goes out again: only the venue's own text ends
    # a chase as "refused".
    assert c.outcome != "refused" or gw_refused, (seed, stats["first_refused"])
    assert sum((x.qty for x in sent if isinstance(x, core.Ioc)), D(0)) <= c.qty, seed
    assert all((f.price <= c.limit) if c.buy else (f.price >= c.limit) for f in c.fills), seed   # never past the cap/floor
    assert all(c.leg_cum(leg) == gw.orders[leg]["cum"] for leg in c.legs if leg in gw.orders), seed   # legs = the venue
    assert stats["id_max"] <= 18 and stats["rate_max"] <= core.RATE_MAX, seed
    gaps = [round(b - a, 6) for a, b in zip(legs_at, legs_at[1:])]
    assert all(g >= core.AMEND_EVERY for g in gaps), (seed, gaps)                  # new legs: at most one each 5 s
    stats["legs_min_gap"] = min(gaps) if gaps else None
    # What Fill now, Stop and the timeout lead to: Stop ends as stopped (or filled);
    # Fill now and the timeout send one IOC for the rest, unless the rest is below the minimum or the venue refused.
    ioc = any(isinstance(x, core.Ioc) for x in sent)
    if c.exit == "stop":
        assert asked == "stop" and c.outcome in ("stopped", "filled"), (seed, c.outcome)
    elif c.exit in ("fillnow", "timeout") and c.outcome == "notfilled":
        assert ioc or gw_refused or c.end_ask is None, (seed, c.exit)   # end_ask None: no valid price, no IOC
    stats["asked"] = asked
    agreed = not stats["liquidated"] and mixed_checks(seed, acc, before, cash0, c, d, close)
    # After the chase: books with no chase. A liquidation still reaches the pages; the old result offers no close then.
    for k in range(rnd.choice([0, 5, 10, 20])):
        now[0] += 1
        if not acc.positions and acc.cash > 0:   # an open of another chase (the account changes with no read of it)
            acc.fill(pair, rnd.choice(["buy", "sell"]), 5, False, D("2"), bid, True, now[0], f"later{k}")
        bid = max(D("20"), bid + D(rnd.choice(["-30", "-12", "-3", "3", "12", "30"])))
        book(bid)
    assert eng.view(c)["open_now"] == any(p["ref"] in ids for p in acc.positions), seed
    tmp.cleanup()
    return {**stats, "agreed": agreed, "part": close and 0 < c.filled < c.qty, "outcome": c.outcome, "close": close, "refuse": gw.refuse_margin_amends, "positions": len(before)}


def mixed_checks(seed, acc, before, cash0, c, d, close):
    """Mixed leverage: a close takes the oldest positions first (FIFO), each pays rollover from its own open;
    an open is a new position of its own and leaves the others as they were. Recomputed here from the fills."""
    left = [dict(p) for p in before]
    if close:
        expect = D(0)
        for f in c.fills:
            take_all = f.qty
            for p in left:
                take = min(take_all, p["qty"])
                if take == 0:
                    continue
                sign = 1 if d == "long" else -1
                roll = core.rollover(take, p["entry"], f.t - p["opened"])
                expect += sign * (f.price - p["entry"]) * take - take * f.price * (core.MAKER_FEE if f.maker else core.TAKER_FEE) - roll
                p["margin"] -= p["margin"] * take / p["qty"]
                p["qty"] -= take
                take_all -= take
            left = [p for p in left if p["qty"] > 0]
        assert [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in acc.positions] == \
            [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in left], seed     # FIFO, partly if needed
        assert abs(acc.cash - cash0 - expect) < D("1e-12"), (seed, acc.cash - cash0, expect)   # rollover of each part
        # The pages' numbers (the close plan of what filled) agree with the account to the cent.
        e = core.margin_summary(c)
        assert cents(acc.cash - cash0, e["pl"] - e["rollover"]), (seed, acc.cash - cash0, e["pl"], e["rollover"])
        takes, stays = core.close_plan(c.margin.positions, c.filled)
        now = acc.parts("X/USD", d)
        assert [(p.id, p.leverage) for p in stays] == [(p.id, p.leverage) for p in now], seed
        assert all(abs(a.qty - b.qty) < D("1e-12") and cents(a.margin, b.margin) for a, b in zip(stays, now)), seed
        assert cents(sum((p["margin"] for p in before), D(0)) - acc.used(), sum((p.margin for p in takes), D(0))), seed
        assert e["stays"] == (core.plan_words(now, D(0), c.pair, c.started)["stays"] if now else ""), seed
        return True
    else:
        old = [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in acc.positions if p["ref"] != c.id]
        assert old == [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in before], seed
        new = [p for p in acc.positions if p["ref"] == c.id]
        assert (sum((p["qty"] for p in new), D(0)), [p["leverage"] for p in new]) == (
            c.filled, [c.margin.leverage] if c.filled else []), seed
        return False


def probe_counts(n: int, first: int = 0) -> dict:
    counts: dict = {"runs": 0, "new_legs": 0, "runs_with_replace": 0, "new_leg_refusals": 0, "runs_with_new_leg_refusal": 0,
                    "legs_min_gap_s": None, "rate_max": 0.0, "id_max": 0, "close_runs_over_2_or_more_positions": 0,
                    "open_runs_beside_other_positions": 0, "liquidated_runs": 0, "liquidations_checked": 0, "of_which_with_no_chase": 0,
                    "of_which_on_the_book_of_the_last_fill": 0, "close_runs_pages_agree_with_the_account": 0,
                    "of_which_part_closes": 0, "fillnow_runs": 0, "stop_runs": 0, "asks_while_a_new_leg_is_in_flight": 0,
                    "first_order_refusals": 0, "runs_with_first_order_refusal": 0, "asks_while_the_first_order_is_in_flight": 0,
                    "outcomes": {}}
    for seed in range(first, first + n):
        r = probe(seed)
        counts["runs"] += 1
        counts["new_legs"] += r["replaces"]
        counts["runs_with_replace"] += r["replaces"] > 0
        counts["new_leg_refusals"] += r["cross"]
        counts["runs_with_new_leg_refusal"] += r["cross"] > 0
        if r["legs_min_gap"] is not None:
            counts["legs_min_gap_s"] = min(r["legs_min_gap"], counts["legs_min_gap_s"] or 1e9)
        counts["rate_max"] = max(counts["rate_max"], r["rate_max"])
        counts["id_max"] = max(counts["id_max"], r["id_max"])
        counts["close_runs_over_2_or_more_positions"] += r["close"] and r["positions"] >= 2
        counts["open_runs_beside_other_positions"] += not r["close"] and r["positions"] >= 1
        counts["liquidated_runs"] += r["liquidated"]
        counts["liquidations_checked"] += r["liquidations"]
        counts["of_which_with_no_chase"] += r["no_chase_liquidations"]
        counts["of_which_on_the_book_of_the_last_fill"] += r["same_book"]
        counts["close_runs_pages_agree_with_the_account"] += r["agreed"]
        counts["of_which_part_closes"] += r["agreed"] and r["part"]
        counts["fillnow_runs"] += r["asked"] == "fillnow"
        counts["stop_runs"] += r["asked"] == "stop"
        counts["asks_while_a_new_leg_is_in_flight"] += r["in_flight"]
        counts["first_order_refusals"] += r["first_refused"]
        counts["runs_with_first_order_refusal"] += r["first_refused"] > 0
        counts["asks_while_the_first_order_is_in_flight"] += r["first_in_flight"]
        key = ("close " if r["close"] else "open ") + r["outcome"]
        counts["outcomes"][key] = counts["outcomes"].get(key, 0) + 1
    return counts


def test_random_margin_runs_keep_every_rule():
    counts = probe_counts(600)
    assert counts["runs"] == 600 and counts["runs_with_replace"] > 30 and counts["runs_with_new_leg_refusal"] > 10
    assert counts["close_runs_over_2_or_more_positions"] > 75 and counts["open_runs_beside_other_positions"] > 75
    # The pages after a liquidation: during a chase, on the book of its last fill, and with no chase.
    assert counts["liquidations_checked"] > 150 and counts["of_which_with_no_chase"] > 100
    assert counts["of_which_on_the_book_of_the_last_fill"] > 1
    assert counts["outcomes"].get("open liquidated", 0) > 3 and counts["outcomes"].get("close liquidated", 0) > 10
    assert counts["close_runs_pages_agree_with_the_account"] > 150 and counts["of_which_part_closes"] > 30
    assert counts["fillnow_runs"] > 30 and counts["stop_runs"] > 15 and counts["asks_while_a_new_leg_is_in_flight"] > 0
    assert counts["runs_with_first_order_refusal"] > 50 and counts["asks_while_the_first_order_is_in_flight"] > 5


def test_an_ioc_that_fills_after_a_liquidation_ends_liquidated_and_names_only_what_stays():
    # LATE-IOC-AFTER-LIQUIDATION-SAYS-FILLED: the core decides the IOC, the exchange liquidates, then the IOC fills.
    pair = core.Pair("BTC/USD", "BTC", "USD", D("0.1"), D("0.0001"), D("0.5"), 1, 8, "online",
                     leverage_buy=(2, 3, 4, 5), leverage_sell=(2, 3, 4, 5), margin_call=80, margin_stop=40)
    gw = SimGateway()
    gw.pair, gw.account.cash = pair, D(1500)
    gw.on_book([(D(60000), D(5))], [(D("60000.1"), D(5))], 0.0, "BTC/USD")
    c, cmds = core.begin("oc000000000001", pair, "buy", D("0.105"), D(60000), D("60000.1"), 30, 0.0, core.SIM_VENUE, margin=Margin(5))
    logs, held = [], []

    def run(c, evs_or_cmds, now, hold=False):
        q = list(evs_or_cmds)
        while q:
            x = q.pop(0)
            if isinstance(x, core.Log):
                logs.append(x.text)
            elif isinstance(x, core.MarginIoc) and hold:
                held.append(x)
            elif hasattr(x, "now"):                     # an event of the exchange
                c, more = core.step(c, x)
                q += more
            else:
                q += gw.send(x, now)
        return c
    c = run(c, cmds, 0.1)
    c = run(c, gw.on_trade("sell", D(59990), D("0.10"), 1.0), 1.0)     # 0.10 of 0.105 fills as maker
    c = run(c, [Book(30.9, D(60000), D("60000.1"), True), Tick(31.0)], 31.0, hold=True)
    assert (c.phase, len(held)) == ("ioc", 1)
    c = run(c, gw.on_book([(D(47000), D(5))], [(D("47000.1"), D(5))], 31.05, "BTC/USD"), 31.05)   # liquidated
    assert (c.exit, gw.account.positions) == ("liquidated", [])
    logs.clear()
    c = run(c, gw.send(held[0], 31.15), 31.15)                         # the IOC reaches the exchange after it
    assert (c.outcome, c.filled, [p["qty"] for p in gw.account.positions]) == ("liquidated", D("0.105"), [D("0.005")])
    assert logs == ["Filled 0.0050 BTC at 47,000.10 (taker, IOC). Order complete. Position now: 0.0050 BTC long, 5x."]
    est = core.margin_summary(c)                                       # collateral and rollover of the 0.005 that stays
    assert (est["collateral"], est["rollover_4h"]) == (D("47.0001"), D("0.11750025"))


if __name__ == "__main__":   # uv run python tests/test_margin.py 5000: the counts of a larger probe
    import json
    import sys
    print(json.dumps(probe_counts(int(sys.argv[1]) if len(sys.argv) > 1 else 2000), indent=1))
