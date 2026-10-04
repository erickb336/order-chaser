"""Margin in the dry run: the core and the simulated exchange and account, with no network."""
import random
from dataclasses import replace
from decimal import Decimal as D

from order_chaser import core
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
    r = Run(SimGateway(acc)).start("sell", "0.05", Margin(5, True, D("0.05"), D("62417.9")))
    r.book("52000.0", "52000.6", T0 + 10)           # the price falls 10,400: the level goes below 40%
    assert r.c.outcome == "liquidated"
    assert isinstance(r.sent[-1], core.Cancel) and not r.gw.orders["oc0123456789ab"]["open"]
    assert acc.positions == []
    assert "liquidated the position: the account margin level fell to 40%" in " ".join(r.logs)


def test_a_reduce_only_close_never_grows_or_flips_the_position():
    acc = opened("0.03")
    # A close of 0.05 against a 0.03 long (the form refuses it; the venue must hold too).
    r = Run(SimGateway(acc)).start("sell", "0.05", Margin(3, True, D("0.03"), D("62417.9")))
    r.trade("buy", "62419.0", "1", T0 + 3)
    assert acc.positions == []                      # closed, and no short opened
    assert r.c.filled == D("0.03") and r.c.outcome == "nopos"
    assert "The long position on BTC/USD is closed. The reduce-only order has nothing left to close." in r.logs


def test_a_reduce_only_order_with_no_position_is_refused():
    r = Run().start("sell", "0.01", Margin(3, True, D("0.01"), D("62417.9")))
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
    assert eq == acc.cash - cost * core.ROLLOVER                    # one started 4 h of rollover
    assert acc.level(T0 + 2) == eq / (cost / 5) * 100
    assert acc.equity(T0 + 4 * 3600 + 2) == acc.cash - cost * core.ROLLOVER * 2


def test_a_close_reports_its_estimated_profit_less_the_close_fee():
    acc = opened("0.03")
    r = Run(SimGateway(acc), bid="62500.0", ask="62500.6").start("sell", "0.03", Margin(3, True, D("0.03"), D("62417.9")))
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
    assert acc.close_entry("BTC/USD", "long", D("0.015")) == (D("0.01") * 60000 + D("0.005") * 61000) / D("0.015")
    assert acc.fill(BTC, "sell", 2, True, D("0.015"), D("62000"), True, 20.0) == D("0.015")
    # The 2x position (the oldest) closes whole; the 5x position closes 0.005 of its 0.02.
    assert [(p["qty"], p["entry"], p["leverage"], p["margin"]) for p in acc.positions] == [
        (D("0.015"), D("61000"), 5, D("0.015") * 61000 / 5)]
    pl = D("0.01") * 2000 + D("0.005") * 1000
    fee = D("0.015") * 62000 * core.MAKER_FEE
    roll = D("0.01") * 60000 * core.ROLLOVER + D("0.005") * 61000 * core.ROLLOVER
    assert acc.cash - cash == pl - fee - roll


def test_rollover_runs_from_the_open_of_each_position():
    # ROLLOVER-FROM-FIRST-OPEN: a second open 20 h after the first pays rollover from its own open.
    acc = SimAccount()
    acc.fill(BTC, "buy", 2, False, D("0.01"), D("60000"), True, 0.0, "oc1")
    acc.fill(BTC, "buy", 2, False, D("1"), D("60000"), True, 4 * 3600 * 5 + 1, "oc2")
    roll = acc.read(4 * 3600 * 5 + 2)["positions"][0]["rollover"]
    assert roll == D("31.80")            # 0.01 BTC: 6 started 4 h (1.80); 1 BTC: 1 started 4 h (30.00)


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


# ---------- many seeded random runs ----------

