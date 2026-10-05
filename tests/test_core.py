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
    c, cmds = core.begin("oc-1", BTC, side, D(qty), D(bid), D(ask), timeout, T0, core.SIM_VENUE,
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


def beat(c, t):
    """The book again at time t, as the feed re-sends it on each heartbeat: the prices are fresh."""
    return Book(t, c.bid, c.ask, True)


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
        "Pick a timeout from the list: 30 s to 15 min."]
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
    c, cmds = run(c, Book(T0 + 1, D("62430.0"), D("62431.0"), True), Tick(T0 + 121),
                  Book(T0 + 121, D("62430.0"), D("62431.0"), True), Canceled(T0 + 121), OrderState(T0 + 121, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-i", "buy", D("62418.5"), D("0.05"))]


# ---------- partial fills and the remainder ----------

def test_remainder_after_partial_fills_sizes_the_ioc():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True, cum=D("0.018")),
                  Filled(T0 + 40, D("0.002"), D("62417.9"), True, cum=D("0.020")),
                  beat(c, T0 + 120), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D("0.020"), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-i", "buy", D("62418.5"), D("0.030"))]
    assert c.phase == "ioc"


def test_reread_trusts_the_venue_when_it_reports_more_filled_than_the_tool_saw():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True, cum=D("0.018")), beat(c, T0 + 120), Tick(T0 + 120),
                  Canceled(T0 + 120), OrderState(T0 + 120, False, D("0.025"), None))
    assert [x.qty for x in cmds if isinstance(x, Ioc)] == [D("0.025")]


# ---------- the fallback order ----------

def test_timeout_fallback_is_cancel_then_canceled_event_then_reread_then_ioc_for_the_remainder_only():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True, cum=D("0.018")))

    c, cmds = run(c, Tick(T0 + 119))
    assert cmds == [] and c.phase == "resting"

    c, cmds = run(c, Tick(T0 + 120))
    assert cmds == [Cancel("oc-1")] and c.phase == "cancelling"

    # A late fill can arrive before the cancel lands: no order goes out yet.
    c, cmds = run(c, Filled(T0 + 120.1, D("0.002"), D("62418.1"), True, cum=D("0.020")), Tick(T0 + 121))
    assert cmds == []

    c, cmds = run(c, Canceled(T0 + 121))
    assert cmds == [Query("oc-1")] and c.phase == "reread"

    c, cmds = run(c, Tick(T0 + 121.5))
    assert cmds == []

    c, cmds = run(c, beat(c, T0 + 122), OrderState(T0 + 122, False, D("0.020"), None))
    assert cmds == [Ioc("oc-1-i", "buy", D("62418.5"), D("0.030"))]

    c, _ = run(c, Filled(T0 + 122, D("0.030"), D("62418.5"), False, "ioc"), IocDone(T0 + 122))
    assert (c.phase, c.outcome, c.filled) == ("done", "filled", D("0.050"))


def test_ioc_that_fills_nothing_above_the_cap_ends_as_not_filled():
    c = resting()
    c, texts = logs(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True, cum=D("0.018")),
                    Book(T0 + 100, D("62429.4"), D("62431.0"), True),
                    Amended(T0 + 100), Book(T0 + 120, D("62429.4"), D("62431.0"), True), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 121, False, D("0.018"), None), IocDone(T0 + 121))
    assert (c.phase, c.outcome, c.filled) == ("done", "notfilled", D("0.018"))
    assert texts[-1] == "IOC buy, 0.0320 BTC at 62,418.50: nothing filled. The ask is 62,431.00."


