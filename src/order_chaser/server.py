"""The local server: runs the chase (the shell around core.step) and serves the page on 127.0.0.1:5180."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import deque
from decimal import Decimal
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import core, feed
from .db import DEFAULT_DIR, Db, lock_folder
from .sim import SimGateway

HOST, PORT = "127.0.0.1", 5180
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
STATIC = Path(__file__).parent / "static"
PAGES = ("new", "chase", "result", "history", "setup")
MODE = "dry"   # T2 has only dry runs. No code path sends a private request to Kraken.
PLAIN_NUMBER = re.compile(r"\d{1,15}(\.\d{1,18})?")
NUMBER_MAX = Decimal(10) ** 12
# No page of the tool may show inside a frame of another site (clickjacking).
FRAME_HEADERS = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'")]


def number(v) -> Decimal | None:
    """A plain decimal string ("0.05", no commas, no exponent) or a JSON number, finite, from 0 to below 10^12.
    None for anything else."""
    if isinstance(v, str) and PLAIN_NUMBER.fullmatch(v.strip()):
        d = Decimal(v.strip())
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        d = Decimal(str(v))
    else:
        return None
    return d if d.is_finite() and 0 <= d < NUMBER_MAX else None


def _plain(o):
    if isinstance(o, Decimal):
        return str(o)
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    raise TypeError(type(o))


class Engine:
    """Owns the one chase. Runs core.step on each event and sends the commands to the gateway."""

    def __init__(self, db: Db, clock=time.time, latency: float = 0.15, rate_start: float = 0.0) -> None:
        self.db, self.clock, self.latency = db, clock, latency
        self.gw = SimGateway()
        self.pairs: dict[str, core.Pair] = {}
        self.pairs_error: str | None = None
        self.watched: str = "BTC/USD"
        self.bid = self.ask = None
        self.book_ok = False
        self.book_at: float | None = None
        self.link = {"up": False, "attempt": 0, "next_in": 0, "since": clock()}
        self.chase: core.Chase | None = None
        self.rates: dict[str, tuple[float, float]] = {}   # pair -> (estimated counter, at)
        self.rate_start = rate_start
        self.queue: deque = deque()
        self.draining = False
        self.saved_key: str | None = None
        self.version = 0
        self.touched = 0.0
        self.feed: feed.PublicFeed | None = None

    # ----- start of the tool -----
    def end_unfinished(self) -> None:
        """A chase that did not finish before the tool stopped ends now. It is not resumed."""
        now = self.clock()
        for c, last_seen in self.db.unfinished():
            self.chase = c
            self.handle(core.Restarted(now, last_seen))

    # ----- feed callbacks -----
    def on_book(self, book, ok: bool) -> None:
        self.bid, self.ask, self.book_ok = book.best_bid, book.best_ask, ok
        fills = []
        if ok:
            self.book_at = self.clock()
            fills = self.gw.on_book(book.top_bids(), book.top_asks(), self.clock())
        if self.active:
            for ev in fills:
                self.handle(ev)
        if self.active:
            self.handle(core.Book(self.clock(), self.bid, self.ask, ok))
        self.version += 1

    def on_trade(self, side: str, price: Decimal, qty: Decimal) -> None:
        if self.active:
            for ev in self.gw.on_trade(side, price, qty, self.clock()):
                self.handle(ev)

    def on_link(self, up: bool, attempt: int, next_in: float) -> None:
        if up != self.link["up"]:
            self.link["since"] = self.clock()
        self.link.update(up=up, attempt=attempt, next_in=next_in)
        if not up:
            self.book_ok = False
        if self.active:
            self.handle(core.FeedBack(self.clock()) if up else core.FeedLost(self.clock()))
        self.version += 1

    def tick(self) -> None:
        if self.active:
            self.handle(core.Tick(self.clock()))
            self.version += 1
        if self.active and self.clock() - self.touched >= 2:
            self.touched = self.clock()
            self.db.touch(self.chase.id, self.touched)

    # ----- the chase -----
    @property
    def active(self) -> bool:
        return self.chase is not None and self.chase.phase != "done"

    def rate_for(self, symbol: str) -> float:
        r, at = self.rates.get(symbol, (self.rate_start, self.clock()))
        return max(0.0, r - (self.clock() - at))

    def start(self, symbol: str, side: str, qty: Decimal, limit: Decimal | None, timeout: int) -> list[str]:
        if self.active:
            return ["A chase runs now. You can start a new chase when it ends."]
        pair = self.pairs.get(symbol)
        if pair is None:
            return ["Unknown pair, or the pair list did not load."]
        if symbol != self.watched or self.book_at is None or self.clock() - self.book_at > 15:
            return ["Start needs live prices."]
        errors = core.validate(pair, side, qty, limit, self.bid, self.ask, self.book_ok, timeout)
        if errors:
            return errors
        c, cmds = core.begin("oc-" + uuid.uuid4().hex[:16], pair, side, qty, self.bid, self.ask, timeout,
                             self.clock(), "the simulation", limit, self.rate_for(symbol))
        self._apply(c, cmds)
        return []

    def user(self, action: str) -> bool:
        if not self.active:
            return False
        self.handle(core.UserStop(self.clock()) if action == "stop" else core.UserFillNow(self.clock()))
        return True

    def handle(self, ev) -> None:
        c, cmds = core.step(self.chase, ev)
        self._apply(c, cmds)

    def _apply(self, c: core.Chase, cmds: list) -> None:
        self.chase = c
        now = self.clock()
        key = core.to_json(dataclasses.replace(c, bid=None, ask=None, rate=0.0, rate_at=0.0, book_ok=True))
        if key != self.saved_key:
            self.db.save(c, MODE, now)
            self.saved_key = key
        for cmd in cmds:
            if isinstance(cmd, core.Log):
                self.db.log(c.id, cmd, now)
            else:
                self.queue.append(cmd)
        if c.phase == "done":
            self.rates[c.pair.symbol] = (c.rate, c.rate_at)
        self.version += 1
        if self.queue and not self.draining:
            if self.latency:
                asyncio.get_running_loop().call_later(self.latency, self._drain)
            else:
                self._drain()

    def _drain(self) -> None:
        """Send the queued commands to the gateway, one at a time, and handle its answers."""
        self.draining = True
        try:
            while self.queue:
                cmd = self.queue.popleft()
                for ev in self.gw.send(cmd, self.clock()):
                    self.handle(ev)
        finally:
            self.draining = False

    # ----- what the page sees -----
    def view(self, c: core.Chase | None, mode: str = MODE) -> dict | None:
        if c is None:
            return None
        d = json.loads(json.dumps(dataclasses.asdict(c), default=_plain))
        d["summary"] = json.loads(json.dumps(core.summary(c), default=_plain))
        d["events"] = self.db.events(c.id)
        d["mode"] = mode
        d["filled"] = str(c.filled)
        d["remainder"] = str(c.qty - c.filled)
        d["worst"] = str(core.worst_case(c.side, c.qty, c.limit))
        if c.phase != "done":
            d["rate"] = max(0.0, c.rate - (self.clock() - c.rate_at))
        return d

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "now": now,
            "mode": MODE,
            "watched": self.watched,
            "feed": {"bid": str(self.bid) if self.bid else None, "ask": str(self.ask) if self.ask else None,
                     "ok": self.book_ok, "age": None if self.book_at is None else now - self.book_at, **self.link},
            "pairs": {k: json.loads(json.dumps(dataclasses.asdict(p), default=_plain)) for k, p in self.pairs.items()},
            "pairs_error": self.pairs_error,
            "rate": self.rate_for(self.watched),
            "chase": self.view(self.chase),
        }


# ---------- Security guard (LOCALHOST-CSRF) ----------

class Guard:
    """Refuse a foreign Host, and a state-changing request (any method but GET and HEAD, and any WebSocket)
    without our Origin and session token. Every HTTP response forbids framing."""

    def __init__(self, app, token: str) -> None:
        self.app, self.token = app, token

    def refusal(self, scope) -> str | None:
        if scope["type"] not in ("http", "websocket"):
            return "Refused: unknown connection type."
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("host") not in ALLOWED_HOSTS:
            return "Refused: unknown Host."
        if scope["type"] == "websocket" or scope["method"] not in ("GET", "HEAD"):
            if headers.get("origin") not in ALLOWED_ORIGINS:
                return "Refused: the Origin is not this tool."
            if not secrets.compare_digest(headers.get("x-session-token", ""), self.token):
                return "Refused: no valid session token."
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        reason = self.refusal(scope)
        if scope["type"] == "http":
            async def framed(msg):
                if msg["type"] == "http.response.start":
                    msg = {**msg, "headers": [*msg.get("headers", []), *FRAME_HEADERS]}
                await send(msg)
            if reason:
                await PlainTextResponse(reason, status_code=403)(scope, receive, framed)
            else:
                await self.app(scope, receive, framed)
        elif scope["type"] == "websocket" and not reason:
            await self.app(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": reason})


# ---------- App ----------

def create_app(data_dir: Path, connect: bool = True, rate_start: float = 0.0, clock=time.time, latency: float = 0.15):
    """connect=False leaves out the Kraken feed and the pair list: tests drive the engine."""
    token = secrets.token_urlsafe(32)
    db = Db(data_dir)
    eng = Engine(db, clock=clock, latency=latency, rate_start=rate_start)
    page_cache: dict[str, str] = {}

    def page(name: str) -> str:
        if name not in page_cache:
            page_cache[name] = (STATIC / f"{name}.html").read_text()
        return page_cache[name].replace("{{TOKEN}}", token)

    async def index(request: Request):
        return RedirectResponse("/chase" if eng.active else "/new")

    async def html(request: Request):
        name = request.url.path.strip("/")
        return HTMLResponse(page(name), headers={"Cache-Control": "no-store"})

    async def state(request: Request):
        return JSONResponse(eng.snapshot())

    async def stream(request: Request):
        async def gen():
            seen = -1
            last = 0.0
            while True:
                if await request.is_disconnected():
                    return
                if eng.version != seen or time.time() - last > 1:
                    seen, last = eng.version, time.time()
                    yield f"data: {json.dumps(eng.snapshot())}\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    async def body(request: Request) -> dict:
        try:
            d = await request.json()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    async def watch(request: Request):
        d = await body(request)
        symbol = d.get("pair")
        if not isinstance(symbol, str) or symbol not in eng.pairs:
            return JSONResponse({"errors": ["Unknown pair."]}, status_code=400)
        if eng.active and symbol != eng.chase.pair.symbol:
            return JSONResponse({"errors": ["A chase runs now."]}, status_code=409)
        if symbol != eng.watched:
            eng.watched = symbol
            eng.bid = eng.ask = eng.book_at = None
            eng.book_ok = False
            if eng.feed:
                await eng.feed.watch(eng.pairs[symbol])
        return JSONResponse({"ok": True})

    async def start(request: Request):
        d = await body(request)
        qty, timeout, pair = number(d.get("qty")), d.get("timeout"), d.get("pair")
        limit = None if d.get("limit") in (None, "") else number(d["limit"])
        error = ("Unknown pair." if not isinstance(pair, str)
                 else "Enter the amount as a plain number, such as 0.0500." if qty is None
                 else "Enter the limit as a plain number, such as 62480.0." if limit is None and d.get("limit") not in (None, "")
                 else "Pick a timeout from the list: 30 s to 15 min." if type(timeout) is not int or timeout not in core.TIMEOUTS
                 else "Accept the extra cost to start." if limit is not None and d.get("accept_extra") is not True
                 else None)
        if error:
            return JSONResponse({"errors": [error]}, status_code=400)
        errors = eng.start(pair, str(d.get("side")), qty, limit, timeout)
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        return JSONResponse({"id": eng.chase.id})

    async def action(request: Request):
        ok = eng.user(request.url.path.rsplit("/", 1)[1])
        return JSONResponse({"ok": ok}, status_code=200 if ok else 409)

    async def one(request: Request):
        got = db.get(request.path_params["id"])
        if not got:
            return JSONResponse({"error": "not found"}, status_code=404)
        c, mode = got
        if eng.chase and eng.chase.id == c.id:
            c = eng.chase
        return JSONResponse(eng.view(c, mode))

    async def history(request: Request):
        rows = []
        for c, mode in db.history():
            s = core.summary(c)
            rows.append({"id": c.id, "started": c.started, "pair": c.pair.symbol, "side": c.side, "qty": str(c.qty),
                         "filled": str(c.filled), "avg": str(s["avg"]), "saving": str(s["saving"]), "mode": mode,
                         "outcome": c.outcome, "maker_qty": str(s["maker_qty"]), "base": c.pair.base,
                         "price_decimals": c.pair.price_decimals, "exit": c.exit, "nofeed": c.end_ask is None,
                         "beyond": c.end_ask is not None and (c.end_ask > c.limit if c.buy else c.end_ask < c.limit)})
        return JSONResponse(rows)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        eng.end_unfinished()
        tasks = []
        if connect:
            eng.feed = feed.PublicFeed(eng.on_book, eng.on_trade, eng.on_link)
            tasks.append(asyncio.create_task(eng.feed.run()))
            tasks.append(asyncio.create_task(_pairs_loop(eng)))
        tasks.append(asyncio.create_task(_tick_loop(eng)))
        yield
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t

    async def no_icon(request: Request):
        return PlainTextResponse("", status_code=204)

    routes = [Route("/", index), Route("/favicon.ico", no_icon)] + [Route(f"/{p}", html) for p in PAGES] + [
        Route("/api/state", state), Route("/api/stream", stream),
        Route("/api/watch", watch, methods=["POST"]), Route("/api/chase", start, methods=["POST"]),
        Route("/api/chase/stop", action, methods=["POST"]), Route("/api/chase/fillnow", action, methods=["POST"]),
        Route("/api/chase/{id:str}", one), Route("/api/history", history),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine, app.state.token = eng, token
    return Guard(app, token)


async def _tick_loop(eng: Engine) -> None:
    while True:
        eng.tick()
        await asyncio.sleep(0.5)


async def _pairs_loop(eng: Engine) -> None:
    """Read the pair list (minimums, tick size, status) at start and every 30 s."""
    async with httpx.AsyncClient() as client:
        while True:
            try:
                eng.pairs = await feed.fetch_pairs(client)
                eng.pairs_error = None
                if eng.feed and eng.watched in eng.pairs:
                    await eng.feed.watch(eng.pairs[eng.watched])
            except Exception as e:  # shown on the page; the tool tries again
                eng.pairs_error = f"Could not read the Kraken pair list: {e}"
            eng.version += 1
            await asyncio.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser(prog="order-chaser", description="Kraken order chaser (dry run only) on http://127.0.0.1:5180")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ORDER_CHASER_DATA", DEFAULT_DIR)),
                    help="folder for the SQLite file (default: ~/Library/Application Support/order-chaser)")
    ap.add_argument("--rate-start", type=float, default=0.0,
                    help="demo only: the estimated rate counter at start, to show the 'rate limit near' state")
    a = ap.parse_args()
    import uvicorn
    logging.basicConfig(format="%(message)s")
    try:
        lock = lock_folder(a.data_dir)  # noqa: F841  (held until the process ends)
    except BlockingIOError:
        raise SystemExit(f"Another order chaser runs on {a.data_dir}. Stop it first.")
    print(f"Order chaser (DRY RUN: no real orders) on http://{HOST}:{PORT}  data: {a.data_dir}")
    # A short graceful shutdown: an open page (SSE) must not keep a stopped tool alive.
    uvicorn.run(create_app(a.data_dir, rate_start=a.rate_start), host=HOST, port=PORT, log_level="warning",
                timeout_graceful_shutdown=2)