def probe(seed: int) -> dict:
    """One random margin chase on a random market; returns what happened. Raises on a broken rule."""
    rnd = random.Random(seed)
    acc = SimAccount(cash=D(rnd.choice(["5000", "900", "400", "150"])))
    gw = SimGateway(acc)
    gw.refuse_margin_amends = rnd.random() < 0.5
    cross = rnd.random() < 0.3          # new legs are often refused would_cross (the book moved while they were in flight)
    bid = D("100.0")
    gw.pair = pair = core.Pair("X/USD", "X", "USD", D("0.1"), D("0.001"), D("0.01"), 1, 8, "online",
                               (2, 3, 4, 5), (2, 3, 4, 5), 80, 40)
    gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))], -1.0)
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
        g = acc.position("X/USD", d)
        margin = Margin(g["leverage"], True, pos0, acc.close_entry("X/USD", d, qty))
    else:
        side, qty, margin = "buy" if d == "long" else "sell", D(rnd.choice(["1", "5", "20"])), Margin(lev)
    limit = rnd.choice([None, bid + D("3") if side == "buy" else bid - D("3")])    # a limit away from the start price
    cash0 = acc.cash
    c, cmds = core.begin("oc" + f"{seed:012x}", pair, side, qty, bid, bid + D("0.1"), rnd.choice([30, 60, 120]), 0.0,
                         core.SIM_VENUE, limit, margin=margin)
    sent, now, queue = [], 0.0, [x for x in cmds if not isinstance(x, core.Log)]
    stats = {"replaces": 0, "rate_max": 0.0, "id_max": 0, "cross": 0, "legs_min_gap": None, "liquidated": False}
    legs_at: list[float] = []

    def feed(evs):
        nonlocal c
        for ev in evs:
            stats["liquidated"] |= isinstance(ev, core.PositionGone) and ev.reason == "liquidated"
            c, m = core.step(c, ev)
            queue.extend(x for x in m if not isinstance(x, core.Log))

    while c.phase != "done" and now < 400:
        while queue:
            cmd = queue.pop(0)
            sent.append(cmd)
            stats["id_max"] = max(stats["id_max"], len(cmd.id))
            new_leg = isinstance(cmd, core.MarginPlace) and cmd.id != c.id
            if new_leg:
                stats["replaces"] += 1
                legs_at.append(now)
                assert c.rate <= core.RATE_MAX - core.cancel_cost(0), seed                 # room for its cancel
            if new_leg and cross and rnd.random() < 0.6:
                stats["cross"] += 1
                feed([core.Rejected(now, "place", "would_cross")])
            else:
                feed(gw.send(cmd, now))
            stats["rate_max"] = max(stats["rate_max"], c.rate)
        now += rnd.choice([0.2, 0.5, 1, 3])
        r = rnd.random()
        if r < 0.45:
            bid = max(D("20"), bid + D(rnd.choice(["-12", "-3", "-0.3", "-0.1", "0.1", "0.2", "0.3", "2", "12"])))
            feed(gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))], now))
            feed([Book(now, bid, bid + D("0.1"), True)])
        elif r < 0.8:
            feed(gw.on_trade(rnd.choice(["sell", "buy"]), bid + D(rnd.choice(["-0.1", "0.2"])), D(rnd.choice(["0.1", "0.7", "3"])), now))
        elif r < 0.82:
            feed([core.UserFillNow(now)])
        elif r < 0.83:
            feed([UserStop(now)])
        else:
            feed([Tick(now)])
        assert not (acc.of("X/USD", "long") and acc.of("X/USD", "short")), seed       # never long and short at once
        assert sum(o["open"] for o in gw.orders.values()) <= 1, seed                  # one open order per chase at most
        mine = acc.position("X/USD", d)
        assert (mine["qty"] if mine else D(0)) <= pos0 + (0 if close else c.filled), seed   # a close never grows
    assert c.phase == "done", seed
    assert c.filled <= c.qty, seed                                                  # never more than asked
    assert sum((x.qty for x in sent if isinstance(x, core.Ioc)), D(0)) <= c.qty, seed
    assert all((f.price <= c.limit) if c.buy else (f.price >= c.limit) for f in c.fills), seed   # never past the cap/floor
    assert all(c.leg_cum(leg) == gw.orders[leg]["cum"] for leg in c.legs if leg in gw.orders), seed   # legs = the venue
    assert stats["id_max"] <= 18 and stats["rate_max"] <= core.RATE_MAX, seed
    gaps = [round(b - a, 6) for a, b in zip(legs_at, legs_at[1:])]
    assert all(g >= core.AMEND_EVERY for g in gaps), (seed, gaps)                  # new legs: at most one each 5 s
    stats["legs_min_gap"] = min(gaps) if gaps else None
    if not stats["liquidated"]:
        mixed_checks(seed, acc, before, cash0, c, d, close)
    return {**stats, "outcome": c.outcome, "close": close, "refuse": gw.refuse_margin_amends, "positions": len(before)}


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
                roll = take * p["entry"] * core.ROLLOVER * core.rollover_periods(f.t - p["opened"])
                expect += sign * (f.price - p["entry"]) * take - take * f.price * (core.MAKER_FEE if f.maker else core.TAKER_FEE) - roll
                p["margin"] -= p["margin"] * take / p["qty"]
                p["qty"] -= take
                take_all -= take
            left = [p for p in left if p["qty"] > 0]
        assert [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in acc.positions] == \
            [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in left], seed     # FIFO, partly if needed
        assert abs(acc.cash - cash0 - expect) < D("1e-12"), (seed, acc.cash - cash0, expect)   # rollover of each part
    else:
        old = [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in acc.positions if p["ref"] != c.id]
        assert old == [(p["qty"], p["entry"], p["leverage"], p["opened"]) for p in before], seed
        new = [p for p in acc.positions if p["ref"] == c.id]
        assert (sum((p["qty"] for p in new), D(0)), [p["leverage"] for p in new]) == (
            c.filled, [c.margin.leverage] if c.filled else []), seed


