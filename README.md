# Order chaser (dry run)

The order chaser is a local tool for Kraken Pro. It places a post-only limit order at the best bid, moves it up as the bid rises, and never goes above a cap. The cap is the ask at the start. After a timeout it cancels, reads the filled quantity again, and sends one IOC (immediate-or-cancel) order at the cap for the rest.

**This version runs dry runs only.** It reads live public Kraken prices and simulates the fills. It takes no API key and sends no order to Kraken. Live trading comes in a later version.

## Run it

You need [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 and the pinned packages from `uv.lock`.

```bash
uv run order-chaser
```

Then open <http://127.0.0.1:5180>. The server listens on 127.0.0.1:5180 only.

Options:

| Option | What it does |
| --- | --- |
| `--data-dir PATH` | Folder for the SQLite file. Default: `~/Library/Application Support/order-chaser`. The variable `ORDER_CHASER_DATA` does the same. The tool makes a missing folder with mode 0700. It never changes an existing folder, and warns if others can read it. |
| `--rate-start N` | Demo only: the estimated rate counter at start, to show the "rate limit near" state (for example 60). |

## Run the tests

```bash
uv run pytest -q
```

To see the rare chase states (amend rejected, disconnected, fallback, rest below the minimum, rate limit near) without waiting for the market, run `uv run python scripts/demo_states.py /tmp/oc-demo` and open <http://127.0.0.1:5180/chase>. It drives the real app with a fake feed and sample prices.

The page tests (`tests/test_pages.py`) drive the real pages in Google Chrome on 127.0.0.1:5180 with a fake feed. They need node and a one-time `npm install` in `tests/pages` (playwright-core, pinned; it downloads no browser). Without them, they skip. Set `OC_SHOTS=/some/folder` to keep their screenshots.

The tests need no network. A recorded sample of the public Kraken feed (`tests/fixtures/kraken-btcusd.jsonl`) checks the book checksum. A fake feed drives one dry run end to end.

## How it works

| Part | File | Job |
| --- | --- | --- |
| Core | `src/order_chaser/core.py` | `step(chase, event) -> (chase, commands)`. No I/O, no clock. All chase rules are here. |
| Simulator | `src/order_chaser/sim.py` | The dry-run gateway. It answers the core's commands like an exchange. A later version puts the Kraken gateway in its place. |
| Feed | `src/order_chaser/feed.py`, `book.py` | Public WebSocket v2 book (depth 10, CRC32 checksum) and trades; REST AssetPairs for minimums, tick size and pair status. |
| Store | `src/order_chaser/db.py` | SQLite: one row for each chase and an append-only event log, keyed by the client order id. |
| Server | `src/order_chaser/server.py` | Runs the chase, serves the pages, pushes updates by server-sent events. |

Dry-run rules:

- A simulated buy at price P fills only when a public sell trade prints below P. The fill is the smaller of the rest and the trade quantity. A sell is the mirror.
- A resting simulated buy at P also fills, as maker at P, when the public ask comes to or below P, up to the size at those levels. A price level fills the order at most up to the largest size seen there. A sell is the mirror.
- If a cancel is rejected and the order is still open, the tool tries again (3 tries in total, within the rate counter). A reject for the rate limit sets the counter to 60, so the next try waits for it to fall. After 3 tries the chase ends. A live chase then tells you to check Kraken Pro; a dry run has nothing to check there.
- The price feed counts as lost after 10 s with no message (Kraken sends a heartbeat each second). The tool never amends and never sends the IOC on a book older than 10 s. Start also needs a book at most 10 s old.
- If the timeout comes while the price feed is lost, the tool cancels and sends no IOC: there is no valid price.
- A post-only place or amend at or above the best ask is rejected (sell: at or below the best bid).
- The IOC fills against the public book up to the cap.
- Fees: 0.40% maker and 0.80% taker, the highest Kraken rates.
- The rate counter copies Kraken's Starter tier (maximum 60, falls 1 each second). Above 40 the tool amends every 15 s, not 5 s.

Safety:

- The server refuses a request whose Host is not `127.0.0.1:5180` or `localhost:5180`.
- No page shows inside a frame of another site (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`).
- A request that changes state needs the tool's own Origin and a session token. Only the pages that the server sends get the token.
- If the tool stops during a chase, the chase ends at the next start. The tool does not continue it.
