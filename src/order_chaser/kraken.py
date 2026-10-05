"""The live side of Kraken (T4): the private WebSocket v2 executions feed (U4) and the Kraken gateway (U5).

The executions channel (wss://ws-auth.kraken.com/v2) reports each change of our orders. The token comes from REST
GetWebSocketsToken. Each (re)subscribe asks for snap_orders and snap_trades, so a link that comes back first gives
the open orders and the last trades. exec_events() maps the reports of one chase to the core's events.

Nothing here reads the key: the REST client has it.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import time
from decimal import Decimal

import httpx
import websockets

from . import core
from .keys import NoKey
from .rest import KrakenError, KrakenRest

WS_AUTH = "wss://ws-auth.kraken.com/v2"
STABLE = 10           # s: the backoff starts again only after a link stayed up this long with no refusal
REQUEST_WAIT = 5      # s: the answer to a request on the link (amend_order)
OPEN_STATES = ("pending", "pending_new", "open", "new", "partially_filled")   # REST and WS v2 order states
log = logging.getLogger("order_chaser")


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def exec_events(c: core.Chase, kind: str, items: list, now: float, asked: set[str] = frozenset()) -> list:
    """The executions reports of the orders of chase c -> the core's events. Reports of other orders are left out.
    - a fill (it has last_qty): Filled with cum_qty, the venue's filled qty of that leg (the core counts it once);
    - an open order of a snapshot: OrderState(open) with its cum (a fill in the gap counts);
    - a cancel or an expiry of a chase leg that the tool did not ask for (asked: the legs it cancelled):
      OrderState(closed). The IOC's cancel of its rest is normal: its fills and IocDone come from the gateway."""
    out = []
    for x in items:
        leg = x.get("cl_ord_id")
        if leg not in c.legs and leg != c.ioc_id:
            continue
        cum = _dec(x.get("cum_qty")) or Decimal(0)
        if x.get("last_qty") is not None:
            out.append(core.Filled(now, _dec(x["last_qty"]), _dec(x["last_price"]), x.get("liquidity_ind") == "m",
                                   "ioc" if leg == c.ioc_id else "chase", cum, leg))
        elif leg == c.ioc_id:
            continue
        elif kind == "snapshot" and x.get("order_status") in OPEN_STATES:
            out.append(core.OrderState(now, True, cum, _dec(x.get("limit_price")), leg))
        elif x.get("exec_type") in ("canceled", "expired") and leg not in asked:
            out.append(core.OrderState(now, False, cum, _dec(x.get("limit_price")), leg))
    return out


class PrivateFeed:
    """One authenticated WebSocket v2 link, subscribed to the executions channel (snap_orders, snap_trades).

    token(): an async call that returns a new token (REST GetWebSocketsToken). on_exec(kind, items): kind is
    "snapshot" or "update". on_link(up, attempt, next_in). The backoff (2, 4, 8, 15 s) starts again only after a
    link stayed up STABLE s and Kraken refused nothing on it."""

    def __init__(self, token, on_exec, on_link, url: str = WS_AUTH, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.token, self.on_exec, self.on_link, self.url, self.clock, self.sleep = token, on_exec, on_link, url, clock, sleep
        self.ws = None          # set while the subscription is up
        self.tok: str | None = None
        self.attempt = 0
        self.req = 0
        self.waiting: dict[int, asyncio.Future] = {}

    async def run(self) -> None:
        while True:
            up_at, refused = None, False
            try:
                self.tok = await self.token()
                async with websockets.connect(self.url, open_timeout=10, ping_interval=20, close_timeout=1) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "params": {
                        "channel": "executions", "token": self.tok, "snap_orders": True, "snap_trades": True}}))
                    while True:   # no message (heartbeats count) for STALE_AFTER s: the link is lost
                        m = json.loads(await asyncio.wait_for(ws.recv(), core.STALE_AFTER), parse_float=Decimal)
                        if m.get("method") == "subscribe" and m.get("success") is False:
                            refused = True
                            raise ConnectionError(f"Kraken refused the subscription: {m.get('error')}")
                        if m.get("method") == "subscribe":
                            self.ws, up_at = ws, self.clock()
                            self.on_link(True, self.attempt, 0)
                        elif m.get("req_id") in self.waiting:
                            self.waiting.pop(m["req_id"]).set_result(m)
                        elif m.get("channel") == "executions":
                            self.on_exec(m.get("type"), m.get("data", []))
            except asyncio.CancelledError:
                self.ws = None
                raise
            except Exception:
                pass
            self.ws = None
            for f in self.waiting.values():
                f.cancel()
            self.waiting.clear()
            if up_at is not None and not refused and self.clock() - up_at >= STABLE:
                self.attempt = 0
            self.attempt += 1
            wait = min(2 ** min(self.attempt, 4), 15)
            self.on_link(False, self.attempt, wait)
            await self.sleep(wait)

    async def request(self, method: str, params: dict) -> dict:
        """One request on the link (amend_order) and its answer. Raises ConnectionError when the link is down or
        does not answer in REQUEST_WAIT s."""
        if self.ws is None:
            raise ConnectionError("the private feed is lost")
        self.req += 1
        req = self.req
        fut = asyncio.get_running_loop().create_future()
        self.waiting[req] = fut
        try:
            await self.ws.send(json.dumps({"method": method, "params": {**params, "token": self.tok}, "req_id": req},
                                          default=float))
            return await asyncio.wait_for(fut, REQUEST_WAIT)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise                                   # the caller was cancelled
            raise ConnectionError("the private feed was lost before the answer") from None   # run() dropped it
        except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
            raise ConnectionError("no answer on the private feed") from e
        finally:
            self.waiting.pop(req, None)


