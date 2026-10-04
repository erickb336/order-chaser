"""Tests of core.step with no network. A SimGateway answers the commands where a test needs a venue."""
import random
from decimal import Decimal as D

from order_chaser import core
from order_chaser.core import (Amend, Amended, Book, Cancel, Canceled, FeedBack, FeedLost, Filled, Ioc, IocDone,
                               OrderState, Place, Placed, Query, Rejected, Restarted, Tick, UserFillNow, UserStop)
from order_chaser.sim import SimGateway

BTC = core.Pair("BTC/USD", "BTC", "USD", D("0.1"), D("0.00005"), D("0.5"), 1, 8, "online")
T0 = 1000.0


def start(side="buy", qty="0.05", bid="62417.9", ask="62418.5", timeout=120, limit=None, rate=0.0):
    c, cmds = core.begin("oc-1", BTC, side, D(qty), D(bid), D(ask), timeout, T0, "the simulation",
                         None if limit is None else D(limit), rate)
    return c, cmds


def run(c, *events):
    """Step through events; return the last chase and all commands, logs left out."""
    out = []
    for ev in events:
        c, cmds = core.step(c, ev)
        out += [x for x in cmds if not isinstance(x, core.Log)]
    return c, out


def resting(**kw):
    c, cmds = start(**kw)
    c, _ = core.step(c, Placed(T0))
    return c


def logs(c, *events):
    texts = []
    for ev in events:
        c, cmds = core.step(c, ev)
        texts += [x.text for x in cmds if isinstance(x, core.Log)]
    return c, texts


# ---------- start ----------

def test_begin_records_the_start_ask_as_cap_and_places_post_only_at_the_bid():
    c, cmds = start()
    assert c.limit == D("62418.5")
    assert [x for x in cmds if not isinstance(x, core.Log)] == [Place("oc-1", "buy", D("62417.9"), D("0.05"))]
    assert cmds[0].text == "Recorded the start ask, 62,418.50, as the cap."


def test_validate_blocks_a_pair_that_is_not_online_and_an_amount_below_the_minimums():
    off = core.Pair(**{**BTC.__dict__, "status": "cancel_only"})
    assert core.validate(off, "buy", D("0.05"), None, D("62417.9"), D("62418.5"), True, 120) == [
        "Kraken accepts no new chase for BTC/USD now. Pair status: cancel_only."]
    assert core.validate(BTC, "buy", D("0.000005"), None, D("62417.9"), D("62418.5"), True, 120) == [
        "The amount is below the Kraken minimums for BTC/USD: at least 0.00005 BTC and 0.5 USD."]
    assert core.validate(BTC, "buy", D("0.05"), None, D("62417.9"), D("62418.5"), True, 120) == []
    assert core.validate(BTC, "buy", D("0.05"), None, D("62417.9"), D("62418.5"), True, 20) == [
        "The timeout must be from 30 s to 15 min."]
    assert core.validate(BTC, "buy", D("0.05"), None, D("62417.9"), D("62418.5"), False, 120) == ["Start needs live prices."]


# ---------- the cap ----------

def test_amends_follow_the_bid_but_never_pass_the_cap():
    c = resting()
    amends = []
    now = T0
    for bid in ["62418.1", "62418.3", "62419.0", "62425.0", "62440.0"]:
        now += 6
        c, cmds = run(c, Book(now, D(bid), D(bid) + D("0.1"), True))
        amends += [x.price for x in cmds if isinstance(x, Amend)]
        if c.phase == "amending":
            c, _ = run(c, Amended(now))
    assert amends == [D("62418.1"), D("62418.3"), D("62418.5")]
    assert c.price == D("62418.5")


def test_the_fallback_ioc_goes_out_at_the_cap_even_when_the_ask_is_far_above():
    c = resting()
    c, cmds = run(c, Book(T0 + 1, D("62430.0"), D("62431.0"), True), Tick(T0 + 121), Canceled(T0 + 121),
                  OrderState(T0 + 121, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-ioc", "buy", D("62418.5"), D("0.05"))]


# ---------- partial fills and the remainder ----------

def test_remainder_after_partial_fills_sizes_the_ioc():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True), Filled(T0 + 40, D("0.002"), D("62417.9"), True),
                  Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D("0.020"), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-ioc", "buy", D("62418.5"), D("0.030"))]
    assert c.phase == "ioc"


def test_reread_trusts_the_venue_when_it_reports_more_filled_than_the_tool_saw():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True), Tick(T0 + 120), Canceled(T0 + 120),
                  OrderState(T0 + 120, False, D("0.025"), None))
    assert [x.qty for x in cmds if isinstance(x, Ioc)] == [D("0.025")]


# ---------- the fallback order ----------