def test_remainder_below_the_minimum_stops_as_not_filled_with_no_ioc():
    c = resting()
    c, cmds = run(c, Filled(T0 + 31, D("0.04997"), D("62417.9"), True, cum=D("0.04997")), beat(c, T0 + 120),
                  Tick(T0 + 120), Canceled(T0 + 120),
                  OrderState(T0 + 120, False, D("0.04997"), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == []
    assert (c.phase, c.outcome) == ("done", "belowmin")


def test_remainder_below_costmin_also_stops():
    cheap = core.Pair("X/USD", "X", "USD", D("0.0001"), D("1"), D("5"), 4, 8, "online")
    c, _ = core.begin("oc-2", cheap, "buy", D("10"), D("1.0000"), D("1.0001"), 60, T0, core.SIM_VENUE)
    c, cmds = run(c, Placed(T0), Filled(T0 + 5, D("6"), D("1.0000"), True, cum=D("6")), beat(c, T0 + 60),
                  Tick(T0 + 60), Canceled(T0 + 60),
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
        gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))], 0.0)
        pair = core.Pair("X/USD", "X", "USD", D("0.1"), D("0.001"), D("0.01"), 1, 8, "online")
        c, cmds = core.begin("oc", pair, "buy", D("1.0"), bid, bid + D("0.1"), 30, 0.0, core.SIM_VENUE)
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
                more = []
                for ev in gw.on_book([(bid, D("1"))], [(bid + D("0.1"), D("0.3")), (bid + D("0.2"), D("5"))], now):
                    c, m = core.step(c, ev)
                    more += m
                c, m = core.step(c, Book(now, bid, bid + D("0.1"), True))
                more += m
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
    c, cmds = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True, cum=D("0.018")), UserStop(T0 + 55))
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
    c, cmds = run(c, Filled(T0 + 10, D("0.01"), D("62417.9"), True, cum=D("0.01")), beat(c, T0 + 20), UserFillNow(T0 + 20))
    assert cmds == [Cancel("oc-1")]
    c, cmds = run(c, Canceled(T0 + 20), OrderState(T0 + 20, False, D("0.01"), None))
    assert cmds == [Query("oc-1"), Ioc("oc-1-i", "buy", D("62418.5"), D("0.04"))]


# ---------- the first order refused (owner decision G21 B: wait and place it again until the timeout) ----------

def refused_first(reason="would_cross", *before, **kw):
    """The venue refuses the first post-only order at T0 + 0.2; the events in `before` come while it is in flight."""
    c, _ = start(**kw)
    c, _ = run(c, *before)
    return logs(c, Rejected(T0 + 0.2, "place", reason))


def test_a_first_order_refused_as_would_cross_is_placed_again_at_the_new_bid_after_the_wait():
    c, texts = refused_first()
    assert (c.phase, c.price, c.outcome) == ("resting", None, None)
    assert texts == ["The simulated exchange rejected the order: would cross the ask (post-only). "
                     "The tool places it again at the best bid after the wait."]
    c, cmds = run(c, Book(T0 + 3, D("62418.0"), D("62418.6"), True))
    assert cmds == []                                   # 5 s from the first order
    c, cmds = run(c, Book(T0 + 5, D("62418.1"), D("62418.6"), True))
    assert cmds == [Place("oc-1-1", "buy", D("62418.1"), D("0.05"))]
    c, cmds = run(c, Placed(T0 + 5.1), Book(T0 + 11, D("62418.3"), D("62418.6"), True))
    assert cmds == [Amend("oc-1-1", D("62418.3"))]     # the new order moves by amends, as a first order does


def test_a_sell_refused_as_would_cross_is_placed_again_at_the_new_ask():
    c, texts = refused_first(side="sell")
    assert texts[0].endswith("would cross the bid (post-only). The tool places it again at the best ask after the wait.")
    c, cmds = run(c, Book(T0 + 5, D("62417.0"), D("62417.6"), True))
    assert cmds == [Place("oc-1-1", "sell", D("62417.9"), D("0.05"))]   # never below the floor, the start bid


def test_after_a_first_order_refusal_the_new_order_never_goes_above_the_cap():
    c, _ = refused_first()
    c, cmds = run(c, Book(T0 + 5, D("62418.5"), D("62418.5"), True))    # a post-only buy at the cap would cross
    assert cmds == []
    c, cmds = run(c, Book(T0 + 6, D("62425.0"), D("62425.1"), True))    # the bid ran above the cap
    assert cmds == [Place("oc-1-1", "buy", D("62418.5"), D("0.05"))]


