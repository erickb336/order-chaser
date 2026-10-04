"""Margin in the dry run: the core and the simulated exchange and account, with no network."""
import random
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


# ---------- the account and the position ----------

def test_liquidation_ends_the_chase_and_cancels_its_order():
    acc = opened("0.05", 5)                         # 3,120.9 USD long at 5x: 624 USD collateral
    acc.cash = D("700")                             # a small account: the level is near 110%
    r = Run(SimGateway(acc)).start("sell", "0.05", Margin(5, True, D("0.05"), D("62417.9")))
    r.book("52000.0", "52000.6", T0 + 10)           # the price falls 10,400: the level goes below 40%
    assert r.c.outcome == "liquidated"
    assert isinstance(r.sent[-1], core.Cancel) and not r.gw.orders["oc0123456789ab"]["open"]
    assert acc.positions == {}
    assert "liquidated the position: the account margin level fell to 40%" in " ".join(r.logs)


def test_a_reduce_only_close_never_grows_or_flips_the_position():
    acc = opened("0.03")
    # A close of 0.05 against a 0.03 long (the form refuses it; the venue must hold too).
    r = Run(SimGateway(acc)).start("sell", "0.05", Margin(3, True, D("0.03"), D("62417.9")))
    r.trade("buy", "62419.0", "1", T0 + 3)
    assert acc.positions == {}                      # closed, and no short opened
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
    bid = D("100.0")
    gw.pair = pair = core.Pair("X/USD", "X", "USD", D("0.1"), D("0.001"), D("0.01"), 1, 8, "online",
                               (2, 3, 4, 5), (2, 3, 4, 5), 80, 40)
    gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))], 0.0)
    lev = rnd.choice([2, 3, 4, 5])
    close = rnd.random() < 0.5
    if close:   # open a position first, then close all or part of it
        d = rnd.choice(["long", "short"])
        acc.fill(pair, "buy" if d == "long" else "sell", lev, False, D("8"), bid, True, 0.0)
        pos0 = acc.position("X/USD", d)["qty"]
        side = "sell" if d == "long" else "buy"
        qty = rnd.choice([pos0, D("5"), D("9")])          # 9: more than the position (the venue must cut it)
        margin = Margin(lev, True, pos0, bid)
    else:
        d, side, qty, margin = None, rnd.choice(["buy", "sell"]), D(rnd.choice(["1", "5", "20"])), Margin(lev)
    limit = rnd.choice([None, bid + D("3") if side == "buy" else bid - D("3")])    # a limit away from the start price
    c, cmds = core.begin("oc" + f"{seed:012x}", pair, side, qty, bid, bid + D("0.1"), rnd.choice([30, 60, 120]), 0.0,
                         core.SIM_VENUE, limit, margin=margin)
    sent, now, queue = [], 0.0, [x for x in cmds if not isinstance(x, core.Log)]
    stats = {"replaces": 0, "rate_max": 0.0, "id_max": 0}

    def feed(evs):
        nonlocal c
        for ev in evs:
            c, m = core.step(c, ev)
            queue.extend(x for x in m if not isinstance(x, core.Log))

    while c.phase != "done" and now < 400:
        while queue:
            cmd = queue.pop(0)
            sent.append(cmd)
            stats["id_max"] = max(stats["id_max"], len(cmd.id))
            stats["replaces"] += isinstance(cmd, core.MarginPlace) and cmd.id != c.id
            feed(gw.send(cmd, now))
            stats["rate_max"] = max(stats["rate_max"], c.rate)
        now += rnd.choice([0.5, 1, 3])
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
        if close and d:
            p = acc.position("X/USD", d)
            other = acc.position("X/USD", "short" if d == "long" else "long")
            assert other is None and (p is None or p["qty"] <= pos0), seed          # never flips, never grows
    assert c.phase == "done", seed
    assert c.filled <= c.qty, seed                                                  # never more than asked
    assert sum((x.qty for x in sent if isinstance(x, core.Ioc)), D(0)) <= c.qty, seed
    assert all((f.price <= c.limit) if c.buy else (f.price >= c.limit) for f in c.fills), seed   # never past the cap/floor
    assert all(c.leg_cum(leg) == gw.orders[leg]["cum"] for leg in c.legs if leg in gw.orders), seed   # legs = the venue
    assert stats["id_max"] <= 18 and stats["rate_max"] <= core.RATE_MAX, seed
    return {**stats, "outcome": c.outcome, "close": close, "refuse": gw.refuse_margin_amends}


def probe_counts(n: int, first: int = 0) -> dict:
    counts: dict = {"runs": 0, "replaces": 0, "runs_with_replace": 0, "rate_max": 0.0, "id_max": 0, "outcomes": {}}
    for seed in range(first, first + n):
        r = probe(seed)
        counts["runs"] += 1
        counts["replaces"] += r["replaces"]
        counts["runs_with_replace"] += r["replaces"] > 0
        counts["rate_max"] = max(counts["rate_max"], r["rate_max"])
        counts["id_max"] = max(counts["id_max"], r["id_max"])
        key = ("close " if r["close"] else "open ") + r["outcome"]
        counts["outcomes"][key] = counts["outcomes"].get(key, 0) + 1
    return counts


def test_random_margin_runs_keep_every_rule():
    counts = probe_counts(400)
    assert counts["runs"] == 400 and counts["runs_with_replace"] > 20
    assert any(k.endswith("liquidated") for k in counts["outcomes"])


if __name__ == "__main__":   # uv run python tests/test_margin.py 5000: the counts of a larger probe
    import json
    import sys
    print(json.dumps(probe_counts(int(sys.argv[1]) if len(sys.argv) > 1 else 2000), indent=1))
