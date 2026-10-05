"""A LOCAL fake Kraken for the live tests: private REST (/0/private/...) and the private WebSocket v2 (executions,
amend_order) on one uvicorn server on 127.0.0.1 and a free port. It checks the signature of each REST call with the
dummy test key. It is sample data only: no request goes to Kraken.

The message shapes follow Kraken's documentation (REST AddOrder, CancelOrder, QueryOrders, OpenOrders, ClosedOrders,
CancelAllOrdersAfter, GetWebSocketsToken; WS v2 executions and amend_order). The numbers are samples.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import threading
import time
import urllib.parse
from decimal import Decimal as D

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from order_chaser import keys, rest

# A probe that imports this file without tests/conftest.py still gets the tool's memory mode: each key call then
# refuses the real Keychain (keys.RealKeyring) instead of using it.
os.environ[keys.MODE] = "memory"

# A dummy key (never a real one): Kraken's published example secret and a made-up API key.
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
API = "DUMMYapiKEYforTESTSonly" + "A" * 33
OPEN = ("pending", "open")


class FakeKraken:
    def __init__(self, clock):
        self.clock = clock
        self.calls: list[tuple[str, dict]] = []
        self.orders: dict[str, dict] = {}     # txid -> order
        self.bid, self.ask = D("62417.9"), D("62418.5")
        self.ask_qty = D("0.01")
        self.timer: float | None = None       # the cancel-all deadline
        self.rest_down = False
        self.ws_down = False
        self.refuse_amend: str | None = None
        self.refuse_subscribe: str | None = None
        self.balance = {"ZUSD": "4210.55", "XXBT": "0.1000"}
        self.clients: set[WebSocket] = set()
        self.n = 0
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url, self.ws_url = f"http://127.0.0.1:{self.port}", f"ws://127.0.0.1:{self.port}/v2"
        app = Starlette(routes=[Route("/0/private/{method}", self.rest, methods=["POST"]), WebSocketRoute("/v2", self.ws)])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", ws="websockets-sansio"))
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_until_complete, args=(self.server.serve(),), daemon=True).start()
        while not self.server.started:
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True

    def run(self, fn, *args):
        """Run fn on the fake's loop (it may push WebSocket messages)."""
        async def go():
            r = fn(*args)
            return await r if asyncio.iscoroutine(r) else r
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(5)

    # ----- the orders -----
    def add_other(self, ordertype: str, pair: str, text: str) -> None:
        """An order of the account that the chase did not place (Q7, C8)."""
        self.n += 1
        self.orders[f"OTHER{self.n}"] = {"status": "open", "cl_ord_id": None, "vol": "1", "vol_exec": "0",
                                          "descr": {"pair": pair, "type": "sell", "ordertype": ordertype, "price": "0", "order": text}}

    def by_cl(self, cl: str) -> tuple[str, dict] | tuple[None, None]:
        return next(((k, o) for k, o in self.orders.items() if o["cl_ord_id"] == cl), (None, None))

    def calls_of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    async def push(self, kind: str, items: list) -> None:
        msg = json.dumps({"channel": "executions", "type": kind, "data": items}, default=str)
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self.clients.discard(ws)

    def exec_of(self, o: dict, exec_type: str, **extra) -> dict:
        return {"exec_type": exec_type, "order_id": o["txid"], "cl_ord_id": o["cl_ord_id"], "symbol": "BTC/USD",
                "side": o["descr"]["type"], "order_status": o["status"], "cum_qty": float(o["vol_exec"]),
                "limit_price": float(o["descr"]["price"]), "order_qty": float(o["vol"]), **extra}

    async def fill(self, cl: str, qty: str, price: str | None = None, push: bool = True) -> None:
        """A maker fill of a resting order (at its price)."""
        _, o = self.by_cl(cl)
        o["vol_exec"] = str(D(o["vol_exec"]) + D(qty))
        o["price"] = price or o["descr"]["price"]
        if D(o["vol_exec"]) >= D(o["vol"]):
            o["status"], o["closetm"] = "closed", self.clock()
        if push:
            await self.push("update", [self.exec_of(o, "trade", last_qty=float(qty), last_price=float(o["price"]),
                                                    liquidity_ind="m")])

    async def fire_timer(self) -> None:
        """The safety timer fires: Kraken cancels every open order of the account."""
        gone = []
        for o in self.orders.values():
            if o["status"] in OPEN:
                o.update(status="canceled", reason="Cancel all orders after timeout", closetm=self.clock())
                gone.append(o)
        self.timer = None
        await self.push("update", [self.exec_of(o, "canceled", reason=o["reason"]) for o in gone if o["cl_ord_id"]])

    async def drop_ws(self) -> None:
        for ws in list(self.clients):
            await ws.close()
        self.clients.clear()

    # ----- REST -----
    async def rest(self, request):
        method, data = request.path_params["method"], (await request.body()).decode()
        params = dict(urllib.parse.parse_qsl(data))
        assert request.headers["API-Key"] == API
        assert request.headers["API-Sign"] == rest.sign(base64.b64decode(SECRET), request.url.path, int(params["nonce"]), data)
        if self.rest_down:
            return JSONResponse({"error": "down"}, status_code=503)
        params.pop("nonce")
        self.calls.append((method, params))
        try:
            return JSONResponse({"error": [], "result": getattr(self, "r_" + method)(params)})
        except KrakenRefusal as e:
            return JSONResponse({"error": [str(e)]})

    def r_GetWebSocketsToken(self, p):
        return {"token": "fake-ws-token", "expires": 900}

    def r_Balance(self, p):
        return self.balance

    def r_TradeVolume(self, p):
        return {"currency": "ZUSD", "volume": "0", "fees": {p["pair"]: {"fee": "0.4000"}},
                "fees_maker": {p["pair"]: {"fee": "0.2500"}}}

    def r_CancelAllOrdersAfter(self, p):
        t = int(p["timeout"])
        self.timer = self.clock() + t if t else None
        return {"currentTime": "2026-10-04T09:52:04Z", "triggerTime": "0" if not t else "2026-10-04T09:53:04Z"}

    def r_WithdrawMethods(self, p):
        raise KrakenRefusal("EGeneral:Permission denied")    # the dummy key cannot withdraw

    def r_AddOrder(self, p):
        if p.get("validate"):                                 # the permission test: Kraken checks, places nothing
            return {"descr": {"order": "validated"}}
        assert len(p["cl_ord_id"]) <= 18
        price, qty, buy = D(p["price"]), D(p["volume"]), p["type"] == "buy"
        ioc = p.get("timeinforce") == "IOC"
        if p.get("oflags") == "post" and (price >= self.ask if buy else price <= self.bid):
            raise KrakenRefusal("EOrder:Post only order")
        self.n += 1
        txid = f"OFAKE{self.n:02d}-AAAAA-BBBBBB"
        o = {"txid": txid, "cl_ord_id": p["cl_ord_id"], "status": "open", "vol": p["volume"], "vol_exec": "0", "price": "0",
             "opentm": self.clock(), "descr": {"pair": p["pair"], "type": p["type"], "ordertype": "limit", "price": p["price"],
                                                "order": f"{p['type']} {p['volume']} {p['pair']} @ limit {p['price']}"}}
        self.orders[txid] = o
        if ioc:   # fills at once against the ask (buy) up to its price, the rest is cancelled
            got = min(qty, self.ask_qty) if (self.ask <= price if buy else self.bid >= price) else D(0)
            o.update(vol_exec=str(got), price=str(self.ask if buy else self.bid), status="closed" if got == qty else "canceled",
                     closetm=self.clock())
        return {"txid": [txid], "descr": {"order": o["descr"]["order"]}}

    def r_CancelOrder(self, p):
        _, o = self.by_cl(p["cl_ord_id"])
        if o is None or o["status"] not in OPEN:
            raise KrakenRefusal("EOrder:Unknown order")
        o.update(status="canceled", reason="User requested", closetm=self.clock())
        asyncio.get_running_loop().create_task(self.push("update", [self.exec_of(o, "canceled", reason="User requested")]))
        return {"count": 1}

    def r_QueryOrders(self, p):
        txid, o = self.by_cl(p["cl_ord_id"])
        if o is None:
            raise KrakenRefusal("EOrder:Unknown order")
        return {txid: o}

    def r_OpenOrders(self, p):
        return {"open": {k: o for k, o in self.orders.items() if o["status"] in OPEN}}

    def r_ClosedOrders(self, p):
        return {"closed": {k: o for k, o in self.orders.items() if o["status"] not in OPEN
                           and o.get("closetm", 0) >= float(p.get("start", 0))}}

    # ----- the private WebSocket v2 -----
    async def ws(self, ws: WebSocket):
        await ws.accept()
        if self.ws_down:
            await ws.close()
            return
        try:
            while True:
                m = json.loads(await ws.receive_text())
                if m["method"] == "subscribe":
                    assert m["params"]["token"] == "fake-ws-token" and m["params"]["channel"] == "executions"
                    self.calls.append(("subscribe", {k: v for k, v in m["params"].items() if k != "token"}))
                    if self.refuse_subscribe:
                        await ws.send_text(json.dumps({"method": "subscribe", "success": False,
                                                       "error": self.refuse_subscribe}))
                        continue
                    self.clients.add(ws)
                    await ws.send_text(json.dumps({"method": "subscribe", "success": True,
                                                   "result": {"channel": "executions", "snapshot": True}}))
                    snap = [self.exec_of(o, "new") for o in self.orders.values() if o["status"] in OPEN and o["cl_ord_id"]]
                    await ws.send_text(json.dumps({"channel": "executions", "type": "snapshot", "data": snap}, default=str))
                elif m["method"] == "amend_order":
                    await ws.send_text(json.dumps(self.amend(m)))
        except WebSocketDisconnect:
            self.clients.discard(ws)

    def amend(self, m: dict) -> dict:
        p = m["params"]
        self.calls.append(("amend_order", {k: v for k, v in p.items() if k != "token"}))
        _, o = self.by_cl(p["cl_ord_id"])
        no = {"method": "amend_order", "req_id": m["req_id"], "success": False}
        if self.refuse_amend:
            return {**no, "error": self.refuse_amend}
        if o is None or o["status"] not in OPEN:
            return {**no, "error": "EOrder:Unknown order"}
        price = D(str(p["limit_price"]))
        if p.get("post_only") and (price >= self.ask if o["descr"]["type"] == "buy" else price <= self.bid):
            return {**no, "error": "EOrder:Post only order"}
        o["descr"]["price"] = str(price)
        return {"method": "amend_order", "req_id": m["req_id"], "success": True,
                "result": {"amend_id": "TFAKE", "cl_ord_id": p["cl_ord_id"]}}


class KrakenRefusal(Exception):
    pass