def test_fill_now_while_the_first_order_is_in_flight_then_refused_reads_the_fills_and_sends_one_ioc_at_the_cap():
    c, _ = start()
    c, _ = run(c, UserFillNow(T0 + 0.1))
    c, cmds = run(c, Rejected(T0 + 0.2, "place", "would_cross"))
    assert (c.phase, cmds) == ("reread", [Query("oc-1")])
    c, cmds = run(c, OrderState(T0 + 0.3, False, D(0), None, "oc-1"))
    assert (c.phase, cmds) == ("ioc", [Ioc("oc-1-i", "buy", D("62418.5"), D("0.05"))])


def test_stop_while_the_first_order_is_in_flight_then_refused_ends_as_stopped():
    c, _ = refused_first("would_cross", UserStop(T0 + 0.1))
    assert (c.phase, c.outcome) == ("done", "stopped")


def test_the_timeout_after_a_first_order_refusal_reads_the_fills_and_sends_one_ioc_at_the_cap():
    c, _ = refused_first(timeout=30)
    c, texts = logs(c, Book(T0 + 30, D("62418.5"), D("62418.5"), True), Tick(T0 + 30))   # a locked book: no post-only price
    assert texts == ["Timeout. The tool places no new order and fills the rest with one IOC."]
    assert c.phase == "reread"
    c, cmds = run(c, OrderState(T0 + 30.1, False, D(0), None, "oc-1"))
    assert cmds == [Ioc("oc-1-i", "buy", D("62418.5"), D("0.05"))]


def test_the_timeout_after_a_first_order_refusal_with_the_feed_lost_promises_no_ioc():
    # TIMEOUT-FEED-LOST-CLAIMS-IOC: no order rests and no valid price: no IOC, and the log says so.
    c, _ = refused_first(timeout=30)
    c, texts = logs(c, FeedLost(T0 + 1), Tick(T0 + 30))
    assert texts[-1] == "Timeout. The tool places no new order."
    c, texts = logs(c, OrderState(T0 + 30.1, False, D(0), None, "oc-1"))
    assert (c.outcome, texts[-1]) == ("notfilled", "No IOC: the price feed is lost, so there is no valid price. The rest counts as not filled.")


def test_stop_or_fill_now_with_no_order_resting_says_there_is_nothing_to_cancel():
    # STOPPED-COPY-CLAIMS-CANCEL: after a refused first order no order rests.
    c, _ = refused_first()
    c, texts = logs(c, UserStop(T0 + 1))
    assert (c.outcome, texts) == ("stopped", ["You pressed Stop. No order rests, so there is nothing to cancel."])
    c, _ = refused_first()
    c, texts = logs(c, UserFillNow(T0 + 1))
    assert texts == ['You pressed "Fill the rest now". No order rests, so there is nothing to cancel.']
    c, _ = start()                                      # the first order in flight: a cancel only if it rests
    c, texts = logs(c, UserStop(T0 + 0.1))
    assert texts == ["You pressed Stop. The tool cancels the order if the exchange places it."]
    c, texts = logs(resting(), UserStop(T0 + 1))        # an order rests: the tool cancels it
    assert texts == ["You pressed Stop. Cancelling the order."]


def test_a_first_order_refused_for_the_rate_limit_waits_15_s_and_for_room_on_the_counter():
    c, texts = refused_first("rate_limit")
    assert (c.phase, c.rate, c.slow) == ("resting", 60.0, True)
    assert texts == ["The simulated exchange rejected the order: EOrder:Rate limit exceeded. "
                     "The tool places it again at the best bid after the wait."]
    placed = []
    for s in range(1, 20):
        c, cmds = run(c, Book(T0 + s, D("62417.9"), D("62418.5"), True))
        placed += [(s, x.id) for x in cmds if isinstance(x, Place)]
    assert placed == [(15, "oc-1-1")]