def test_timeout_fallback_is_cancel_then_canceled_event_then_reread_then_ioc_for_the_remainder_only():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True))

    c, cmds = run(c, Tick(T0 + 119))
    assert cmds == [] and c.phase == "resting"

    c, cmds = run(c, Tick(T0 + 120))
    assert cmds == [Cancel("oc-1")] and c.phase == "cancelling"

    # A late fill can arrive before the cancel lands: no order goes out yet.
    c, cmds = run(c, Filled(T0 + 120.1, D("0.002"), D("62418.1"), True), Tick(T0 + 121))
    assert cmds == []

    c, cmds = run(c, Canceled(T0 + 121))
    assert cmds == [Query("oc-1")] and c.phase == "reread"

    c, cmds = run(c, Tick(T0 + 121.5))
    assert cmds == []

    c, cmds = run(c, OrderState(T0 + 122, False, D("0.020"), None))
    assert cmds == [Ioc("oc-1-ioc", "buy", D("62418.5"), D("0.030"))]

    c, _ = run(c, Filled(T0 + 122, D("0.030"), D("62418.5"), False, "ioc"), IocDone(T0 + 122))
    assert (c.phase, c.outcome, c.filled) == ("done", "filled", D("0.050"))


def test_ioc_that_fills_nothing_above_the_cap_ends_as_not_filled():
    c = resting()
    c, texts = logs(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True), Book(T0 + 100, D("62429.4"), D("62431.0"), True),
                    Amended(T0 + 100), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 121, False, D("0.018"), None), IocDone(T0 + 121))
    assert (c.phase, c.outcome, c.filled) == ("done", "notfilled", D("0.018"))
    assert texts[-1] == "IOC buy, 0.0320 BTC at 62,418.50: nothing filled. The ask is 62,431.00."


def test_remainder_below_the_minimum_stops_as_not_filled_with_no_ioc():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.04997"), D("62417.9"), True), Tick(T0 + 120), Canceled(T0 + 120),
                  OrderState(T0 + 120, False, D("0.04997"), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == []
    assert (c.phase, c.outcome) == ("done", "belowmin")


def test_remainder_below_costmin_also_stops():
    cheap = core.Pair("X/USD", "X", "USD", D("0.0001"), D("1"), D("5"), 4, 8, "online")
    c, _ = core.begin("oc-2", cheap, "buy", D("10"), D("1.0000"), D("1.0001"), 60, T0, "the simulation")
    c, cmds = run(c, Placed(T0), Filled(T0 + 5, D("6"), D("1.0000"), True), Tick(T0 + 60), Canceled(T0 + 60),
                  OrderState(T0 + 60, False, D("6"), None))
    # The rest, 4 X, is above ordermin (1) but 4 x 1.0001 is below costmin (5).
    assert [x for x in cmds if isinstance(x, Ioc)] == []
    assert c.outcome == "belowmin"


# ---------- never more than asked ----------

def test_never_buys_more_than_asked_on_random_markets():
    rnd = random.Random(7)
    for trial in range(300):
        gw = SimGateway()
        bid = D("100.0")
        gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))])
        pair = core.Pair("X/USD", "X", "USD", D("0.1"), D("0.001"), D("0.01"), 1, 8, "online")
        c, cmds = core.begin("oc", pair, "buy", D("1.0"), bid, bid + D("0.1"), 30, 0.0, "the simulation")
        sent_qty = D(0)
        now = 0.0
        queue = [x for x in cmds if not isinstance(x, core.Log)]
        while c.phase != "done" and now < 200:
            while queue:
                cmd = queue.pop(0)
                if isinstance(cmd, (Place, Ioc)):
                    sent_qty += cmd.qty
                for ev in gw.send(cmd, now):
                    c, more = core.step(c, ev)
                    queue += [x for x in more if not isinstance(x, core.Log)]
            now += rnd.choice([0.5, 1, 3])
            r = rnd.random()
            if r < 0.4:
                bid += D(rnd.choice(["-0.3", "-0.1", "0.1", "0.2"]))
                gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))])
                ev = Book(now, bid, bid + D("0.1"), True)
                c, more = core.step(c, ev)
            elif r < 0.8:
                more = []
                for ev in gw.on_trade("sell", bid - D("0.1"), D(rnd.choice(["0.1", "0.25", "0.7"])), now):
                    c, m = core.step(c, ev)
                    more += m
            elif r < 0.83:
                c, more = core.step(c, UserFillNow(now))
            else:
                c, more = core.step(c, Tick(now))
            queue += [x for x in more if not isinstance(x, core.Log)]
        assert c.filled <= c.qty, trial
        assert c.phase == "done", trial
        assert all(f.price <= c.limit for f in c.fills), trial


# ---------- amend throttle ----------

