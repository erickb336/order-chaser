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
from .kraken import WS_AUTH, KrakenGateway, others
from .sim import SimAccount, SimGateway

HOST, PORT = "127.0.0.1", 5180
WATCH_EVERY = 1.0   # s: at most one pair change each second, so that Kraken does not refuse the subscriptions
STATIC = Path(__file__).parent / "static"
PAGES = ("new", "chase", "result", "history", "setup", "reconcile")
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
ACCOUNT_ANSWERS = ("no", "yes", "sub")   # Q10: no other tool; another tool (live stays off); "I use a sub-account" (G19 P2)
OTHERS_TICK_TEXT = ("Tick the box to start: the safety timer can cancel your stop-loss and take-profit orders.")
FIRST_TICK_TEXT = "Tick the box for your first live order."
STOP_TIMER_TEXT = ("The tool stopped during a live chase. The safety timer stays on: within 60 s Kraken cancels ALL "
                   "open orders on this account, also stop-loss and take-profit orders.")


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
        self.gw = SimGateway(self._saved_account())
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
        self.blocked: str | None = None         # why no live chase can start now (the restart reconcile is not done)
        self.restart: core.Chase | None = None  # the live chase of the last stop, until the restart reconcile ends it
        self.reading = {"attempt": 0, "next_in": 0, "error": None}   # the restart reconcile's tries

    def _saved_account(self) -> SimAccount | None:
        """The simulated account of the dry run; None (a new account) when the row is not a valid account."""
        saved = self.db.load_account()
        try:
            return SimAccount.from_json(saved) if saved else None
        except (ValueError, KeyError, TypeError, AttributeError):
            logging.getLogger("order_chaser").warning(
                "The saved simulated account is not valid. Margin dry runs start with a new account of 5,000 USD.")
            return None

    # ----- start of the tool -----
    def end_unfinished(self) -> None:
        """A chase that did not finish before the tool stopped ends now. It is not resumed. A live chase waits for
        the restart reconcile (reconcile()); new live chases wait too."""
        now = self.clock()
        for c, last_seen in self.db.unfinished():
            self.chase = c
            self.handle(core.Restarted(now, last_seen))
        if self.chase is not None and self.chase.phase == "restart":
            self.restart, self.chase = self.chase, None
            self.blocked = "The tool reads Kraken to check the live chase of the last stop. New live chases wait for it."

    async def reconcile(self) -> bool:
        """The restart reconcile (Q5): read the legs of the live chase of the last stop by cl_ord_id, cancel an open
        leg, record the fills, end the chase. False when Kraken cannot be read: blocked says why."""
        c = self.restart
        self.reading["attempt"] += 1
        try:
            await self.kgw.open_key()
            evs = await self.kgw.reconcile(c)
        except Exception as e:   # no key, Keychain denied, Kraken refused or did not answer
            self.reading["error"] = keys.why(e)
            self.blocked = (f"The tool cannot read Kraken to check the live chase of the last stop. {keys.why(e)} "
                            "New live chases wait until it can.")
            self.version += 1
            return False
        self.chase = c
        for ev in evs:
            self.handle(ev)
        self.restart = self.blocked = None
        self.reading["error"] = None
        self.version += 1
        return True

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

    # ----- setup and the live check (Q6, Q7, Q9, Q10) -----
    def setup_state(self) -> dict:
        """What Setup recorded, and why a live chase cannot start now (empty: it can)."""
        a = json.loads(self.db.setting("account") or "null")    # {"answer", "at"}
        k = json.loads(self.db.setting("key") or "null")        # {"saved", "tested", "verdict", "permissions"}
        why = []
        if a is None:
            why.append("Answer Setup, step 2: does another bot or API tool use this Kraken account?")
        elif a["answer"] == "yes":
            why.append("Setup, step 2: use a Kraken sub-account for this tool. Live chases stay off until you confirm it.")
        if k is None or k.get("verdict") == "withdraw":
            why.append("Save a Kraken API key in Setup, step 3.")
        elif k.get("verdict") != "ok":
            why.append("Test the Kraken API key in Setup, step 3. It must pass.")
        if self.blocked:
            why.append(self.blocked)
        return {"account": a, "key": k, "ready": not why, "why": why, "first": not self.db.any_live(),
                "unlocked": self.kgw is not None and self.kgw.rest.key is not None}   # Q9: macOS asked this start

    async def live_check(self, symbol: str) -> dict:
        """The checks before a live start: Setup, live prices, the key (macOS asks one time at each start, Q9),
        the private feed's token, and the other open orders of the account (Q7). Nothing changes on Kraken."""
        s = self.setup_state()
        pair = self.pairs.get(symbol)
        out = {"errors": list(s["why"]), "first": s["first"], "min_qty": str(pair.ordermin) if pair else None,
               "others": None}
        if pair is None:
            out["errors"].append("Unknown pair, or the pair list did not load.")
        elif symbol != self.watched or not self.fresh():
            out["errors"].append("Start needs live prices.")
        if out["errors"]:
            return out
        try:
            await self.kgw.open_key()
            got = await self.kgw.rest.call("OpenOrders")
            await self.kgw.rest.call("GetWebSocketsToken")   # Access WebSockets API: the private feed can connect
        except Exception as e:
            out["errors"].append(f"Nothing was placed. {keys.why(e)}")
            return out
        out["others"] = others(got.get("open", {}))
        return out

    def on_private(self, ev) -> None:
        if isinstance(ev, core.OthersCancelled) and self.chase and self.chase.id == ev.id:
            self.handle(ev)                     # also after the chase ended (C8)
        elif self.active and not self.chase.dry:
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

    def user(self, action: str) -> str | None:
        """Stop or Fill the rest now. None: done; else the reason why the tool ignored it (the page shows it)."""
        if not self.active:
            return "No chase runs now."
        before = self.chase
        stop = action == "stop"
        self.handle(core.UserStop(self.clock()) if stop else core.UserFillNow(self.clock()))
        if self.chase.exit == ("stop" if stop else "fillnow") and before.exit != self.chase.exit or self.chase.phase == "done" and before.phase != "done":
            return None
        c = before
        if c.phase == "ioc":
            return "The IOC for the rest is at Kraken now. It ends the chase in a moment."
        if c.exit == "stop":
            return "Stop is already under way: the tool is cancelling the order."
        if c.exit in core.GONE:
            return "The position is gone. The chase ends now."
        if c.exit is not None:
            return "The chase already ends: the tool cancels the order and fills the rest with one IOC."
        if not stop and c.phase == "feed_lost":
            return "The price feed is lost, so there is no valid price for the IOC. Stop the chase, or wait for the feed."
        return "The tool cannot do that in this state of the chase."

    def handle(self, ev) -> None:
        was = self.chase.phase
        c, cmds = core.step(self.chase, ev)
        self._apply(c, cmds)
        if not c.dry and c.phase == "done" and was != "done":
            self._spawn(self.kgw.close(keep_timer=c.outcome == "noanswer"))   # timer 0, no private feed, caffeinate off
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
        if not c.dry:
            d["txids"] = self.db.txids(c.id)
        if not c.dry and c.phase != "done" and self.kgw.chase is c:
            d["timer"] = dict(self.kgw.timer)
            d["awake"] = self.kgw.awake is not None
        return d

    def snapshot(self) -> dict:
        now = self.clock()
        return {
            "now": now,
            "live": self.setup_state(),
            "watched": self.watched,
            "feed": {"bid": str(self.bid) if self.bid else None, "ask": str(self.ask) if self.ask else None,
                     "ok": self.book_ok, "fresh": self.fresh(), "age": None if self.book_at is None else now - self.book_at,
                     **self.link},
            "pairs": {k: json.loads(json.dumps(dataclasses.asdict(p), default=_plain)) for k, p in self.pairs.items()},
            "pairs_error": self.pairs_error,
            "rate": self.rate_for(self.watched),
            "account": self.account,
            "chase": self.view(self.chase),
            "restart": self.view(self.restart) and {**self.view(self.restart), "reading": self.reading},
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
    eng.kgw.on_event, eng.kgw.on_link, eng.kgw.on_txid = eng.on_private, eng.on_private_link, db.set_txid
    page_cache: dict[str, str] = {}
    watched_at = [float("-inf")]

    def page(name: str) -> str:
        if name not in page_cache:
            page_cache[name] = (STATIC / f"{name}.html").read_text()
        return page_cache[name].replace("{{TOKEN}}", token)

    async def index(request: Request):
        return RedirectResponse("/reconcile" if eng.restart else "/chase" if eng.active else "/new")

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
        mode = d.get("mode", "dry")
        if mode == "live":
            errors = await live_start(d, pair, what, qty, limit, timeout)
        elif mode == "dry":
            errors = eng.start(pair, what, qty, limit, timeout, lev)
        else:
            errors = ['Send the mode "dry" or "live".']
        if errors:
            return JSONResponse({"errors": errors}, status_code=400)
        return JSONResponse({"id": eng.chase.id})

    async def live_start(d: dict, pair: str, what: str, qty: Decimal, limit: Decimal | None, timeout: int) -> list[str]:
        """A live chase starts only after the live check, with its ticks (Q6, Q7)."""
        if eng.active:
            return ["A chase runs now. You can start a new chase when it ends."]
        chk = await eng.live_check(pair)
        if chk["errors"]:
            return chk["errors"]
        p = eng.pairs[pair]
        if chk["first"] and qty != p.ordermin:
            return [f"Your first live order uses the Kraken minimum: {core.fmt_qty(p.ordermin)} {p.base}."]
        if chk["first"] and d.get("first_ok") is not True:
            return [FIRST_TICK_TEXT]
        if any(o["protect"] for o in chk["others"]) and d.get("others_ok") is not True:
            return [OTHERS_TICK_TEXT]
        return await eng.start_live(pair, what, qty, limit, timeout)

    async def action(request: Request):
        why = eng.user(request.url.path.rsplit("/", 1)[1])
        return JSONResponse({"ok": why is None, "reason": why}, status_code=200 if why is None else 409)

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
        if eng.restart:
            tasks.append(asyncio.create_task(_reconcile_loop(eng)))
        yield
        if eng.tasks:   # the end of a live chase that just ended: timer 0 (else it cancels other orders later)
            await asyncio.wait(eng.tasks, timeout=3)
        # A live chase that the stop cuts keeps its safety timer: Kraken cancels its order within 60 s.
        if eng.active and not eng.chase.dry and eng.kgw.timer["on"]:
            logging.getLogger("order_chaser").warning(STOP_TIMER_TEXT)
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

    # ----- Setup (Q10, G19) and the live check -----

    async def setup_get(request: Request):
        return JSONResponse({**eng.setup_state(), "host": f"{HOST}:{port}"})

    async def setup_post(request: Request):
        d = await body(request)
        if d.get("account") not in ACCOUNT_ANSWERS:
            return JSONResponse({"errors": ['Send the account answer "no", "yes" or "sub".']}, status_code=400)
        db.set_setting("account", json.dumps({"answer": d["account"], "at": eng.clock()}))
        eng.version += 1
        return JSONResponse(eng.setup_state())

    async def live_check(request: Request):
        d = await body(request)
        if not isinstance(d.get("pair"), str):
            return JSONResponse({"errors": ["Unknown pair."]}, status_code=400)
        return JSONResponse(await eng.live_check(d["pair"]))

    def key_state(**k) -> None:
        db.set_setting("key", json.dumps(k) if k else None)
        eng.version += 1

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
        kraken.key = None                              # the next live call takes the new key (from memory)
        key_state(saved=eng.clock(), tested=None, verdict=None, permissions=None)
        return JSONResponse({"saved": True})

    async def key_remove(request: Request):
        try:
            await asyncio.to_thread(store.remove)
        except Exception:
            return JSONResponse({"errors": [KEYCHAIN_TEXT]}, status_code=503)
        kraken.key = None
        key_state()
        return JSONResponse({"removed": True})

    async def key_test(request: Request):
        """Test each permission of the saved key with calls that change nothing. A key that can withdraw is
        removed from the Keychain at once (Q4)."""
        try:
            kraken.key = await asyncio.to_thread(store.read)
        except Exception:
            return JSONResponse({"errors": [KEYCHAIN_TEXT]}, status_code=503)
        if kraken.key is None:
            key_state()
            return JSONResponse({"errors": ["No key is saved. Paste the key first."]}, status_code=409)
        p = await rest.check(kraken)
        removed = False
        if p.verdict == "withdraw":
            kraken.key = None
            try:
                await asyncio.to_thread(store.remove)
                removed = True
            except Exception:
                key_state(saved=eng.clock(), tested=eng.clock(), verdict="withdraw", permissions=p.on)
                return JSONResponse({"errors": [WITHDRAW_NOT_REMOVED]}, status_code=503)
            key_state(saved=None, tested=eng.clock(), verdict="withdraw", permissions=p.on)   # the page says why
        else:
            saved = (eng.setup_state()["key"] or {}).get("saved", eng.clock())
            key_state(saved=saved, tested=eng.clock(), verdict=p.verdict, permissions=p.on)
        return JSONResponse({"verdict": p.verdict, "permissions": p.on, "error": p.error, "removed": removed})

    async def no_icon(request: Request):
        return PlainTextResponse("", status_code=204)

    routes = [Route("/", index), Route("/favicon.ico", no_icon)] + [Route(f"/{p}", html) for p in PAGES] + [
        Route("/api/state", state), Route("/api/stream", stream),
        Route("/api/watch", watch, methods=["POST"]), Route("/api/chase", start, methods=["POST"]),
        Route("/api/chase/stop", action, methods=["POST"]), Route("/api/chase/fillnow", action, methods=["POST"]),
        Route("/api/key", key_save, methods=["POST"]), Route("/api/key/remove", key_remove, methods=["POST"]),
        Route("/api/key/test", key_test, methods=["POST"]),
        Route("/api/setup", setup_get), Route("/api/setup", setup_post, methods=["POST"]),
        Route("/api/live/check", live_check, methods=["POST"]),
        Route("/api/chase/{id:str}", one), Route("/api/history", history), Route("/api/account", account), Route("/api/plan", plan),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine, app.state.token, app.state.keys = eng, token, store
    return Guard(app, token, port)


async def _reconcile_loop(eng: Engine) -> None:
    """The restart reconcile: try until Kraken can be read, 2, 4, 8 then 15 s apart. New live chases wait."""
    wait = 2
    while not await eng.reconcile():
        eng.reading["next_in"] = wait
        await asyncio.sleep(wait)
        wait = min(wait * 2, 15)


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


def _port(v: str) -> int:
    """--port: a whole number from 1 to 65535."""
    if not re.fullmatch(r"[0-9]{1,5}", v) or not 1 <= int(v) <= 65535:
        raise argparse.ArgumentTypeError(f"{v!r} is not a port: use a number from 1 to 65535")
    return int(v)


def main() -> None:
    ap = argparse.ArgumentParser(prog="order-chaser", description="Kraken order chaser (dry run and live) on http://127.0.0.1:5180")
    ap.add_argument("--port", type=_port, default=PORT, help="port on 127.0.0.1, 1 to 65535 (default: 5180)")
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
    print(f"Order chaser on http://{HOST}:{a.port}  data: {a.data_dir}  (a chase is a dry run unless you choose Live)")
    # A short graceful shutdown: an open page (SSE) must not keep a stopped tool alive.
    uvicorn.run(create_app(a.data_dir, rate_start=a.rate_start, port=a.port, refuse_margin_amends=a.refuse_margin_amends), host=HOST, port=a.port, log_level="warning",
                timeout_graceful_shutdown=2)