def test_a_first_order_refused_with_another_reason_ends_the_chase_with_the_venues_text():
    c, texts = refused_first("EOrder:Insufficient funds")
    assert (c.phase, c.outcome) == ("done", "refused")
    assert texts == ["The simulated exchange rejected the order: EOrder:Insufficient funds."]
    c, _ = refused_first()                              # no order ever rested: still "refused" after a would-cross
    c, _ = run(c, Book(T0 + 5, D("62417.9"), D("62418.5"), True), Rejected(T0 + 5.1, "place", "EOrder:Insufficient funds"))
    assert (c.phase, c.outcome, c.legs) == ("done", "refused", ("oc-1", "oc-1-1"))


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
    c, cmds = run(c, beat(c, T0 + 120), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-i", "sell", D("62417.9"), D("0.05"))]


def test_sell_saving_counts_money_received():
    c = resting(side="sell")
    c, _ = run(c, Filled(T0 + 5, D("0.05"), D("62418.5"), True, cum=D("0.05")))
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
    c, cmds = run(c, Book(T0 + 60, D("62418.2"), D("62418.5"), True), Tick(T0 + 100))
    assert cmds == [] and c.phase == "feed_lost"   # no amend without the feed
    c, cmds = run(c, FeedBack(T0 + 101))
    assert cmds == [Query("oc-1")] and c.phase == "reconcile"
    c, cmds = run(c, beat(c, T0 + 101), OrderState(T0 + 101, True, D(0), D("62417.9")))
    assert cmds == [Amend("oc-1", D("62418.2"))]   # the book seen during the outage is not used...


def test_stop_works_while_the_feed_is_lost():
    c = resting()
    c, cmds = run(c, FeedLost(T0 + 5), UserStop(T0 + 6))
    assert cmds == [Cancel("oc-1")]


# ---------- restart ----------

def test_restart_ends_an_unfinished_dry_run_and_does_not_resume_it():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62417.9"), True, cum=D("0.018")))
    c, texts = logs(c, Restarted(T0 + 70, T0 + 62))
    assert (c.phase, c.outcome) == ("done", "ended")
    assert texts == ["Tool started again after 8 s off. Ended the simulated order. No order was on Kraken.",
                     "Recorded the simulated fills: 0.0180 of 0.0500 BTC.", "Did not continue the dry run."]
    c, cmds = run(c, Tick(T0 + 200), Book(T0 + 201, D("62418.1"), D("62418.5"), True))
    assert cmds == []


# ---------- result numbers ----------

def test_saving_against_a_market_order_at_the_start_matches_the_design_sample():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True, cum=D("0.018")),
               Filled(T0 + 78, D("0.032"), D("62418.3"), True, cum=D("0.050")))
    s = core.summary(c)
    assert c.outcome == "filled"
    assert round(s["avg"], 2) == D("62418.23")
    assert round(s["fee"], 2) == D("12.48")
    assert round(s["market"], 2) == D("3145.89")
    assert round(s["saving"], 2) == D("12.50")


def test_maker_saving_is_not_below_zero_with_the_default_cap():
    # Maker fills at the cap, the worst maker price, still save the fee difference.
    c = resting()
    c, _ = run(c, Filled(T0 + 5, D("0.05"), D("62418.5"), True, cum=D("0.05")))
    assert core.summary(c)["saving_maker"] == D("12.4837000")


def test_worst_case_with_a_higher_limit():
    assert round(core.worst_case("buy", D("0.05"), D("62480.00")), 2) == D("3148.99")
    assert round(core.worst_case("buy", D("0.05"), D("62418.5")), 2) == D("3145.89")


def test_state_survives_json():
    c = resting()
    c, _ = run(c, Filled(T0 + 31, D("0.018"), D("62418.1"), True, cum=D("0.018")))
    assert core.from_json(core.to_json(c)) == c


# ---------- repair round 1 (R13 code review) ----------