def amend_times(c, seconds):
    """The bid rises one tick each second. Return the seconds (since start) of each amend."""
    times = []
    for s in range(1, seconds + 1):
        c, cmds = run(c, Book(T0 + s, D("62400.0") + D("0.1") * s, D("62418.5"), True))
        if any(isinstance(x, Amend) for x in cmds):
            times.append(s)
            c, _ = run(c, Amended(T0 + s))
    return c, times


def test_amends_at_most_every_5_seconds():
    c = resting(bid="62400.0")
    c, times = amend_times(c, 22)
    assert times == [5, 10, 15, 20]


def test_amends_every_15_seconds_above_40_on_the_rate_counter():
    c, _ = start(bid="62400.0", rate=55)
    c, texts = logs(c, Placed(T0))
    assert texts == ["Placed a post-only buy, 0.0500 BTC at 62,400.00.",
                     "Estimated rate counter at 56 of 60. Next amend in 15 s, not 5 s."]
    c, times = amend_times(c, 40)
    # First amend waits 15 s, not 5 s. At 20 s the counter is 56 - 20 + 1 = 37, below 40: back to every 5 s.
    assert times == [15, 20, 25, 30, 35, 40]


def test_rate_counter_costs():
    assert [core.amend_cost(a) for a in (0, 5, 10, 15)] == [4, 3, 2, 1]
    assert [core.cancel_cost(a) for a in (0, 5, 10, 15, 45, 90, 300)] == [8, 6, 5, 4, 2, 1, 0]
    c, _ = start(rate=10)               # add order +1
    assert c.rate == 11
    c, _ = run(c, Placed(T0), Book(T0 + 6, D("62418.0"), D("62418.5"), True))  # amend at age 6: +1 +2, decay 6
    assert c.rate == 11 - 6 + 3


# ---------- amend reject ----------

def test_no_amend_to_a_price_that_would_cross_the_other_side():
    c = resting()
    c, cmds = run(c, Book(T0 + 6, D("62418.3"), D("62418.3"), True))
    assert cmds == []
    c, cmds = run(c, Book(T0 + 7, D("62418.3"), D("62418.4"), True))
    assert cmds == [Amend("oc-1", D("62418.3"))]


def test_amend_reject_reads_the_order_again_and_does_not_retry_blindly():
    c = resting()
    c, cmds = run(c, Book(T0 + 6, D("62418.1"), D("62418.5"), True))
    assert cmds == [Amend("oc-1", D("62418.1"))]
    c, texts = logs(c, Rejected(T0 + 6.2, "amend", "would_cross"))
    assert texts == ["Amend to 62,418.10 rejected: would cross the ask (post-only)."]
    assert c.phase == "reconcile"
    # While the order state is unknown, nothing is sent, also on new prices.
    c, cmds = run(c, Book(T0 + 7, D("62418.2"), D("62418.5"), True), Tick(T0 + 12))
    assert cmds == []
    c, cmds = run(c, OrderState(T0 + 12, True, D(0), D("62417.9")))
    assert c.phase in ("resting", "amending") and c.price == D("62417.9")


def test_amend_reject_for_rate_limit_sets_the_counter_to_the_maximum_and_slows_amends():
    c = resting()
    c, _ = run(c, Book(T0 + 6, D("62418.1"), D("62418.5"), True), Rejected(T0 + 6, "amend", "rate_limit"),
               OrderState(T0 + 6, True, D(0), D("62417.9")))
    assert c.rate == 60 and c.slow
    c, cmds = run(c, Book(T0 + 12, D("62418.2"), D("62418.5"), True))
    assert cmds == []                    # 6 s after the last amend: too early at 15 s


# ---------- stop and fill the rest now ----------

def test_stop_cancels_and_ends_with_no_ioc():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True), UserStop(T0 + 55))
    assert cmds == [Cancel("oc-1")]
    c, cmds = run(c, Tick(T0 + 200), Canceled(T0 + 55))
    assert cmds == []
    assert (c.phase, c.outcome, c.filled) == ("done", "stopped", D("0.018"))


def test_stop_while_placing_waits_for_the_order_then_cancels():
    c, _ = start()
    c, cmds = run(c, UserStop(T0 + 0.1))
    assert cmds == []
    c, cmds = run(c, Placed(T0 + 0.2))
    assert cmds == [Cancel("oc-1")]


def test_fill_the_rest_now_runs_the_fallback_at_once():
    c = resting()
    c, cmds = run(c, Filled(T0 + 10, D("0.01"), D("62417.9"), True), UserFillNow(T0 + 20))
    assert cmds == [Cancel("oc-1")]
    c, cmds = run(c, Canceled(T0 + 20), OrderState(T0 + 20, False, D("0.01"), None))
    assert cmds == [Query("oc-1"), Ioc("oc-1-ioc", "buy", D("62418.5"), D("0.04"))]


# ---------- sell is the mirror ----------

