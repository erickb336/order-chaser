"""Demo: serve the real app on 127.0.0.1:5180 and drive it with a FAKE public feed, so that the
rare chase states show for a few seconds each. All prices here are sample data.

    uv run python scripts/demo_states.py DATA_DIR

Order of states: placing, resting, amending, partial fill, amend rejected, disconnected, fallback,
rest not filled; then a second chase: rate limit near, rest below the minimum.
"""
import asyncio
import json
import sys
import zlib
from decimal import Decimal as D
from pathlib import Path

import uvicorn

from order_chaser import feed
from order_chaser.db import lock_folder
from order_chaser.server import create_app

PAIRS = feed.parse_pairs({"BTC/USD": {"tick_size": "0.1", "ordermin": "0.00005", "costmin": "0.5",
                                      "pair_decimals": 1, "lot_decimals": 8, "status": "online"}})


class FakeWs:
    async def send(self, msg):
        pass


class FakeBook:
    def __init__(self):
        self.bids, self.asks = {}, {}

    def msg(self, kind, bids, asks):
        if kind == "snapshot":
            self.bids, self.asks = {}, {}
        for side, levels in ((self.bids, bids), (self.asks, asks)):
            for p, q in levels:
                side.pop(D(p), None) if D(q) == 0 else side.__setitem__(D(p), D(q))
        f = lambda v, d: f"{D(v):.{d}f}".replace(".", "").lstrip("0")
        s = "".join(f(p, 1) + f(q, 8) for p, q in sorted(self.asks.items())[:10])
        s += "".join(f(p, 1) + f(q, 8) for p, q in sorted(self.bids.items(), reverse=True)[:10])
        return {"channel": "book", "type": kind, "data": [{"symbol": "BTC/USD", "checksum": zlib.crc32(s.encode()),
                "bids": [{"price": D(p), "qty": D(q)} for p, q in bids], "asks": [{"price": D(p), "qty": D(q)} for p, q in asks]}]}


def trade(side, price, qty):
    return {"channel": "trade", "type": "update", "data": [{"symbol": "BTC/USD", "side": side, "price": D(price), "qty": D(qty)}]}


async def drive(eng, speed: float = 1.0):
    """speed > 1 runs the demo faster; the tests use it with a clock that runs as fast."""
    wait = lambda s: asyncio.sleep(s / speed)
    f = feed.PublicFeed(eng.on_book, eng.on_trade, eng.on_link)
    f.ws = FakeWs()
    eng.feed, eng.pairs = f, PAIRS
    await f.watch(PAIRS["BTC/USD"])
    eng.on_link(True, 0, 0)
    kb = FakeBook()
    say = lambda m: print(m, flush=True)
    link_up = [True]

    async def heartbeat():   # Kraken sends one each second while the link is up; it keeps the book valid
        while True:
            if link_up[0]:
                await f._handle({"channel": "heartbeat"})
            await wait(1)
    beats = asyncio.create_task(heartbeat())  # noqa: F841  (runs until the demo ends)

    await f._handle(kb.msg("snapshot", [("62417.9", "1.0"), ("62417.0", "2.0")], [("62418.5", "0.01"), ("62419.5", "3.0")]))
    await wait(8)                              # time to open the page

    eng.latency = 3 / speed
    say("start"); eng.start("BTC/USD", "buy", D("0.05"), None, 120)        # placing (3 s), then resting
    await wait(8)
    say("amend"); await f._handle(kb.msg("update", [("62418.1", "0.4")], []))  # amending (3 s)
    await wait(5)
    say("fill"); await f._handle(trade("sell", "62418.0", "0.018"))            # partial fill
    await wait(5)
    say("reject")
    await f._handle(kb.msg("update", [("62418.3", "0.2")], []))               # amend to 62418.3 goes out...
    await wait(0.5)
    await f._handle(kb.msg("update", [("62418.3", "0")], [("62418.3", "0.05")]))  # ...a seller takes that bid, the ask falls to it: rejected
    await wait(9)
    say("disconnect"); link_up[0] = False; eng.on_link(False, 3, 4)           # disconnected
    await wait(7)
    say("reconnect"); link_up[0] = True; eng.on_link(True, 0, 0)
    await f._handle(kb.msg("snapshot", [("62418.1", "1.0")], [("62418.4", "0.01"), ("62431.0", "3.0")]))
    await wait(7)
    eng.latency = 2.5 / speed
    say("fallback"); eng.user("fillnow")                                       # cancel, re-read, IOC: about 8 s
    await wait(12)

    eng.latency = 0.15 / speed
    eng.rates["BTC/USD"] = (60.0, eng.clock())                                  # second chase: counter near the maximum
    await f._handle(kb.msg("snapshot", [("62417.9", "1.0")], [("62418.5", "1.0")]))
    say("start 2"); eng.start("BTC/USD", "buy", D("0.05"), None, 60)            # rate limit near
    await wait(8)
    say("fill 2"); await f._handle(trade("sell", "62417.0", "0.04996"))        # the rest, 0.00004, is below ordermin
    await wait(4)
    say("fill now 2"); eng.user("fillnow")                                     # rest below the minimum (the heartbeats keep the book valid)
    await wait(600)


async def main(data_dir: Path):
    lock = lock_folder(data_dir)  # noqa: F841  (makes the folder; held until the demo ends)
    guard = create_app(data_dir, connect=False)
    server = uvicorn.Server(uvicorn.Config(guard, host="127.0.0.1", port=5180, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    await drive(guard.app.state.engine)
    await task


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