def test_sim_fills_a_resting_order_as_maker_when_the_book_crosses_it():
    # SIM-CROSSED-NO-FILL: real Kraken fills a resting post-only order at once, at our price,
    # when the opposite side comes to or through it.
    gw = SimGateway()
    gw.on_book([(D("100.0"), D("1"))], [(D("100.2"), D("1"))], T0)
    assert gw.send(Place("oc-1", "buy", D("100.1"), D("0.05")), T0) == [Placed(T0)]
    crossed = [(D("100.0"), D("0.02")), (D("100.1"), D("0.01")), (D("100.2"), D("5"))]
    assert gw.on_book([(D("99.9"), D("1"))], crossed, T0 + 1) == [
        Filled(T0 + 1, D("0.03"), D("100.1"), True, "chase", D("0.03"), "oc-1")]
    # The same public levels do not fill twice.
    assert gw.on_book([(D("99.9"), D("1"))], crossed, T0 + 2) == []
    # New size at a crossing level fills, up to the rest of the order.
    more = [(D("100.0"), D("0.10")), (D("100.1"), D("0.01")), (D("100.2"), D("5"))]
    assert gw.on_book([(D("99.9"), D("1"))], more, T0 + 3) == [
        Filled(T0 + 3, D("0.02"), D("100.1"), True, "chase", D("0.05"), "oc-1")]
    assert gw.send(Query("oc-1"), T0 + 3) == [OrderState(T0 + 3, False, D("0.05"), D("100.1"), "oc-1")]


def test_sim_does_not_refill_from_a_level_that_flickers_out_and_back():
    # Seen in a live dry run: an ask that leaves and comes back at our price filled the same size again and again.
    gw = SimGateway()
    gw.on_book([(D("100.0"), D("1"))], [(D("100.2"), D("1"))], T0)
    gw.send(Place("oc-1", "buy", D("100.1"), D("1")), T0)
    at_our_price = [(D("100.1"), D("0.3")), (D("100.2"), D("5"))]
    away = [(D("100.2"), D("5"))]
    assert gw.on_book([(D("100.0"), D("1"))], at_our_price, T0 + 1) == [
        Filled(T0 + 1, D("0.3"), D("100.1"), True, "chase", D("0.3"), "oc-1")]
    assert gw.on_book([(D("100.0"), D("1"))], away, T0 + 2) == []
    assert gw.on_book([(D("100.0"), D("1"))], at_our_price, T0 + 3) == []


def test_sim_fills_a_resting_sell_when_the_bid_rises_through_it():
    gw = SimGateway()
    gw.on_book([(D("99.8"), D("1"))], [(D("100.0"), D("1"))], T0)
    gw.send(Place("oc-1", "sell", D("99.9"), D("0.05")), T0)
    assert gw.on_book([(D("99.9"), D("0.01")), (D("99.8"), D("1"))], [(D("100.0"), D("1"))], T0 + 1) == [
        Filled(T0 + 1, D("0.01"), D("99.9"), True, "chase", D("0.01"), "oc-1")]


def test_a_cancel_reject_with_the_order_still_open_retries_then_ends_and_says_check_kraken():
    # CANCEL-REJECT-STUCK
    c = resting()
    c, cmds = run(c, UserStop(T0 + 20))
    assert cmds == [Cancel("oc-1")]
    for attempt in (2, 3):
        c, cmds = run(c, Rejected(T0 + 20, "cancel", "EGeneral:Temporary lockout"))
        assert cmds == [Query("oc-1")]
        c, cmds = run(c, OrderState(T0 + 20, True, D(0), D("62417.9")))
        assert cmds == [Cancel("oc-1")], attempt
    c, cmds = run(c, Rejected(T0 + 21, "cancel", "EGeneral:Temporary lockout"))
    c, texts = logs(c, OrderState(T0 + 21, True, D(0), D("62417.9")))
    assert (c.phase, c.outcome) == ("done", "cancelfail")
    assert texts == ["The simulated exchange rejected the cancel 3 times and the order is still open. The tool stopped "
                     "the chase. Nothing to check in Kraken Pro: a dry run sends no orders."]


