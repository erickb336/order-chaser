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

from . import core, feed, keys, rest
from .db import DEFAULT_DIR, Db, lock_folder
from .kraken import WS_AUTH, KrakenGateway
from .sim import SimAccount, SimGateway

HOST, PORT = "127.0.0.1", 5180
WATCH_EVERY = 1.0   # s: at most one pair change each second, so that Kraken does not refuse the subscriptions
STATIC = Path(__file__).parent / "static"
PAGES = ("new", "chase", "result", "history", "setup")
MODE = "dry"   # the pages offer only dry runs until U6; Engine.start_live is the live path (tests use a local fake)
PLAIN_NUMBER = re.compile(r"[0-9]{1,15}(\.[0-9]{1,18})?")   # ASCII digits only: no "١" or "０"
NUMBER_MAX = Decimal(10) ** 12
WHAT = {"buy": "buy", "sell": "sell", "long": "buy", "short": "sell", "close-long": "sell", "close-short": "buy"}  # -> side
READ_EVERY = 3.0    # s: the account and positions are read at most this often (after fills; never each second)
# Every response: no frame of another site (clickjacking), and scripts only from the tool's own files (no inline
# script, no eval), so that an injected text cannot run before or after a key exists. Inline style attributes stay.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
SECURITY_HEADERS = [(b"x-frame-options", b"DENY"), (b"content-security-policy", CSP.encode()),
                    (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]


AMOUNT_TEXT = "Enter the amount as a plain number, such as 0.0500."
WITHDRAW_NOT_REMOVED = ("This key can withdraw funds. The tool refused it, but macOS did not let it remove the key. "
                        "Delete the item \"Kraken API key (order-chaser)\" in Keychain Access, and delete the key in Kraken Pro.")
KEYCHAIN_TEXT = keys.KEYCHAIN_TEXT


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

    def __init__(self, db: Db, clock=time.time, latency: float = 0.15, rate_start: float = 0.0,
                 refuse_margin_amends: bool = False) -> None:
        self.db, self.clock, self.latency = db, clock, latency
        saved = db.load_account()
        self.gw = SimGateway(SimAccount.from_json(saved) if saved else None)
        self.gw.refuse_margin_amends = refuse_margin_amends
        self.account: dict | None = None      # the last read of the (simulated) account and positions
        self.account_due = False              # a fill came after the last read
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
        self.tasks: set[asyncio.Task] = set()
        self.draining = False
        self.saved_key: str | None = None
        self.version = 0
        self.touched = 0.0
        self.feed: feed.PublicFeed | None = None
        self.kgw: KrakenGateway | None = None   # the live gateway (create_app gives it); a dry run uses self.gw

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
            fills = self.gw.on_book(book.top_bids(), book.top_asks(), self.clock(), self.watched)
        for ev in fills:
            # A liquidation also reaches a chase that the same book ended (its last fill): the core decides.
            # SimGateway never fills a live chase.
            if self.chase and self.chase.dry and (self.active or isinstance(ev, core.PositionGone)):
                self.handle(ev)
        if any(getattr(ev, "reason", None) == "liquidated" for ev in fills) or (
                ok and self.account and self.watched in self.account["unpriced"]):
            # A liquidation, with or without a chase, or the first price of a pair with positions: the pages show it now.
            self.read_account(force=True)
        if self.active:
            self.handle(core.Book(self.clock(), self.bid, self.ask, ok))
        self._save_account()
        self.version += 1

    def on_trade(self, side: str, price: Decimal, qty: Decimal) -> None:
        if self.active and self.chase.dry:
            for ev in self.gw.on_trade(side, price, qty, self.clock()):
                self.handle(ev)
        self._save_account()

    # ----- the simulated margin account -----
    def read_account(self, force: bool = False) -> dict:
        """Read the account and the positions (Kraken: TradeBalance and OpenPositions; simulated in a dry run):
        at the start of a chase, at most every 3 s after a fill, at its end, and when a form asks."""
        now = self.clock()
        if force or self.account is None or now - self.account["at"] >= READ_EVERY:
            self.account = json.loads(json.dumps(self.gw.read(now), default=_plain))
            self.account_due = False
            self.version += 1
        return self.account

    def _save_account(self) -> None:
        if self.gw.account.changed:
            self.db.save_account(self.gw.account.to_json())
            self.gw.account.changed = False

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
        if self.active and not self.chase.dry:
            self.kgw.tick(self.clock())
        if self.account_due and self.clock() - self.account["at"] >= READ_EVERY:
            self.read_account()
        if self.active and self.clock() - self.touched >= 2:
            self.touched = self.clock()
            self.db.touch(self.chase.id, self.touched)

    # ----- the chase -----
    @property
    def active(self) -> bool:
        return self.chase is not None and self.chase.phase != "done"

    def fresh(self) -> bool:
        """Valid prices for Start: a valid book at most core.STALE_AFTER s old (a heartbeat renews it)."""
        return self.book_ok and self.book_at is not None and self.clock() - self.book_at <= core.STALE_AFTER

    def rate_for(self, symbol: str) -> float:
        r, at = self.rates.get(symbol, (self.rate_start, self.clock()))
        return max(0.0, r - (self.clock() - at))

    async def start_live(self, symbol: str, what: str, qty: Decimal, limit: Decimal | None, timeout: int) -> list[str]:
        """A live spot chase: the checks of a dry run, then the key, the private feed, the safety timer and
        caffeinate, then the first order. Nothing goes to Kraken when a check fails.
        Not yet here (U6, with the pages): Setup's checks (Q10, the tested key), the confirm (Q6, Q7), live margin."""
        if what not in ("buy", "sell"):
            return ["A live chase is spot only for now. Use a dry run for margin."]
        if errors := self.start(symbol, what, qty, limit, timeout, check_only=True):
            return errors
        try:
            await self.kgw.open_key()
            await self.kgw.open()
        except Exception as e:
            await self.kgw.close()
            return [f"Nothing was placed. {keys.why(e)}"]
        if errors := self.start(symbol, what, qty, limit, timeout, venue=core.LIVE_VENUE):   # the prices moved
            await self.kgw.close()
        return errors

    def on_private(self, ev) -> None:
        if self.active and not self.chase.dry:
            self.handle(ev)

    def on_private_link(self, up: bool) -> None:
        if self.active and not self.chase.dry:
            self.handle(core.PrivateBack(self.clock()) if up else core.PrivateLost(self.clock()))

    def start(self, symbol: str, what: str, qty: Decimal, limit: Decimal | None, timeout: int,
              leverage: int | None = None, venue: str = core.SIM_VENUE, check_only: bool = False) -> list[str]:
        """what: buy or sell (spot); long or short (a margin open with leverage); close-long or close-short.
        check_only: the checks only, no chase."""
        if self.active:
            return ["A chase runs now. You can start a new chase when it ends."]
        pair = self.pairs.get(symbol)
        if pair is None:
            return ["Unknown pair, or the pair list did not load."]
        if what not in WHAT:
            return ["Pick what to do: buy, sell, open long, open short or close a position."]
        if symbol != self.watched or not self.fresh():
            return ["Start needs live prices."]
        side, close = WHAT[what], what.startswith("close-")
        errors = core.validate(pair, side, qty, limit, self.bid, self.ask, self.book_ok, timeout, close)
        margin = None
        if what not in ("buy", "sell"):
            acc = self.read_account(force=True)
            errors += core.validate_margin(pair, what, qty, leverage, self.ask if side == "buy" else self.bid,
                                           self.gw.account.list(), Decimal(acc["free_orders"]))
            if not errors and close:
                # The positions at the start, oldest first: the close plan (core.close_plan) takes them in this order.
                parts = self.gw.account.parts(symbol, what[6:])
                margin = core.Margin(core.average(parts)[2], True, parts)
            elif not errors:
                margin = core.Margin(leverage)
        if errors or check_only:
            return errors
        self.gw.pair = pair
        c, cmds = core.begin("oc" + uuid.uuid4().hex[:12], pair, side, qty, self.bid, self.ask, timeout,
                             self.clock(), venue, limit, self.rate_for(symbol), margin)
        self._apply(c, cmds)
        return []

    def close_preview(self, symbol: str, d: str, qty: Decimal) -> dict | None:
        """The close form: the close plan of qty in words, and the account margin level after it (estimate)."""
        acc, now = self.gw.account, self.clock()
        parts, pair = acc.parts(symbol, d), self.pairs.get(symbol)
        if not parts or pair is None:
            return None
        mark = acc.marks.get(symbol)
        return {**core.plan_words(parts, qty, pair, now), "level_now": acc.level(now),
                "level_after": core.close_preview(parts, qty, acc.equity(now), acc.used(), mark and mark[0])}

    def user(self, action: str) -> bool:
        if not self.active:
            return False
        self.handle(core.UserStop(self.clock()) if action == "stop" else core.UserFillNow(self.clock()))
        return True

    def handle(self, ev) -> None:
        was = self.chase.phase
        c, cmds = core.step(self.chase, ev)
        self._apply(c, cmds)
        if not c.dry and c.phase == "done" and was != "done":
            self._spawn(self.kgw.close())     # timer 0, no private feed, caffeinate off
        if c.margin is not None and isinstance(ev, core.Filled):
            self.account_due = True               # tick() reads it, at most every 3 s
        if c.margin is not None and c.phase == "done" and was != "done":
            self.read_account(force=True)         # at the end of a margin chase

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _apply(self, c: core.Chase, cmds: list) -> None:
        self.chase = c
        if not c.dry:
            self.kgw.chase = c
        now = self.clock()
        # Leave out what each book message changes: a book message alone does not write the chase.
        key = core.to_json(dataclasses.replace(c, bid=None, ask=None, book_at=None, rate=0.0, rate_at=0.0, book_ok=True))
        if key != self.saved_key:
            self.db.save(c, "dry" if c.dry else "live", now)
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
                self.draining = True   # the commands wait for the (simulated) latency; new ones join the queue
                asyncio.get_running_loop().call_later(self.latency, self._start_drain)
            else:
                self._start_drain()

    def _start_drain(self) -> None:
        """Run the drain as an eager task: it runs now, up to the first gateway call that waits (the network).
        A gateway that answers at once (the simulator) so finishes the drain before this returns, as before.
        An error in that part reaches the caller; an error after a wait is logged."""
        task = asyncio.Task(self._drain(), loop=asyncio.get_running_loop(), eager_start=True)
        if task.done():
            task.result()
        else:
            task.add_done_callback(lambda t: t.cancelled() or t.exception() is None
                                   or logging.getLogger(__name__).error("drain failed", exc_info=t.exception()))

    async def _drain(self) -> None:
        """Send the queued commands to the gateway, one at a time, and handle its answers. While a call waits,
        new events still reach the core; their commands join the queue and go out in order after it."""
        self.draining = True
        try:
            while self.queue:
                cmd = self.queue.popleft()
                for ev in await (self.gw if self.chase.dry else self.kgw).send(cmd, self.clock()):
                    self.handle(ev)
        finally:
            self.draining = False
            self._save_account()

    # ----- what the page sees -----
    def view(self, c: core.Chase | None) -> dict | None:
        if c is None:
            return None
        mode = "dry" if c.dry else "live"
        d = json.loads(json.dumps(dataclasses.asdict(c), default=_plain))
        d["summary"] = json.loads(json.dumps(core.summary(c), default=_plain))
        d["events"] = self.db.events(c.id)
        d["mode"] = mode
        d["filled"] = str(c.filled)
        d["remainder"] = str(c.qty - c.filled)
        d["rest_below_min"] = c.rest_below_min
        d["worst"] = str(core.worst_case(c.side, c.qty, c.limit, c.margin))
        d["dir"] = c.dir
        d["margin_est"] = json.loads(json.dumps(core.margin_summary(c), default=_plain))
        if c.margin:   # a position of this chase is in the account now: the pages offer its close only then
            ids = {p.id for p in c.margin.positions} if c.margin.close else {c.id}
            d["open_now"] = any(p["ref"] in ids for p in self.gw.account.positions)
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
                     "ok": self.book_ok, "fresh": self.fresh(), "age": None if self.book_at is None else now - self.book_at,
                     **self.link},
            "pairs": {k: json.loads(json.dumps(dataclasses.asdict(p), default=_plain)) for k, p in self.pairs.items()},
            "pairs_error": self.pairs_error,
            "rate": self.rate_for(self.watched),
            "account": self.account,
            "chase": self.view(self.chase),
        }


