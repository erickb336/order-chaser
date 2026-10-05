"""The live gateway (T4 U4, U5): Kraken's private WebSocket v2 executions feed, the Kraken gateway, the safety
timer and caffeinate.

The gateway takes the core's commands and answers with the core's events, as the simulator does:
- Place, MarginPlace, Ioc, MarginIoc: REST AddOrder (post-only for the chase, IOC at the cap; leverage and
  reduce_only for margin), with our cl_ord_id. The IOC is read back at once (QueryOrders): Filled(cum) and IocDone.
- Amend: WebSocket v2 amend_order by cl_ord_id. A refusal goes to the core; the core decides (for margin: cancel and
  replace).
- Cancel: REST CancelOrder by cl_ord_id. Query: REST QueryOrders by cl_ord_id; an unknown order is open False, cum 0.
- The executions feed: a trade is Filled(cum) of its leg; a cancel that the tool did not ask for is OrderState and
  VenueCanceled ("timer" when the safety timer was due). snap_orders on each (re)subscribe gives the open legs.
- The safety timer: CancelAllOrdersAfter 60 s, set before the first order goes to Kraken (no order rests without it),
  renewed every 20 s while the chase runs, and 0 at the end.
- caffeinate -i while a live chase runs, so that the Mac does not sleep.

Nothing here reads the key: the REST client gets it from the key store.
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
TIMER = 60            # s: CancelAllOrdersAfter timeout
RENEW_EVERY = 20      # s
RETRY_EVERY = 2       # s: a failed renewal tries again
STABLE = 10           # s: the backoff of the private feed starts again only after a link stayed up this long
AMEND_WAIT = 5        # s: the answer to an amend_order request
OPEN_STATES = ("pending", "open", "new", "partially_filled")
PROTECT = {"stop-loss": "Stop-loss", "stop-loss-limit": "Stop-loss", "trailing-stop": "Stop-loss",
           "trailing-stop-limit": "Stop-loss", "take-profit": "Take-profit", "take-profit-limit": "Take-profit"}
log = logging.getLogger("order_chaser")


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


def other_orders(result: dict) -> list[dict]:
    """OpenOrders or ClosedOrders -> [{kind, pair, text}], stop-loss and take-profit orders first."""
    rows = []
    for o in result.values():
        d = o.get("descr", {})
        kind = PROTECT.get(d.get("ordertype", ""), d.get("ordertype", "order").capitalize())
        rows.append({"kind": kind, "pair": d.get("pair", ""), "text": d.get("order", ""), "cl_ord_id": o.get("cl_ord_id")})
    return sorted(rows, key=lambda r: r["kind"] not in ("Stop-loss", "Take-profit"))


# ---------- the private executions feed ----------

class PrivateFeed:
    """One authenticated WebSocket v2 link, subscribed to the executions channel (snap_orders, snap_trades).

    on_exec(kind, items): kind "snapshot" or "update". on_link(up, attempt, next_in).
    The backoff (2, 4, 8, 15 s) starts again only after a link stayed up STABLE s with no refusal."""

    def __init__(self, token, on_exec, on_link, url: str = WS_AUTH, clock=time.monotonic) -> None:
        self.token, self.on_exec, self.on_link, self.url, self.clock = token, on_exec, on_link, url, clock
        self.ws = None
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
                        if m.get("method") == "subscribe":
                            if m.get("success") is False:
                                refused = True
                                raise ConnectionError(f"Kraken refused the subscription: {m.get('error')}")
                            self.ws, up_at = ws, self.clock()
                            self.on_link(True, self.attempt, 0)
                        elif "req_id" in m and m["req_id"] in self.waiting:
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
            await asyncio.sleep(wait)

    async def request(self, method: str, params: dict) -> dict:
        """One request on the link (amend_order). Raises ConnectionError when the link is down or does not answer."""
        if self.ws is None:
            raise ConnectionError("the private feed is lost")
        self.req += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiting[self.req] = fut
        await self.ws.send(json.dumps({"method": method, "params": {**params, "token": self.tok}, "req_id": self.req},
                                      default=float))
        try:
            return await asyncio.wait_for(fut, AMEND_WAIT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.waiting.pop(self.req, None)
            raise ConnectionError("no answer on the private feed")


# ---------- the Kraken gateway ----------

class KrakenGateway:
    """The live gateway of one chase at a time. on_event(ev) and on_link(up) go to the engine."""

    def __init__(self, rest: KrakenRest, read_key, ws_url: str = WS_AUTH, clock=time.time,
                 awake_cmd: list[str] | None = None) -> None:
        self.rest, self.read_key, self.ws_url, self.clock = rest, read_key, ws_url, clock
        self.awake_cmd = ["caffeinate", "-i"] if awake_cmd is None else awake_cmd
        self.on_event = lambda ev: None
        self.on_link = lambda up: None
        self.chase: core.Chase | None = None
        self.feed: PrivateFeed | None = None
        self.task: asyncio.Task | None = None
        self.awake: subprocess.Popen | None = None
        self.private = {"up": False, "attempt": 0, "next_in": 0, "since": None}
        self.txids: dict[str, str] = {}        # cl_ord_id -> Kraken order id
        self.cancels: set[str] = set()         # legs that the tool asked to cancel
        self.timer = {"on": False, "renewed_at": None, "deadline": None, "fired_at": None, "tried_at": None}
        self.renewing = False

    async def open_key(self) -> None:
        """The key for the REST client: from memory, or from the Keychain one time at each start of the tool (Q9:
        macOS asks). Raises keys.NoKey or a Keychain error."""
        if self.rest.key is None:
            self.rest.key = await asyncio.to_thread(self.read_key)
        if self.rest.key is None:
            raise NoKey()

    # ----- the life of a live chase -----
    async def open(self) -> None:
        """Before the first order: the safety timer (raises KrakenError or httpx.HTTPError: nothing is sent then),
        the private feed and caffeinate."""
        self.txids, self.cancels = {}, set()
        self.timer = {"on": False, "renewed_at": None, "deadline": None, "fired_at": None, "tried_at": None}
        await self._set_timer(TIMER)
        self.feed = PrivateFeed(self._token, self._on_exec, self._link, self.ws_url)
        self.task = asyncio.create_task(self.feed.run())
        try:
            self.awake = subprocess.Popen([*self.awake_cmd, "-w", str(os.getpid())], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except OSError:   # no caffeinate (not macOS): the chase runs, the Mac can sleep
            log.warning("caffeinate did not start: the Mac can sleep during the chase.")

    async def close(self) -> None:
        """The chase ended: timer 0 (no cancel-all later), no feed, the Mac can sleep."""
        if self.awake is not None:
            self.awake.terminate()
            self.awake = None
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
            self.task = None
        self.private.update(up=False)
        if self.timer["on"]:
            self.timer["on"] = False
            with contextlib.suppress(KrakenError, httpx.HTTPError):
                await self.rest.call("CancelAllOrdersAfter", timeout=0)

    async def _token(self) -> str:
        return (await self.rest.call("GetWebSocketsToken"))["token"]

    def _link(self, up: bool, attempt: int, next_in: float) -> None:
        if up != self.private["up"]:
            self.private["since"] = self.clock()
        self.private.update(up=up, attempt=attempt, next_in=next_in)
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

    def due(self, now: float) -> bool:
        """The safety timer has fired on Kraken by now (it was not renewed in time)."""
        return self.timer["deadline"] is not None and now >= self.timer["deadline"]

    # ----- commands -----
    async def send(self, cmd, now: float) -> list:
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
                return await self.query(cmd.id)
        except httpx.HTTPError:   # no answer: did the order reach Kraken? Read it (the read tries again).
            if isinstance(cmd, core.Ioc):
                return await self._ioc_read(cmd.id)
            if isinstance(cmd, core.Place):
                st, _ = await self._read(cmd.id)
                return [core.Placed(self.clock())] if st.open else [core.Rejected(self.clock(), "place", "Kraken did not answer")]
            if isinstance(cmd, core.Cancel):
                return [core.Rejected(self.clock(), "cancel", "Kraken did not answer")]   # the core reads the order
        return []

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

    async def _place(self, cmd: core.Place) -> list:
        try:
            r = await self.rest.call("AddOrder", **self._order(cmd, False))
        except KrakenError as e:
            return [core.Rejected(self.clock(), "place", _reason(e.errors))]
        self.txids[cmd.id] = (r.get("txid") or [""])[0]
        return [core.Placed(self.clock())]

    async def _ioc(self, cmd: core.Ioc) -> list:
        try:
            r = await self.rest.call("AddOrder", **self._order(cmd, True))
        except KrakenError as e:
            return [core.Rejected(self.clock(), "ioc", _reason(e.errors))]
        self.txids[cmd.id] = (r.get("txid") or [""])[0]
        return await self._ioc_read(cmd.id)

    async def _ioc_read(self, leg: str) -> list:
        """An IOC is done when AddOrder answers: read what it filled (cum), then IocDone."""
        st, info = await self._read(leg)
        out = [core.Filled(self.clock(), st.cum_qty, Decimal(info.get("price") or 0) or st.price, False, "ioc",
                           st.cum_qty, leg)] if st.cum_qty > 0 else []
        return out + [core.IocDone(self.clock())]

    async def _amend(self, cmd: core.Amend) -> list:
        try:
            m = await self.feed.request("amend_order", {"cl_ord_id": cmd.id, "limit_price": cmd.price, "post_only": True})
        except (ConnectionError, AttributeError):
            return [core.Rejected(self.clock(), "amend", "no_feed")]
        if m.get("success"):
            return [core.Amended(self.clock())]
        return [core.Rejected(self.clock(), "amend", _reason([str(m.get("error"))]))]

    async def _cancel(self, cmd: core.Cancel) -> list:
        self.cancels.add(cmd.id)
        try:
            r = await self.rest.call("CancelOrder", cl_ord_id=cmd.id)
        except KrakenError as e:
            return [core.Rejected(self.clock(), "cancel", _reason(e.errors))]
        return [core.Canceled(self.clock())] if r.get("count", 1) else [core.Rejected(self.clock(), "cancel", "not_open")]

    async def _read(self, leg: str, tries: int = 30) -> tuple[core.OrderState, dict]:
        """REST QueryOrders by cl_ord_id. Kraken's "unknown order" (or no row): open False, cum 0.
        No answer: it tries again every 2 s, `tries` times, then raises httpx.HTTPError."""
        for i in range(tries):
            try:
                r = await self.rest.call("QueryOrders", cl_ord_id=leg)
                break
            except KrakenError as e:
                if _reason(e.errors) != "not_open":
                    raise
                r = {}
                break
            except httpx.HTTPError:
                if i == tries - 1:
                    raise
                await asyncio.sleep(RETRY_EVERY)
        for txid, info in r.items():
            self.txids.setdefault(leg, txid)
            price = info.get("descr", {}).get("price")
            return core.OrderState(self.clock(), info.get("status") in OPEN_STATES, Decimal(str(info.get("vol_exec", 0))),
                                   Decimal(str(price)) if price else None, leg), info
        return core.OrderState(self.clock(), False, Decimal(0), None, leg), {}

    async def query(self, leg: str) -> list:
        """The core's Query: the order state; a cancel by Kraken that the tool did not ask for is VenueCanceled."""
        st, info = await self._read(leg)
        out = [st]
        if not st.open and info.get("status") in ("canceled", "expired") and leg not in self.cancels and leg == self._leg():
            out.append(self._venue_canceled(info.get("reason") or "Kraken cancelled the order"))
        return out

    def _leg(self) -> str | None:
        return self.chase.leg if self.chase else None

    def _venue_canceled(self, reason: str) -> core.VenueCanceled:
        """Kraken cancelled the order without the tool: after the timer's deadline, that is the timer."""
        now = self.clock()
        if not self.due(now):
            return core.VenueCanceled(now, reason)
        self.timer.update(on=False, fired_at=self.timer["deadline"])
        return core.VenueCanceled(now, "timer")

    async def cancelled_others(self) -> list[dict]:
        """After the timer fired: the other orders that Kraken cancelled then (stop-loss and take-profit first)."""
        start = int(self.timer["fired_at"] or self.clock()) - 5
        r = await self.rest.call("ClosedOrders", start=start)
        mine = set(self.chase.legs) | {self.chase.ioc_id} if self.chase else set()
        closed = {k: o for k, o in r.get("closed", {}).items() if o.get("status") == "canceled" and o.get("cl_ord_id") not in mine}
        return other_orders(closed)

    async def open_orders(self) -> list[dict]:
        """The other open orders of the account (the live confirm, Q7)."""
        return other_orders((await self.rest.call("OpenOrders")).get("open", {}))

    # ----- the executions feed -----
    def _on_exec(self, kind: str, items: list) -> None:
        c = self.chase
        if c is None:
            return
        legs = set(c.legs)
        now = self.clock()
        for x in items:
            leg = x.get("cl_ord_id")
            if leg not in legs and leg != c.ioc_id:
                continue
            exec_type = x.get("exec_type")
            cum = Decimal(str(x.get("cum_qty", 0)))
            if exec_type == "trade":
                self.on_event(core.Filled(now, Decimal(str(x["last_qty"])), Decimal(str(x["last_price"])),
                                          x.get("liquidity_ind") == "m", "ioc" if leg == c.ioc_id else "chase", cum, leg))
            elif kind == "snapshot" and x.get("order_status") in OPEN_STATES:
                price = x.get("limit_price")
                self.on_event(core.OrderState(now, True, cum, Decimal(str(price)) if price else None, leg))
            elif exec_type in ("canceled", "expired") and leg == c.leg and leg not in self.cancels:
                self.on_event(core.OrderState(now, False, cum, None, leg))
                self.on_event(self._venue_canceled(x.get("reason") or "Kraken cancelled the order"))

    async def reconcile(self, c: core.Chase) -> tuple[list, str]:
        """The restart reconcile (Q5) of one live chase: read each leg by cl_ord_id; cancel a leg that is still open.
        Returns the OrderState of each leg (after the cancel) and what the tool found: open, timer or closed.
        Raises httpx.HTTPError or KrakenError when Kraken cannot be read."""
        out, found = [], "closed"
        for leg in (*c.legs, c.ioc_id):
            st, info = await self._read(leg, tries=1)
            if st.open:
                await self.rest.call("CancelOrder", cl_ord_id=leg)
                st, info = await self._read(leg, tries=1)
                found = "open"
            elif found == "closed" and info.get("status") == "canceled" and float(info.get("closetm", 0)) >= (c.off_from or 0):
                found = "timer"      # cancelled after the tool stopped, and not by the tool: the safety timer
            if info:
                out.append(st)
        return out, found