def test_a_cancel_retry_waits_for_room_on_the_rate_counter():
    c = resting(rate=40)                  # begin adds 1, the cancel at age 1 adds 8: 49
    c, cmds = run(c, UserStop(T0 + 1), Rejected(T0 + 1, "cancel", "x"))
    c = core.replace(c, rate=59.0, rate_at=T0 + 1)
    c, cmds = run(c, OrderState(T0 + 1, True, D(0), D("62417.9")))
    assert cmds == []                     # 59 + 8 is above 60: wait
    c, cmds = run(c, Tick(T0 + 2))
    assert cmds == []                     # 58 + 8: still above 60
    c, cmds = run(c, Tick(T0 + 7))
    assert cmds == [Cancel("oc-1")]       # 53 + 6 (age 7) = 59


def test_the_outcome_and_the_summary_use_the_venue_filled_quantity():
    # OUTCOME-VS-VENUE-QTY: the venue reports 0.4 filled that the tool never saw; the IOC fills 0.6.
    c = resting(qty="1.0")
    c, texts = logs(c, beat(c, T0 + 120), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D("0.4"), D("62417.9")))
    assert "The simulated exchange reports 0.4000 BTC more filled than the tool saw. Recorded it at 62,417.90 (maker)." in texts
    c, cmds = run(c, Filled(T0 + 121, D("0.6"), D("62418.5"), False, "ioc"), IocDone(T0 + 121))
    assert (c.phase, c.outcome, c.filled) == ("done", "filled", D("1.0"))
    assert core.summary(c)["maker_qty"] == D("0.4")


def test_a_late_fill_report_after_the_reread_is_not_counted_twice():
    c = resting()
    c, cmds = run(c, beat(c, T0 + 120), Tick(T0 + 120), Canceled(T0 + 120), OrderState(T0 + 120, False, D("0.01"), D("62417.9")))
    assert [x.qty for x in cmds if isinstance(x, Ioc)] == [D("0.04")]
    # The fill report of the same 0.01 arrives after the reread: the venue total stays 0.01.
    c, _ = run(c, Filled(T0 + 120.5, D("0.01"), D("62417.9"), True, "chase", D("0.01")))
    assert c.filled == D("0.01")


def test_the_timeout_runs_while_the_feed_is_lost_and_ends_with_no_ioc():
    # TIMEOUT-FROZEN-FEED-LOST
    c = resting(timeout=30)
    c, cmds = run(c, FeedLost(T0 + 10), Tick(T0 + 29))
    assert cmds == [] and c.phase == "feed_lost"
    c, texts = logs(c, Tick(T0 + 30))
    assert c.phase == "cancelling"
    assert texts == ["Timeout while the price feed is lost. Cancelling the order."]
    c, cmds = run(c, Canceled(T0 + 30))
    c, texts = logs(c, OrderState(T0 + 30, False, D(0), D("62417.9")))
    assert [x for x in cmds if isinstance(x, Ioc)] == []
    assert (c.phase, c.outcome, c.end_ask) == ("done", "notfilled", None)
    assert texts[-1] == "No IOC: the price feed is lost, so there is no valid price. The rest counts as not filled."


def test_a_limit_must_be_above_0_and_a_multiple_of_the_tick_size():
    # SELL-FLOOR-NO-BOUND and LIMIT-NOT-POSITIVE
    v = lambda side, lim, pair=BTC: core.validate(pair, side, D("0.05"), D(lim), D("62417.9"), D("62418.5"), True, 120)
    assert v("sell", "0") == ["The floor must be above 0."]
    assert v("sell", "-1") == ["The floor must be above 0."]
    assert v("buy", "0") == ["The cap must be above 0."]
    assert v("sell", "62417.85") == ["The limit must be a multiple of 0.1."]
    half = core.Pair(**{**BTC.__dict__, "tick": D("0.5")})
    assert v("sell", "62417.3", half) == ["The limit must be a multiple of 0.5."]
    assert v("sell", "62417.5", half) == []
    assert v("sell", "62000", BTC) == []


def test_validate_answers_a_huge_amount_with_an_error_not_an_exception():
    # HUGE-QTY-500 (core part)
    errs = core.validate(BTC, "buy", D("1e30"), None, D("62417.9"), D("62418.5"), True, 120)
    assert errs == []  # many decimals are the problem, not size: the server bounds the size
    assert core.validate(BTC, "buy", D("0.123456789"), None, D("62417.9"), D("62418.5"), True, 120) == [
        "Enter an amount above 0 with at most 8 decimals."]
    assert core.validate(BTC, "buy", D("0.050000000"), None, D("62417.9"), D("62418.5"), True, 120) == []