def probe_counts(n: int, first: int = 0) -> dict:
    counts: dict = {"runs": 0, "new_legs": 0, "runs_with_replace": 0, "would_cross_refusals": 0, "runs_with_would_cross": 0,
                    "legs_min_gap_s": None, "rate_max": 0.0, "id_max": 0, "close_runs_over_2_or_more_positions": 0,
                    "open_runs_beside_other_positions": 0, "liquidated_runs": 0, "outcomes": {}}
    for seed in range(first, first + n):
        r = probe(seed)
        counts["runs"] += 1
        counts["new_legs"] += r["replaces"]
        counts["runs_with_replace"] += r["replaces"] > 0
        counts["would_cross_refusals"] += r["cross"]
        counts["runs_with_would_cross"] += r["cross"] > 0
        if r["legs_min_gap"] is not None:
            counts["legs_min_gap_s"] = min(r["legs_min_gap"], counts["legs_min_gap_s"] or 1e9)
        counts["rate_max"] = max(counts["rate_max"], r["rate_max"])
        counts["id_max"] = max(counts["id_max"], r["id_max"])
        counts["close_runs_over_2_or_more_positions"] += r["close"] and r["positions"] >= 2
        counts["open_runs_beside_other_positions"] += not r["close"] and r["positions"] >= 1
        counts["liquidated_runs"] += r["liquidated"]
        key = ("close " if r["close"] else "open ") + r["outcome"]
        counts["outcomes"][key] = counts["outcomes"].get(key, 0) + 1
    return counts


def test_random_margin_runs_keep_every_rule():
    counts = probe_counts(400)
    assert counts["runs"] == 400 and counts["runs_with_replace"] > 20 and counts["runs_with_would_cross"] > 10
    assert counts["close_runs_over_2_or_more_positions"] > 50 and counts["open_runs_beside_other_positions"] > 50
    assert any(k.endswith("liquidated") for k in counts["outcomes"])


if __name__ == "__main__":   # uv run python tests/test_margin.py 5000: the counts of a larger probe
    import json
    import sys
    print(json.dumps(probe_counts(int(sys.argv[1]) if len(sys.argv) > 1 else 2000), indent=1))