def test_sell_rests_at_the_ask_moves_down_never_below_the_floor_and_ioc_at_the_floor():
    c, cmds = start(side="sell")
    assert c.limit == D("62417.9")           # floor = the start bid
    assert [x for x in cmds if isinstance(x, Place)] == [Place("oc-1", "sell", D("62418.5"), D("0.05"))]
    c, _ = run(c, Placed(T0))
    amends = []
    now = T0
    for ask in ["62418.3", "62418.0", "62417.0", "62410.0"]:
        now += 6
        c, cmds = run(c, Book(now, D(ask) - D("0.1"), D(ask), True))
        amends += [x.price for x in cmds if isinstance(x, Amend)]
        if c.phase == "amending":
            c, _ = run(c, Amended(now))
    assert amends == [D("62418.3"), D("62418.0"), D("62417.9")]
    c, cmds = run(c, Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-ioc", "sell", D("62417.9"), D("0.05"))]


def test_sell_saving_counts_money_received():
    c = resting(side="sell")
    c, _ = run(c, Filled(T0 + 5, D("0.05"), D("62418.5"), True))
    s = core.summary(c)
    # received 0.05 x 62418.5 x (1 - 0.004) = 3108.441300; market 0.05 x 62417.9 x (1 - 0.008) = 3095.927840
    assert s["ours"] == D("3108.4413000")
    assert s["market"] == D("3095.9278400")
    assert s["saving"] == D("12.5134600")


# ---------- the feed ----------

def test_no_amend_on_a_stale_book_until_it_is_valid_again():
    c = resting()
    c, texts = logs(c, Book(T0 + 6, D("62418.1"), D("62418.5"), False))
    assert texts == ["The order book checksum did not match. Reading the book again. No amend until the book is valid."]
    c, cmds = run(c, Tick(T0 + 8))
    assert cmds == [] and c.phase == "resting"
    c, cmds = run(c, Book(T0 + 9, D("62418.1"), D("62418.5"), True))
    assert cmds == [Amend("oc-1", D("62418.1"))]


def test_feed_lost_freezes_the_order_then_reconciles():
    c = resting()
    c, texts = logs(c, FeedLost(T0 + 55))
    assert c.phase == "feed_lost"
    assert texts == ["Lost the public Kraken price feed. Order frozen at 62,417.90."]
    c, cmds = run(c, Book(T0 + 60, D("62418.2"), D("62418.5"), True), Tick(T0 + 200))
    assert cmds == [] and c.phase == "feed_lost"   # no amend, and no timeout without prices
    c, cmds = run(c, FeedBack(T0 + 201))
    assert cmds == [Query("oc-1")] and c.phase == "reconcile"
    c, cmds = run(c, OrderState(T0 + 201, True, D(0), D("62417.9")))
    assert cmds == [Amend("oc-1", D("62418.2"))]   # the book seen during the outage is not used...


def test_stop_works_while_the_feed_is_lost():
    c = resting()
    c, cmds = run(c, FeedLost(T0 + 5), UserStop(T0 + 6))
    assert cmds == [Cancel("oc-1")]


# ---------- restart ----------

def test_restart_ends_an_unfinished_dry_run_and_does_not_resume_it():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True))
    c, texts = logs(c, Restarted(T0 + 70, T0 + 62))
    assert (c.phase, c.outcome) == ("done", "ended")
    assert texts == ["Tool started again after 8 s off. Ended the simulated order. No order was on Kraken.",
                     "Recorded the simulated fills: 0.0180 of 0.0500 BTC.", "Did not continue the dry run."]
    c, cmds = run(c, Tick(T0 + 200), Book(T0 + 201, D("62418.1"), D("62418.5"), True))
    assert cmds == []


# ---------- result numbers ----------

def test_saving_against_a_market_order_at_the_start_matches_the_design_sample():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True), Filled(T0 + 78, D("0.032"), D("62418.3"), True))
    s = core.summary(c)
    assert c.outcome == "filled"
    assert round(s["avg"], 2) == D("62418.23")
    assert round(s["fee"], 2) == D("12.48")
    assert round(s["market"], 2) == D("3145.89")
    assert round(s["saving"], 2) == D("12.50")


def test_maker_saving_is_not_below_zero_with_the_default_cap():
    # Maker fills at the cap, the worst maker price, still save the fee difference.
    c = resting()
    c, _ = run(c, Filled(T0 + 5, D("0.05"), D("62418.5"), True))
    assert core.summary(c)["saving_maker"] == D("12.4837000")


def test_worst_case_with_a_higher_limit():
    assert round(core.worst_case("buy", D("0.05"), D("62480.00")), 2) == D("3148.99")
    assert round(core.worst_case("buy", D("0.05"), D("62418.5")), 2) == D("3145.89")


def test_state_survives_json():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True))
    assert core.from_json(core.to_json(c)) == c