# ---------- repair round 2 (R17 code review, R20 QA) ----------

def test_a_cancel_rejected_for_rate_limit_waits_for_the_counter_before_it_tries_again():
    # CANCEL-RETRY-IGNORES-RATE-LIMIT: the 3 tries must not all go out in one second into the same limit.
    c = resting()
    c, cmds = run(c, UserStop(T0 + 100))
    assert cmds == [Cancel("oc-1")]
    c, cmds = run(c, Rejected(T0 + 100.2, "cancel", "rate_limit"))
    assert cmds == [Query("oc-1")] and c.rate == 60
    c, cmds = run(c, OrderState(T0 + 100.3, True, D(0), D("62417.9")), Tick(T0 + 101.1))
    assert cmds == []                     # 60 - 0.9 + 1 (a cancel at age 101) is above 60: wait
    c, cmds = run(c, Tick(T0 + 101.3))
    assert cmds == [Cancel("oc-1")] and c.phase == "cancelling"


def test_a_chase_fill_without_the_venue_total_is_logged_and_not_counted():
    # FILLED-WITHOUT-CUM-DOUBLE-COUNT: the reread counted 0.4; a late report of the same 0.4 has no cum.
    c = resting(qty="1.0")
    c, _ = run(c, beat(c, T0 + 30), Tick(T0 + 30), Canceled(T0 + 30.1), OrderState(T0 + 30.2, False, D("0.4"), D("62417.9")))
    c, texts = logs(c, Filled(T0 + 30.3, D("0.4"), D("62417.9"), True, "chase"))
    assert c.filled == D("0.4")
    assert texts == ["The simulated exchange reported a fill of 0.4000 BTC with no filled total. The tool did not count it. "
                     "It counts the filled total that the venue reports."]
    c, _ = run(c, Filled(T0 + 30.4, D("0.6"), D("62418.5"), False, "ioc"))   # an IOC fill counts its qty
    assert c.filled == D("1.0")


def test_no_ioc_when_the_feed_is_back_but_the_book_is_not_valid_yet():
    # FEEDBACK-BEFORE-REREAD-IOC: timeout while the feed is lost, the feed returns before the reread answer.
    c = resting(timeout=30)
    c, _ = run(c, FeedLost(T0 + 10), Tick(T0 + 30))
    c, cmds = run(c, FeedBack(T0 + 30.1), Canceled(T0 + 30.2), OrderState(T0 + 30.3, False, D(0), D("62417.9")))
    assert [x for x in cmds if isinstance(x, Ioc)] == []
    assert (c.phase, c.outcome, c.end_ask) == ("done", "notfilled", None)


def test_no_amend_on_a_book_older_than_10_seconds():
    # STALE-FEED-FREEZE-AFTER-20S: the bid rose at T0 + 2; no message came after it.
    c = resting()
    c, _ = run(c, Book(T0 + 2, D("62418.1"), D("62418.5"), True))
    _, cmds = run(c, Tick(T0 + 12.5))
    assert cmds == []                                   # the book is 10.5 s old
    _, cmds = run(c, Tick(T0 + 11.5))
    assert cmds == [Amend("oc-1", D("62418.1"))]        # 9.5 s old: still valid


