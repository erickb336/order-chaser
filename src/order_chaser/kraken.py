"""The live side of Kraken (T4): the private WebSocket v2 executions feed (U4).

The executions channel (wss://ws-auth.kraken.com/v2) reports each change of our orders. The token comes from REST
GetWebSocketsToken. Each (re)subscribe asks for snap_orders and snap_trades, so a link that comes back first gives
the open orders and the last trades. exec_events() maps the reports of one chase to the core's events.

Nothing here reads the key: the REST client has it.
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import websockets

from . import core

WS_AUTH = "wss://ws-auth.kraken.com/v2"
STABLE = 10           # s: the backoff starts again only after a link stayed up this long with no refusal
REQUEST_WAIT = 5      # s: the answer to a request on the link (amend_order)
OPEN_STATES = ("pending", "pending_new", "open", "new", "partially_filled")   # REST and WS v2 order states


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
