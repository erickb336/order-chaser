# Order chaser (dry run)

The order chaser is a local tool for Kraken Pro. It places a post-only limit order at the best bid, moves it up as the bid rises, and never goes above a cap. The cap is the ask at the start. After a timeout it cancels, reads the filled quantity again, and sends one IOC (immediate-or-cancel) order at the cap for the rest.

It also chases margin orders: open a long, open a short (2x to 5x), and close a position with a reduce-only order.

**This version runs dry runs only.** It reads live public Kraken prices and simulates the fills. It takes no API key and sends no order to Kraken. A margin dry run uses a simulated account of 5,000 USD. Live trading comes in a later version.

The pages are dark. Every text keeps a contrast of 4.5:1 or more; the page tests check it.

## Run it

You need [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 and the pinned packages from `uv.lock`.

```bash
uv run order-chaser
```

Then open <http://127.0.0.1:5180>. The server listens on 127.0.0.1 only.

Options:

| Option | What it does |
| --- | --- |
| `--data-dir PATH` | Folder for the SQLite file. Default: `~/Library/Application Support/order-chaser`. The variable `ORDER_CHASER_DATA` does the same. The tool makes a missing folder with mode 0700. It never changes an existing folder, and warns if others can read it. |
| `--port N` | The port on 127.0.0.1. Default: 5180. |
| `--rate-start N` | Demo only: the estimated rate counter at start, to show the "rate limit near" state (for example 60). |
| `--refuse-margin-amends` | Demo only: the simulated exchange refuses each amend of a margin order, so that the tool cancels and replaces it. |

## Run the tests

```bash
uv run pytest -q
```

To see the rare chase states (amend rejected, disconnected, fallback, rest below the minimum, rate limit near) without waiting for the market, run `uv run python scripts/demo_states.py /tmp/oc-demo` and open <http://127.0.0.1:5180/chase>. It drives the real app with a fake feed and sample prices.

The page tests (`tests/test_pages.py`) drive the real pages in Google Chrome with a fake feed. Each test starts the app on a free port, so the tests also run while the tool runs on 5180. They need node and a one-time `npm install` in `tests/pages` (playwright-core, pinned; it downloads no browser). Without them, they skip. Set `OC_SHOTS=/some/folder` to keep their screenshots. A page test moves prices only at a look step `{"signal": name}`, when the page is at a known state; `tests/test_test_rules.py` fails on a timer, a sleep or a thread that moves prices on the wall clock.

`tests/test_margin.py` has a random-run probe: many seeded margin chases on random markets, run by the server's engine, which check every rule after each run. After each liquidation (during a chase, on the book of its last fill, and with no chase) it checks what the pages read: the server's account is the simulated account, the close list has no row of a closed position, and the result of the chase says "liquidated" and offers no close. The test runs 600 seeds. For the counts of a larger probe, run `uv run python tests/test_margin.py 5000`.

The tests need no network. A recorded sample of the public Kraken feed (`tests/fixtures/kraken-btcusd.jsonl`) checks the book checksum. A fake feed drives one dry run end to end.

## How it works

| Part | File | Job |
| --- | --- | --- |
| Core | `src/order_chaser/core.py` | `step(chase, event) -> (chase, commands)`. No I/O, no clock. All chase rules are here. |
| Simulator | `src/order_chaser/sim.py` | The dry-run gateway and the simulated margin account. It answers the core's commands like an exchange. A later version puts the Kraken gateway in its place. |
| Feed | `src/order_chaser/feed.py`, `book.py` | Public WebSocket v2 book (depth 10, CRC32 checksum) and trades; REST AssetPairs for minimums, tick size and pair status. |
| Store | `src/order_chaser/db.py` | SQLite: one row for each chase, an append-only event log, a table that maps each order id (leg) to its chase, and the simulated account. |
| Server | `src/order_chaser/server.py` | Runs the chase, serves the pages, pushes updates by server-sent events. |

Dry-run rules:

- A simulated buy at price P fills only when a public sell trade prints below P. The fill is the smaller of the rest and the trade quantity. A sell is the mirror.
- A resting simulated buy at P also fills, as maker at P, when the public ask comes to or below P, up to the size at those levels. A price level fills the order at most up to the largest size seen there. A sell is the mirror.
- If a cancel is rejected and the order is still open, the tool tries again (3 tries in total, within the rate counter). A reject for the rate limit sets the counter to 60, so the next try waits for it to fall. After 3 tries the chase ends. A live chase then tells you to check Kraken Pro; a dry run has nothing to check there.
- The price feed counts as lost after 10 s with no message (Kraken sends a heartbeat each second). The tool never amends and never sends the IOC on a book older than 10 s. Start also needs a book at most 10 s old.
- If the timeout comes while the price feed is lost, the tool cancels and sends no IOC: there is no valid price.
- A post-only place or amend at or above the best ask is rejected (sell: at or below the best bid).
- The IOC fills against the public book up to the cap.
- A limit past the price now (a higher cap, a lower floor) needs a tick box that accepts the worst extra cost. A limit at the price now (the ask for a buy, the bid for a sell) needs none.
- Fees: 0.40% maker and 0.80% taker, the highest Kraken rates.
- The rate counter copies Kraken's Starter tier (maximum 60, falls 1 each second). Above 40 the tool amends every 15 s, not 5 s.

Margin (dry run):

- "What to do" on the form: Buy or Sell (spot), Open long, Open short or Close a position (margin).
- Leverage: 2x to 5x, and only the levels that the pair allows (AssetPairs `leverage_buy`, `leverage_sell`). The form starts at 2x.
- The simulated account starts with 5,000 USD and is in the SQLite file, so a position stays open after a restart. Mixed leverage works as on Kraken: each open is its own position, with its own leverage, entry, collateral and rollover start (the fills of one chase make one position). Long and short on one pair are never open together. The close list shows one row for each pair and direction, with the count and the average leverage (cost ÷ collateral, rounded): "BTC/USD long · 2 positions · average 4x". A close takes the oldest position first (FIFO), partly if needed. One rule (`core.close_plan`) makes this plan, for the account and for every screen: the form says which positions a close takes and what stays ("Closes, oldest first: the 2x position opened 12:59 (0.0100 BTC) and 0.0050 BTC of the 4x position opened 13:00. Stays open: 0.0150 BTC at 4x."), with the margin level after it; the chase page says what the fills took so far; the result shows the profit or loss of each position that the close took. `GET /api/plan?pair=&dir=&qty=` gives the plan of a size to the form. It refuses (400) a size that `POST /api/chase` refuses for a close, with the same text: above the position, below the Kraken minimum, or too many decimals.
- Mark = the mid of the last valid public book of each pair, with its time. The tool watches one pair, so another pair shows "at the last price seen, HH:MM:SS"; the tool subscribes to no extra pair. The marks are not saved: after a restart a pair shows "no price yet" until its book arrives, and its profit or loss counts as 0 in the margin level. Margin level = equity ÷ used margin × 100. At the pair's `margin_stop` (AssetPairs, 40% today) the simulated exchange liquidates every position when a book arrives, each at the mark of its own pair, and a running chase ends.
- A liquidation always makes the tool read the account at once, also when no chase runs. The form then shows a notice: which positions closed, at what mark, their profit or loss, and when. The notice shows the last liquidation until a new one comes. When the book that liquidates also fills the rest of a chase order (the simulated exchange fills first, then marks), the chase ends as "liquidated", not "filled".
- The result page offers the close of a position only while the account has a position of that chase (`open_now`, read when the page loads). So a result after a later liquidation or a restart does not claim a position that is gone.
- Fees: opening fee and rollover at 0.05% of the cost, the stated maximum of Kraken US (0.01% to 0.05%). The pages label these numbers as estimates. Rollover counts each full 4 h after the open of each position (as Kraken: nothing before the first 4 h).
- Start needs enough free margin for orders: cost ÷ leverage. An open against an open position of the other direction is blocked: "Close the long first" (or the short).
- A close is reduce-only: the order can only make the position smaller. It starts at the whole position; you can make it smaller. A rest below the Kraken minimum gets a warning, not a block.
- The simulated exchange refuses with Kraken's texts: "Insufficient margin", "Margin allowance exceeded", "Margin position size exceeded" (AssetPairs position limits), "Cannot open opposing position", and "Reduce only:No position exists".
- If the exchange refuses an amend of a margin order, the tool cancels and replaces for the rest of the chase: cancel, wait for the confirmation, read the filled quantity of that order, place a new order for the rest at the bid. A move (a cancel or a new order) comes at most every 5 s (15 s above 40 on the rate counter). A new order goes out only when the rate counter keeps room for its cancel.
- If the exchange refuses a new order because it would cross, or for the rate limit (this sets the counter to 60), no order rests: the tool places it again after the wait, until the timeout. "Fill the rest now" and the timeout then read the filled quantity again and send one IOC at the cap for the rest; Stop ends the chase as stopped. Another refusal (for example "EOrder:Insufficient margin") ends the chase with the exchange's text.
- The result and the history judge a close by its order, not by the position. When the whole order fills, they say "Closed as asked" ("Position closed" when nothing stays), and what stays open is a plain fact: "Stays open: 0.0150 BTC at 4x". Only the part of the order that did not fill is red: "Not closed: 0.0030 BTC". The numbers and the bar use the order size.
- `POST /api/chase` takes what to do in one field, `what` (buy, sell, long, short, close-long, close-short), and `leverage` only for long and short. A request with `side`, with leverage for buy, sell or a close, or with no leverage for an open gets 400 and a message.
- Each order of a chase has its own client order id of at most 18 characters (Kraken's limit): `oc` + 12 hex for the first order, `-1`, `-2`, … for the new orders of a cancel and replace, and `-i` for the IOC. The filled quantity is the sum of what the exchange reports for each order, so a fill never counts twice.
- The tool reads the account and the positions at the start of a chase, after fills (at most every 3 s), at the end, at a liquidation, and when the form opens. It never reads them every second.

Safety:

- The server refuses a request whose Host is not `127.0.0.1:<port>` or `localhost:<port>`.
- The tool changes the watched pair at most once each second. A faster change gets 429, so that Kraken does not refuse the subscriptions. If Kraken refuses a subscription, the tool counts the feed as lost and connects again.
- No page shows inside a frame of another site (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`).
- A request that changes state needs the tool's own Origin and a session token. Only the pages that the server sends get the token.
- If the tool stops during a chase, the chase ends at the next start. The tool does not continue it.