# ---------- the Kraken gateway (U5) ----------

TIMER = 60            # s: the safety timer, CancelAllOrdersAfter
RENEW_EVERY = 20      # s
RETRY_EVERY = 2       # s: a failed renewal, or a REST read with no answer, tries again
NO_ANSWER = 60        # s: a read with no answer for this long ends the chase (core.NoAnswer); the timer stays
FEED_WAIT = 10        # s: the private feed must be up before the first order


PROTECT = ("stop-loss", "take-profit", "trailing-stop")   # Kraken ordertypes that protect a position (Q7, C8)


def others(orders: dict) -> list[dict]:
    """Orders of the account (Kraken OpenOrders or ClosedOrders), stop-loss and take-profit orders first (Q7, C8)."""
    rows = [{"id": k, "pair": o.get("descr", {}).get("pair", ""), "type": o.get("descr", {}).get("ordertype", ""),
             "text": o.get("descr", {}).get("order", ""), "cl_ord_id": o.get("cl_ord_id")} for k, o in orders.items()]
    for r in rows:
        r["protect"] = r["type"].startswith(PROTECT)
    return sorted(rows, key=lambda r: not r["protect"])


def _reason(errors: list[str]) -> str:
    """Kraken's error text -> the core's reason of a refusal."""
    text = ", ".join(errors)
    if "Post only" in text:
        return "would_cross"
    if "Rate limit" in text:
        return "rate_limit"
    if "Unknown order" in text:
        return "not_open"
    return text