def test_no_ioc_on_a_book_older_than_10_seconds():
    c = resting()
    c, _ = run(c, Book(T0 + 105, c.bid, c.ask, True), Tick(T0 + 120), Canceled(T0 + 120))
    stale, cmds = run(c, OrderState(T0 + 120, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [] and (stale.outcome, stale.end_ask) == ("notfilled", None)
    _, cmds = run(c, beat(c, T0 + 119), OrderState(T0 + 120, False, D(0), None))
    assert [x for x in cmds if isinstance(x, Ioc)] == [Ioc("oc-1-i", "buy", D("62418.5"), D("0.05"))]


# ---------- SIM-FILL-DOUBLE-COUNT-SUSPECT ----------

def test_sim_a_sell_print_takes_bid_liquidity_so_the_ask_side_never_fills_it_again():
    # A sell print at X consumes a bid at X. Kraken then lowers that bid, not an ask. A buy fills from sell
    # prints below its price and from asks at or below it, so one quantity cannot fill it twice.
    gw = SimGateway()
    gw.on_book([(D("99.9"), D("0.3"))], [(D("100.1"), D("1"))], T0)
    gw.send(Place("oc-1", "buy", D("100.0"), D("1")), T0)
    assert gw.on_trade("sell", D("99.9"), D("0.3"), T0 + 1) == [Filled(T0 + 1, D("0.3"), D("100.0"), True, "chase", D("0.3"), "oc-1")]
    assert gw.on_book([], [(D("100.1"), D("1"))], T0 + 1) == []     # the bid at 99.9 is gone: nothing more
    # A seller whose remainder rests as an ask at 99.9 is new liquidity: it fills once, the print does not repeat.
    assert gw.on_book([], [(D("99.9"), D("0.2")), (D("100.1"), D("1"))], T0 + 2) == [
        Filled(T0 + 2, D("0.2"), D("100.0"), True, "chase", D("0.5"), "oc-1")]
    assert gw.on_book([], [(D("99.9"), D("0.2")), (D("100.1"), D("1"))], T0 + 3) == []


def replay_recorded_feed(price, qty):
    """Replay tests/fixtures/kraken-btcusd.jsonl (public Kraken BTC/USD) through the dry-run gateway, with a
    resting buy placed after the snapshot. Return the fills as (line index, qty, price, maker)."""
    import json
    from pathlib import Path

    from order_chaser.book import OrderBook
    gw, book, fills = SimGateway(), OrderBook(1, 8), []
    for i, raw in enumerate((Path(__file__).parent / "fixtures" / "kraken-btcusd.jsonl").open()):
        m = json.loads(raw, parse_float=D)
        if m["channel"] == "book":
            assert book.apply(m["data"][0], m["type"] == "snapshot")
            if i == 0:
                assert gw.send(core.Place("oc-1", "buy", D(price), D(qty)), 0) == [core.Placed(0)]
            got = gw.on_book(book.top_bids(), book.top_asks(), i)
        elif m["channel"] == "trade":
            got = [f for t in m["data"] for f in gw.on_trade(t["side"], D(str(t["price"])), D(str(t["qty"])), i)]
        else:
            got = []
        fills += [(i, str(f.qty), str(f.price), f.maker) for f in got]
    return fills


def test_recorded_kraken_feed_a_sell_print_at_the_order_price_does_not_fill_it():
    # Lines 132 and 141 of the recording print sells at 85311.5: at the price of the buy, not through it, so
    # they lower the bid and fill nothing. The order fills only when the ask comes down to it (line 146).
    assert replay_recorded_feed("85311.5", "0.05") == [(146, "0.05", "85311.5", True)]


def test_recorded_kraken_feed_fills_a_buy_above_the_bid_level_by_level_up_to_its_amount():
    assert replay_recorded_feed("85311.6", "1") == [
        (90, "0.18295300", "85311.6", True), (94, "0.12228273", "85311.6", True), (98, "0.11364277", "85311.6", True),
        (103, "0.03722318", "85311.6", True), (104, "0.12556389", "85311.6", True), (107, "0.41833443", "85311.6", True)]


def test_one_name_for_the_dry_run_venue_in_every_page_and_log():
    # UX-WORD-SIMULATION: "the simulated exchange", never "the simulation".
    from pathlib import Path
    src = Path(core.__file__).parent
    files = [*sorted((src / "static").glob("*.*")), src / "core.py", src / "server.py"]
    assert [f.name for f in files if "the simulation" in f.read_text()] == []
    assert [f.name for f in files if "simulated exchange" in f.read_text()] == ["app.js", "chase.html", "new.html", "result.html", "core.py"]