# ---------- Security guard (LOCALHOST-CSRF) ----------

class Guard:
    """Refuse a foreign Host, and a state-changing request (any method but GET and HEAD, and any WebSocket)
    without our Origin and session token. Every HTTP response carries SECURITY_HEADERS."""

    def __init__(self, app, token: str, port: int = PORT) -> None:
        self.app, self.token = app, token
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}

    def refusal(self, scope) -> str | None:
        if scope["type"] not in ("http", "websocket"):
            return "Refused: unknown connection type."
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = next((v for k, v in scope["headers"] if k.lower() == b"x-session-token"), b"")
        if headers.get("host") not in self.hosts:
            return "Refused: unknown Host."
        if scope["type"] == "websocket" or scope["method"] not in ("GET", "HEAD"):
            if headers.get("origin") not in self.origins:
                return "Refused: the Origin is not this tool."
            if not secrets.compare_digest(token, self.token.encode()):   # bytes: a non-ASCII header is a mismatch
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
                    msg = {**msg, "headers": [*msg.get("headers", []), *SECURITY_HEADERS]}
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

def create_app(data_dir: Path, connect: bool = True, rate_start: float = 0.0, clock=time.time, latency: float = 0.15,
               port: int = PORT, refuse_margin_amends: bool = False, kraken_transport: httpx.AsyncBaseTransport | None = None,
               kraken_url: str = rest.URL, kraken_ws: str = WS_AUTH, awake_cmd: list[str] | None = None):
    """connect=False leaves out the Kraken feed and the pair list: tests drive the engine.
    kraken_transport, kraken_url, kraken_ws: where the private calls go (tests give a local fake Kraken).
    awake_cmd: the command that keeps the Mac awake during a live chase (caffeinate -i)."""
    token = secrets.token_urlsafe(32)
    db = Db(data_dir)
    eng = Engine(db, clock=clock, latency=latency, rate_start=rate_start, refuse_margin_amends=refuse_margin_amends)
    # One REST client for each process: the nonce and the rate counter belong to the key.
    store = keys.KeyStore()
    kraken = rest.KrakenRest(None, httpx.AsyncClient(transport=kraken_transport, timeout=10), url=kraken_url)
    eng.kgw = KrakenGateway(kraken, store.read, kraken_ws, clock, awake_cmd)
    eng.kgw.on_event, eng.kgw.on_link = eng.on_private, eng.on_private_link
    page_cache: dict[str, str] = {}
    watched_at = [float("-inf")]

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
            if time.monotonic() - watched_at[0] < WATCH_EVERY:   # real time: it limits messages to Kraken
                return JSONResponse({"errors": ["Too many pair changes. Wait 1 s and try again."]}, status_code=429)
            watched_at[0] = time.monotonic()
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
                 else AMOUNT_TEXT if qty is None
                 else "Enter the limit as a plain number, such as 62480.0." if limit is None and d.get("limit") not in (None, "")
                 else "Pick a timeout from the list: 30 s to 15 min." if type(timeout) is not int or timeout not in core.TIMEOUTS
                 else None)
        if error:
            return JSONResponse({"errors": [error]}, status_code=400)
        what, lev = d.get("what"), d.get("leverage")
        error = ('Send what to do in the field "what" only, not "side".' if "side" in d
                 else "Pick what to do: buy, sell, open long, open short or close a position." if not isinstance(what, str) or what not in WHAT
                 else "Pick a leverage for the open: 2x to 5x." if what in ("long", "short") and type(lev) is not int
                 else "Leverage is only for an open long or an open short. Leave it out." if what not in ("long", "short") and "leverage" in d
                 # A limit beyond the price now (buy: above the ask, sell: below the bid) can cost more: the tick box accepts it.
                 # A limit on the other side gets the limit rule of the start (core.validate), with or without the tick box.
                 else "Accept the extra cost to start." if limit is not None and d.get("accept_extra") is not True
                      and (eng.ask is not None and limit > eng.ask if WHAT[what] == "buy" else eng.bid is not None and limit < eng.bid)
                 else None)
        if error:
            return JSONResponse({"errors": [error]}, status_code=400)
        errors = eng.start(pair, what, qty, limit, timeout, lev)
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
        c, _ = got
        if eng.chase and eng.chase.id == c.id:
            c = eng.chase
        return JSONResponse(eng.view(c))

    async def history(request: Request):
        rows = []
        for c, mode in db.history():
            s = core.summary(c)
            rows.append({"id": c.id, "started": c.started, "pair": c.pair.symbol, "side": c.side, "qty": str(c.qty),
                         "filled": str(c.filled), "avg": str(s["avg"]), "saving": str(s["saving"]), "mode": mode,
                         "outcome": c.outcome, "maker_qty": str(s["maker_qty"]), "base": c.pair.base,
                         "price_decimals": c.pair.price_decimals, "exit": c.exit, "nofeed": c.end_ask is None,
                         "beyond": c.end_ask is not None and (c.end_ask > c.limit if c.buy else c.end_ask < c.limit),
                         "dir": c.dir, "close": bool(c.margin and c.margin.close),
                         "leverage": c.margin.leverage if c.margin else None,
                         "pos_rest": str(c.margin.pos_qty - c.filled) if c.margin and c.margin.close else None})
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
        if eng.tasks:   # the end of a live chase that just ended: timer 0 (else it cancels other orders later)
            await asyncio.wait(eng.tasks, timeout=3)
        # A live chase that the stop cuts keeps its safety timer: Kraken cancels its order within 60 s.
        for t in tasks + [eng.kgw.task]:
            if t is not None:
                t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        await kraken.http.aclose()

    async def account(request: Request):
        return JSONResponse(eng.read_account())

    async def plan(request: Request):
        q = request.query_params
        qty, d, pair = number(q.get("qty")), q.get("dir"), eng.pairs.get(q.get("pair"))
        error = ("No position to close." if d not in ("long", "short") or pair is None
                 else AMOUNT_TEXT if qty is None else None)   # the number check and text of POST /api/chase
        if error:
            return JSONResponse({"errors": [error]}, status_code=400)
        # The size rules of POST /api/chase for a close: the amount, the minimums at the price now, the position.
        ref = (eng.ask if d == "short" else eng.bid) if pair.symbol == eng.watched else None
        errors = core.amount_errors(pair, qty, ref) or core.validate_margin(
            pair, "close-" + d, qty, None, None, eng.gw.account.list(), Decimal(0))
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        return JSONResponse(json.loads(json.dumps(eng.close_preview(pair.symbol, d, qty), default=_plain)))

    # ----- the Kraken API key (T4 U2): token + Origin guard (Guard), JSON only, never logged or sent back -----

    async def key_save(request: Request):
        d = await body(request)
        key = keys.parse(d.get("api_key"), d.get("private_key"))
        if key is None:
            return JSONResponse({"errors": [keys.SHAPE_TEXT]}, status_code=400)
        try:
            await asyncio.to_thread(store.save, key)   # a macOS prompt must not stop the chase loop
        except Exception:   # the text of a Keychain error is not shown: say what to do
            return JSONResponse({"errors": [KEYCHAIN_TEXT]}, status_code=503)
        return JSONResponse({"saved": True})

    async def key_remove(request: Request):
        try:
            await asyncio.to_thread(store.remove)
        except Exception:
            return JSONResponse({"errors": [KEYCHAIN_TEXT]}, status_code=503)
        return JSONResponse({"removed": True})

    async def key_test(request: Request):
        """Test each permission of the saved key with calls that change nothing. A key that can withdraw is
        removed from the Keychain at once (Q4)."""
        try:
            kraken.key = await asyncio.to_thread(store.read)
        except Exception:
            return JSONResponse({"errors": [KEYCHAIN_TEXT]}, status_code=503)
        if kraken.key is None:
            return JSONResponse({"errors": ["No key is saved. Paste the key first."]}, status_code=409)
        p = await rest.check(kraken)
        removed = False
        if p.verdict == "withdraw":
            kraken.key = None
            try:
                await asyncio.to_thread(store.remove)
                removed = True
            except Exception:
                return JSONResponse({"errors": [WITHDRAW_NOT_REMOVED]}, status_code=503)
        return JSONResponse({"verdict": p.verdict, "permissions": p.on, "error": p.error, "removed": removed})

    async def no_icon(request: Request):
        return PlainTextResponse("", status_code=204)

    routes = [Route("/", index), Route("/favicon.ico", no_icon)] + [Route(f"/{p}", html) for p in PAGES] + [
        Route("/api/state", state), Route("/api/stream", stream),
        Route("/api/watch", watch, methods=["POST"]), Route("/api/chase", start, methods=["POST"]),
        Route("/api/chase/stop", action, methods=["POST"]), Route("/api/chase/fillnow", action, methods=["POST"]),
        Route("/api/key", key_save, methods=["POST"]), Route("/api/key/remove", key_remove, methods=["POST"]),
        Route("/api/key/test", key_test, methods=["POST"]),
        Route("/api/chase/{id:str}", one), Route("/api/history", history), Route("/api/account", account), Route("/api/plan", plan),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine, app.state.token, app.state.keys = eng, token, store
    return Guard(app, token, port)


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
    ap.add_argument("--port", type=int, default=PORT, help="port on 127.0.0.1 (default: 5180)")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ORDER_CHASER_DATA", DEFAULT_DIR)),
                    help="folder for the SQLite file (default: ~/Library/Application Support/order-chaser)")
    ap.add_argument("--rate-start", type=float, default=0.0,
                    help="demo only: the estimated rate counter at start, to show the 'rate limit near' state")
    ap.add_argument("--refuse-margin-amends", action="store_true",
                    help="demo only: refuse each amend of a simulated margin order, to show cancel and replace")
    a = ap.parse_args()
    import uvicorn
    logging.basicConfig(format="%(message)s")
    try:
        lock = lock_folder(a.data_dir)  # noqa: F841  (held until the process ends)
    except BlockingIOError:
        raise SystemExit(f"Another order chaser runs on {a.data_dir}. Stop it first.")
    print(f"Order chaser (DRY RUN: no real orders) on http://{HOST}:{a.port}  data: {a.data_dir}")
    # A short graceful shutdown: an open page (SSE) must not keep a stopped tool alive.
    uvicorn.run(create_app(a.data_dir, rate_start=a.rate_start, port=a.port, refuse_margin_amends=a.refuse_margin_amends), host=HOST, port=a.port, log_level="warning",
                timeout_graceful_shutdown=2)