class KrakenGateway:
    """The live gateway: the core's commands to Kraken, Kraken's answers and reports back as the core's events.

    - Place, Ioc (and their margin forms): REST AddOrder with our cl_ord_id; post-only for the chase, IOC at the cap;
      leverage and reduce_only for margin. The IOC is read back at once (QueryOrders): Filled(cum), then IocDone.
    - Amend: WebSocket v2 amend_order by cl_ord_id. A refusal goes to the core, which decides (margin: cancel and
      replace). Cancel: REST CancelOrder. Query: REST QueryOrders; "unknown order" is OrderState(open False, cum 0).
    - The executions feed: exec_events(); a cancel of the current leg that the tool did not ask for also gives
      VenueCanceled ("timer" when the safety timer was due).
    - The safety timer: CancelAllOrdersAfter 60 s before the first order (no order rests without it), renewed every
      20 s, and 0 at the end. caffeinate -i keeps the Mac awake while the chase runs.
    on_event(ev) and on_link(up) go to the engine; the engine keeps .chase up to date."""

    def __init__(self, rest: KrakenRest, read_key, ws_url: str = WS_AUTH, clock=time.time,
                 awake_cmd: list[str] | None = None) -> None:
        self.rest, self.read_key, self.ws_url, self.clock = rest, read_key, ws_url, clock
        self.awake_cmd = ["caffeinate", "-i"] if awake_cmd is None else awake_cmd
        self.on_event = lambda ev: None
        self.on_link = lambda up: None
        self.on_txid = lambda leg, txid: None   # Kraken's order id of a leg (the result page shows it)
        self.chase: core.Chase | None = None
        self.feed: PrivateFeed | None = None
        self.task: asyncio.Task | None = None
        self.awake: subprocess.Popen | None = None
        self.cancels: set[str] = set()    # the legs that the tool asked to cancel
        self.timer = {"on": False, "renewed_at": None, "deadline": None, "tried_at": None}
        self.renewing = False
        self.bg: set[asyncio.Task] = set()   # the read of the orders that the timer cancelled (C8)

    async def open_key(self) -> None:
        """The key, from memory or from the Keychain (Q9: macOS asks one time at each start of the tool)."""
        if self.rest.key is None:
            self.rest.key = await asyncio.to_thread(self.read_key)
        if self.rest.key is None:
            raise NoKey()

    # ----- the life of a live chase -----
    async def open(self) -> None:
        """Before the first order: the private feed (up within FEED_WAIT s), the safety timer and caffeinate.
        Raises (ConnectionError, KrakenError, httpx.HTTPError) when one of them fails: the caller sends no order."""
        self.cancels = set()
        self.timer = {"on": False, "renewed_at": None, "deadline": None, "tried_at": None}
        self.feed = PrivateFeed(self._token, self._on_exec, self._link, self.ws_url)
        self.task = asyncio.create_task(self.feed.run())
        end = time.monotonic() + FEED_WAIT
        while self.feed.ws is None:
            if time.monotonic() > end:
                raise ConnectionError("the private feed of the fills did not connect")
            await asyncio.sleep(0.05)
        await self._set_timer(TIMER)
        try:
            self.awake = subprocess.Popen([*self.awake_cmd, "-w", str(os.getpid())], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError:   # no caffeinate (not macOS): the chase runs, the Mac can sleep
            log.warning("caffeinate did not start: the Mac can sleep during the chase.")

    async def close(self, keep_timer: bool = False) -> None:
        """The chase ended (or did not start): the Mac can sleep, no feed, timer 0 (no cancel-all later).
        keep_timer: Kraken did not answer, so an order can still rest: no renewal, no 0; the timer cancels it."""
        if self.awake is not None:
            self.awake.terminate()
            self.awake.wait()
            self.awake = None
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
            self.task = self.feed = None
        if self.timer["on"]:
            self.timer["on"] = False
            if keep_timer:
                return
            try:
                await self.rest.call("CancelAllOrdersAfter", timeout=0)
            except (KrakenError, httpx.HTTPError) as e:   # Kraken cancels all orders when the 60 s end
                log.warning(f"The safety timer was not set to 0: {e}")

    async def _token(self) -> str:
        return (await self.rest.call("GetWebSocketsToken"))["token"]

    def _link(self, up: bool, attempt: int, next_in: float) -> None:
        self.on_link(up)

    # ----- the safety timer -----
    async def _set_timer(self, seconds: int) -> None:
        now = self.clock()
        self.timer["tried_at"] = now
        await self.rest.call("CancelAllOrdersAfter", timeout=seconds)
        self.timer.update(on=True, renewed_at=now, deadline=now + seconds)

    def tick(self, now: float) -> None:
        """Renew the timer every 20 s; after a failure, every 2 s until it works."""
        t = self.timer
        if not t["on"] or self.renewing or now - t["renewed_at"] < RENEW_EVERY or now - t["tried_at"] < RETRY_EVERY:
            return
        self.renewing = True

        async def renew():
            try:
                await self._set_timer(TIMER)
            except (KrakenError, httpx.HTTPError) as e:
                log.warning(f"The safety timer was not renewed: {e}")
            finally:
                self.renewing = False
        asyncio.get_running_loop().create_task(renew())

    def _venue_canceled(self, reason: str) -> core.VenueCanceled:
        """Kraken cancelled the order without the tool: after the timer's deadline, that is the safety timer."""
        now = self.clock()
        if self.timer["deadline"] is not None and now >= self.timer["deadline"]:
            self.timer["on"] = False
            task = asyncio.get_running_loop().create_task(self._after_timer(self.chase, self.timer["deadline"] - 5, now + 5))
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)
            return core.VenueCanceled(now, "timer")
        return core.VenueCanceled(now, reason)

    async def cancelled_by_timer(self, c: core.Chase, since: float, until: float) -> tuple[tuple[str, str, str], ...]:
        """C8: the other orders of the account that Kraken cancelled from since to until (ClosedOrders), stop-loss
        and take-profit orders first. The tool never places them again (G19 P3)."""
        ours = {*c.legs, c.ioc_id}
        got = (await self.rest.call("ClosedOrders", start=int(since)))
        rows = {k: o for k, o in got.get("closed", {}).items() if o.get("status") == "canceled"
                and o.get("cl_ord_id") not in ours and since <= float(o.get("closetm") or 0) <= until}
        return tuple((r["type"], r["pair"], r["text"]) for r in others(rows))

    async def _after_timer(self, c: core.Chase, since: float, until: float) -> None:
        start = self.clock()
        while True:
            try:
                got = await self.cancelled_by_timer(c, since, until)
                break
            except (httpx.HTTPError, KrakenError):
                if self.clock() - start >= NO_ANSWER:
                    got = None
                    break
                await asyncio.sleep(RETRY_EVERY)
        self.on_event(core.OthersCancelled(self.clock(), c.id, got))

    async def reconcile(self, c: core.Chase) -> list:
        """The restart reconcile (Q5) of a live chase of the last stop: read each leg by cl_ord_id (one try each) and
        cancel a leg that is still open. Returns the OrderState of each leg (after a cancel), then core.Reconciled.
        Raises httpx.HTTPError or KrakenError when Kraken cannot be read: the caller tries again later."""
        out, found = [], "closed"
        for leg in (*c.legs, c.ioc_id):
            st, info = await self._read(leg, wait=0)
            if st.open:
                await self.rest.call("CancelOrder", cl_ord_id=leg)
                st, info = await self._read(leg, wait=0)
                found = "open"
            elif found == "closed" and info.get("status") == "canceled" and float(info.get("closetm") or 0) >= (c.off_from or 0):
                found = "timer"      # cancelled after the tool stopped, not by the tool: the safety timer
            if info:
                out.append(st)
        gone = await self.cancelled_by_timer(c, c.off_from, c.off_from + TIMER + 5) if found == "timer" else None
        return out + [core.Reconciled(self.clock(), found, gone)]

    # ----- commands -----
    async def send(self, cmd, now: float) -> list:
        """The events of one command. Never raises for Kraken: a read that gets no answer for NO_ANSWER s (or an
        error) gives core.NoAnswer, so that the chase ends in a clear state and the queue goes on."""
        try:
            try:
                if isinstance(cmd, core.Ioc):
                    return await self._ioc(cmd)
                if isinstance(cmd, core.Place):
                    return await self._place(cmd)
                if isinstance(cmd, core.Amend):
                    return await self._amend(cmd)
                if isinstance(cmd, core.Cancel):
                    return await self._cancel(cmd)
                if isinstance(cmd, core.Query):
                    return await self._query(cmd.id)
            except httpx.HTTPError:   # no answer to an order call: did the order reach Kraken? Read it.
                if isinstance(cmd, core.Ioc):
                    return await self._ioc_read(cmd.id)
                if isinstance(cmd, core.Place):
                    st, _ = await self._read(cmd.id)
                    return [core.Placed(self.clock())] if st.open else [core.Rejected(self.clock(), "place", "Kraken did not answer")]
                if isinstance(cmd, core.Cancel):
                    return [core.Rejected(self.clock(), "cancel", "Kraken did not answer")]   # the core reads the order
                raise                 # a Query: _read already tried for NO_ANSWER s
        except httpx.HTTPError:
            return [core.NoAnswer(self.clock(), "")]
        except KrakenError as e:
            return [core.NoAnswer(self.clock(), str(e))]
        raise TypeError(cmd)

    def _order(self, cmd, ioc: bool) -> dict:
        pair = self.chase.pair
        p = {"ordertype": "limit", "type": cmd.side, "volume": str(cmd.qty), "price": str(cmd.price),
             "pair": pair.rest or pair.symbol.replace("/", ""), "cl_ord_id": cmd.id}
        p.update({"timeinforce": "IOC"} if ioc else {"oflags": "post"})
        if isinstance(cmd, (core.MarginPlace, core.MarginIoc)):
            p["leverage"] = str(cmd.leverage)
            if cmd.reduce_only:
                p["reduce_only"] = "true"
        return p

    async def _add(self, cmd, ioc: bool) -> None:
        r = await self.rest.call("AddOrder", **self._order(cmd, ioc))
        for txid in r.get("txid", [])[:1]:
            self.on_txid(cmd.id, txid)

    async def _place(self, cmd: core.Place) -> list:
        try:
            await self._add(cmd, False)
        except KrakenError as e:
            return [core.Rejected(self.clock(), "place", _reason(e.errors))]
        return [core.Placed(self.clock())]

    async def _ioc(self, cmd: core.Ioc) -> list:
        try:
            await self._add(cmd, True)
        except KrakenError as e:
            return [core.Rejected(self.clock(), "ioc", _reason(e.errors))]
        return await self._ioc_read(cmd.id)

    async def _ioc_read(self, leg: str) -> list:
        """An IOC is done when AddOrder answers: read what it filled (cum), then IocDone."""
        st, info = await self._read(leg)
        fill = [core.Filled(self.clock(), st.cum_qty, _dec(info.get("price") or None) or st.price, False, "ioc",
                            st.cum_qty, leg)] if st.cum_qty > 0 else []
        return fill + [core.IocDone(self.clock())]

    async def _amend(self, cmd: core.Amend) -> list:
        try:
            m = await self.feed.request("amend_order", {"cl_ord_id": cmd.id, "limit_price": cmd.price, "post_only": True})
        except ConnectionError:
            return [core.Rejected(self.clock(), "amend", "no_feed")]
        if m.get("success"):
            return [core.Amended(self.clock())]
        return [core.Rejected(self.clock(), "amend", _reason([str(m.get("error"))]))]

    async def _cancel(self, cmd: core.Cancel) -> list:
        self.cancels.add(cmd.id)
        try:
            await self.rest.call("CancelOrder", cl_ord_id=cmd.id)
        except KrakenError as e:
            return [core.Rejected(self.clock(), "cancel", _reason(e.errors))]
        return [core.Canceled(self.clock())]

    async def _read(self, leg: str, wait: float = NO_ANSWER) -> tuple[core.OrderState, dict]:
        """REST QueryOrders by cl_ord_id. Kraken's "unknown order": open False, cum 0.
        No answer: it tries again every 2 s; after `wait` s (the chase clock) it raises httpx.HTTPError."""
        start = self.clock()
        while True:
            try:
                r = await self.rest.call("QueryOrders", cl_ord_id=leg)
                break
            except KrakenError as e:
                if _reason(e.errors) != "not_open":
                    raise
                r = {}
                break
            except httpx.HTTPError:
                if self.clock() - start >= wait:
                    raise
                await asyncio.sleep(RETRY_EVERY)
        for info in r.values():
            return core.OrderState(self.clock(), info.get("status") in OPEN_STATES, Decimal(str(info.get("vol_exec", 0))),
                                   _dec(info.get("descr", {}).get("price")), leg), info
        return core.OrderState(self.clock(), False, Decimal(0), None, leg), {}

    async def _query(self, leg: str) -> list:
        st, info = await self._read(leg)
        if not st.open and info.get("status") in ("canceled", "expired") and leg not in self.cancels and leg == self.chase.leg:
            return [st, self._venue_canceled(info.get("reason") or "Kraken cancelled the order")]
        return [st]

    # ----- the executions feed -----
    def _on_exec(self, kind: str, items: list) -> None:
        c = self.chase
        if c is None:
            return
        for ev in exec_events(c, kind, items, self.clock(), self.cancels):
            self.on_event(ev)
            if isinstance(ev, core.OrderState) and not ev.open and ev.id == c.leg:
                why = next((x.get("reason") for x in items if x.get("cl_ord_id") == ev.id and x.get("reason")), None)
                self.on_event(self._venue_canceled(why or "Kraken cancelled the order"))
