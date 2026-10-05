"""Draw the README graphics in a light and a dark version, and check their contrast.

Run from the repository root:  python3 scripts/graphics.py
It writes hero, walkthrough, states and margin, each as -light.svg and -dark.svg,
and stops with an error if a text colour is below 4.5:1 on a background it sits on.
All prices are sample data. The script uses only the Python standard library.
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"

THEMES = {
    "dark": dict(bg="#0f1115", card="#191c22", line="#3a404b", fg="#e8eaee", muted="#a7aebb",
                 bid="#5ee08f", ask="#ff8a80", order="#8fb6ff", cap="#f6bd5a", ioc="#c5b3ff"),
    "light": dict(bg="#ffffff", card="#f3f5f8", line="#c4cad4", fg="#14171c", muted="#4a5260",
                  bid="#0f7a3a", ask="#b42318", order="#1f4fd1", cap="#8a5300", ioc="#5b3fd1"),
}
TEXT_TOKENS = ("fg", "muted", "bid", "ask", "order", "cap", "ioc")
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def _lum(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check_contrast():
    rows = []
    for name, t in THEMES.items():
        for token in TEXT_TOKENS:
            for ground in ("bg", "card"):
                ratio = contrast(t[token], t[ground])
                rows.append((name, token, ground, ratio))
                if ratio < 4.5:
                    raise SystemExit(f"{name}: {token} on {ground} is {ratio:.2f}:1, below 4.5:1")
    return rows


class Svg:
    def __init__(self, t, w, h, title, desc):
        self.t, self.w, self.h = t, w, h
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-labelledby="title desc">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
            '<defs>' + "".join(
                f'<marker id="arrow-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{t[k]}"/></marker>'
                for k in ("muted", "order", "ioc", "ask", "cap")) + '</defs>',
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{t["bg"]}" stroke="{t["line"]}"/>',
        ]

    def text(self, x, y, s, colour="fg", size=14, weight=400, anchor="start", mono=False):
        self.parts.append(
            f'<text x="{x}" y="{y}" fill="{self.t[colour]}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" font-family="{MONO if mono else SANS}">{escape(s)}</text>')

    def rect(self, x, y, w, h, stroke="line", fill="card", rx=10, dash=None, width=1.2):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{self.t[fill]}" '
                          f'stroke="{self.t[stroke]}" stroke-width="{width}"{d}/>')

    def line(self, x1, y1, x2, y2, colour="muted", width=1.5, dash=None, arrow=False):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#arrow-{colour})"' if arrow else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{self.t[colour]}" '
                          f'stroke-width="{width}"{d}{m}/>')

    def path(self, d, colour="muted", width=1.5, dash=None, arrow=False, fill="none"):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#arrow-{colour})"' if arrow else ""
        self.parts.append(f'<path d="{d}" fill="{fill}" stroke="{self.t[colour]}" stroke-width="{width}" '
                          f'stroke-linejoin="round"{da}{m}/>')

    def circle(self, x, y, r, colour, fill=None):
        f = self.t[fill] if fill else self.t[colour]
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{f}" stroke="{self.t[colour]}" stroke-width="2"/>')

    def badge(self, x, y, n, colour="order"):
        self.circle(x, y, 13, colour, fill="card")
        self.text(x, y + 5, str(n), colour, 14, 700, "middle")

    def save(self, name):
        (OUT / name).write_text("\n".join(self.parts + ["</svg>"]) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- hero: one chase on a price chart
HERO_ALT = ("A chase of a buy order of 0.0500 BTC on BTC/USD, drawn with sample data. The cap is the ask at the "
            "start, 62,420.00. The tool places a post-only buy at the best bid, 62,400.00, and moves it up as the "
            "bid rises, never above the cap. At 1:20, 0.0300 BTC fills at 62,413.00 (simulated). At the 2:00 "
            "timeout the tool cancels, reads the filled quantity again, and sends one IOC for the rest, 0.0200 BTC, "
            "at the cap. The IOC fills at the ask, 62,418.00.")


def hero(t):
    s = Svg(t, 960, 470, "Order chaser: one chase, sample data", HERO_ALT)
    s.text(32, 46, "Order chaser", "fg", 26, 700)
    s.text(32, 72, "One chase of a buy order, 0.0500 BTC on BTC/USD. Dry run, sample data.", "muted", 15)

    x0, x1 = 110, 690          # 0:00 .. 2:00
    lo, hi, ytop, ybot = 62396, 62424, 112, 352

    def X(sec):
        return x0 + (x1 - x0) * sec / 120

    def Y(price):
        return ybot - (ybot - ytop) * (price - lo) / (hi - lo)

    # axes and grid
    for p in (62400, 62410, 62420):
        s.line(x0 - 6, Y(p), 900, Y(p), "line", 1)
        s.text(x0 - 12, Y(p) + 4, f"{p:,}", "muted", 12, anchor="end", mono=True)
    for sec, lab in ((0, "0:00"), (60, "1:00"), (120, "2:00")):
        s.text(X(sec), ybot + 22, lab, "muted", 12, anchor="middle", mono=True)
    s.line(x0, ybot, 900, ybot, "line", 1)

    # cap
    s.line(x0, Y(62420), 900, Y(62420), "cap", 2, dash="7 5")
    s.text(x0 + 4, Y(62420) + 19, "cap 62,420.00 = the ask at the start. The order never goes above it.", "cap", 13, 600)

    # ask and bid
    ask = [(0, 62420), (25, 62421), (50, 62422), (75, 62421), (100, 62419), (120, 62418), (150, 62418)]
    bid = [(0, 62400), (20, 62404), (45, 62409), (70, 62413), (95, 62416), (120, 62416), (150, 62416)]

    def stepped(points, x_of=X):
        d = f"M{x_of(points[0][0]):.1f},{Y(points[0][1]):.1f}"
        for (sec, p) in points[1:]:
            d += f" H{x_of(sec):.1f} V{Y(p):.1f}"
        return d

    s.path(stepped(ask), "ask", 1.6)
    s.path(stepped(bid), "bid", 1.6)
    s.text(X(150) + 6, Y(62418) + 4, "best ask", "ask", 12)
    s.text(X(150) + 6, Y(62416) + 18, "best bid", "bid", 12)

    # the order: follows the bid with a short delay, stops at 2:00
    order = [(0, 62400), (21, 62404), (46, 62409), (71, 62413), (96, 62416), (120, 62416)]
    s.path(stepped(order), "order", 4)
    s.text(X(4), Y(62400) + 22, "your order: post-only at the best bid", "order", 13, 600)

    # fill
    fx, fy = X(80), Y(62413)
    s.circle(fx, fy, 6, "bid")
    s.line(fx, fy + 8, fx, fy + 46, "bid", 1.2)
    s.text(fx, fy + 62, "fill 0.0300 BTC at 62,413.00", "bid", 13, 600, "middle")

    # timeout and IOC
    s.line(X(120), ytop - 6, X(120), ybot, "muted", 1.5, dash="4 4")
    s.text(X(120), ytop - 12, "timeout 2:00", "fg", 13, 700, "middle")
    ix = 750
    s.path(f"M{ix - 9},{Y(62420)} L{ix},{Y(62420) - 9} L{ix + 9},{Y(62420)} L{ix},{Y(62420) + 9} z",
           "ioc", 2, fill=t["card"])
    s.line(ix, Y(62420) + 10, ix, Y(62418) - 4, "ioc", 2, arrow=True)
    s.text(704, Y(62412) + 6, "IOC 0.0200 BTC at the cap", "ioc", 13, 700)
    s.text(704, Y(62412) + 24, "fills at the ask, 62,418.00", "ioc", 13, 400)

    # four steps
    steps = [("order", "Place post-only", "at the best bid"),
             ("order", "Move up with the bid", "never above the cap"),
             ("fg", "Timeout: cancel", "read the fill again"),
             ("ioc", "One IOC at the cap", "for the rest")]
    for i, (c, a, b) in enumerate(steps):
        bx = 32 + i * 228
        s.rect(bx, 398, 214, 52)
        s.badge(bx + 24, 424, i + 1, c)
        s.text(bx + 46, 419, a, "fg", 14, 600)
        s.text(bx + 46, 438, b, "muted", 13)
    return s


# ---------------------------------------------------------------- walkthrough: one chase from form to result
WALK = [
    ("order", "Fill in the form", "Buy · BTC/USD · 0.0500 BTC · timeout 2 min · Dry run.", "Press Start dry run."),
    ("cap", "The tool records the cap", "The ask at the start is 62,420.00. That is the cap.",
     "Worst case: never more than a market order at the start."),
    ("order", "The order chases the bid", "Post-only buy at 62,400.00. The bid rises; the tool amends,",
     "at most every 5 s: 62,404.00, 62,409.00, 62,413.00, never above the cap."),
    ("bid", "A part fills", "A public sell trade prints below 62,413.00.",
     "Simulated fill: 0.0300 BTC at 62,413.00 as maker. 0.0200 BTC rests."),
    ("ioc", "Timeout: cancel, read again, IOC", "2:00: cancel, confirmed. Filled read again: 0.0300 BTC.",
     "One IOC, 0.0200 BTC at the cap 62,420.00. It fills at the ask, 62,418.00."),
    ("bid", "The result", "Filled 0.0500 of 0.0500 BTC. Average 62,415.00 before fees.",
     "History keeps the chase. Every number above is simulated."),
]
WALK_ALT = ("Learn it in 5 minutes: one dry-run chase with sample data, in six numbered steps. "
            + " ".join(f"{i + 1}. {a}: {b} {c}" for i, (_, a, b, c) in enumerate(WALK)))


def walkthrough(t):
    h = 86 + len(WALK) * 78 + 14
    s = Svg(t, 960, h, "Learn order chaser in 5 minutes", WALK_ALT)
    s.text(32, 44, "One chase, step by step", "fg", 22, 700)
    s.text(32, 68, "Dry run. Sample data: the prices and fills are made up for this example.", "muted", 14)
    for i, (c, a, b, d) in enumerate(WALK):
        y = 86 + i * 78
        s.rect(32, y, 896, 66)
        if i < len(WALK) - 1:
            s.line(62, y + 47, 62, y + 78 + 19, "line", 2)
        s.badge(62, y + 33, i + 1, c)
        s.text(92, y + 26, a, "fg", 16, 700)
        s.text(92, y + 45, b, "muted", 14)
        s.text(92, y + 61, d, "muted", 14)
    return s


# ---------------------------------------------------------------- chase states
STATES_ALT = ("The chase states. Placing goes to resting. Resting and amending go back and forth while the bid "
              "moves. An amend that Kraken rejects goes to reconcile, which reads the order and goes back to "
              "resting. No message for 10 s makes the feed lost; when the feed comes back, reconcile reads the "
              "order again. A timeout, Fill the rest now, or Stop goes to cancelling. After a timeout or Fill the "
              "rest now: reread the filled quantity, send one IOC at the cap, then done. Stop goes from "
              "cancelling to done, and so does a cancel rejected 3 times. A timeout while the feed is lost "
              "cancels and sends no IOC. Done has one outcome: filled, not filled, stopped, liquidated or ended.")


def states(t):
    s = Svg(t, 960, 560, "Chase states", STATES_ALT)
    s.text(32, 44, "Chase states", "fg", 22, 700)
    s.text(32, 68, "From src/order_chaser/core.py. One step(chase, event) function moves a chase.", "muted", 14)
    W, H = 150, 52
    box = {
        "placing": (40, 110), "resting": (290, 110), "amending": (560, 110),
        "feed lost": (290, 260), "reconcile": (560, 260),
        "cancelling": (290, 410), "reread": (520, 410), "ioc": (740, 410),
    }
    sub = {"placing": "post-only order", "resting": "at the bid, ≤ cap", "amending": "move to the new bid",
           "feed lost": "no message 10 s", "reconcile": "read the order", "cancelling": "cancel, wait",
           "reread": "read the fill again", "ioc": "one IOC at the cap"}
    colour = {"ioc": "ioc", "feed lost": "ask", "reconcile": "cap"}

    def c(name, dx=0.5, dy=0.5):
        x, y = box[name]
        return x + W * dx, y + H * dy

    def arrow(a, b, label="", lx=0, ly=0, ad=(1, .5), bd=(0, .5), col="muted", anchor="middle"):
        x1, y1 = c(a, *ad)
        x2, y2 = c(b, *bd)
        s.line(x1, y1, x2, y2, col, 1.6, arrow=True)
        if label:
            s.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, "muted", 12, anchor=anchor)

    arrow("placing", "resting", "placed", ly=-8)
    x1, y1 = c("resting", 1, .35); x2, _ = c("amending", 0, .35)
    s.line(x1, y1, x2, y1, "muted", 1.6, arrow=True)
    s.text((x1 + x2) / 2, y1 - 8, "bid moves", "muted", 12, anchor="middle")
    x1, y1 = c("amending", 0, .7); x2, _ = c("resting", 1, .7)
    s.line(x1, y1, x2, y1, "muted", 1.6, arrow=True)
    s.text((x1 + x2) / 2, y1 + 18, "amended", "muted", 12, anchor="middle")
    arrow("amending", "reconcile", "amend rejected", lx=8, ad=(.5, 1), bd=(.5, 0), anchor="start")
    arrow("resting", "feed lost", "", ad=(.5, 1), bd=(.5, 0))
    s.text(c("resting")[0] - 8, 236, "no message 10 s", "muted", 12, anchor="end")
    arrow("feed lost", "reconcile", "feed back", ly=-8)
    # reconcile -> resting (diagonal)
    arrow("reconcile", "resting", "", ad=(0, .1), bd=(.8, 1))
    s.text(472, 222, "order read", "muted", 12, anchor="middle")
    # resting -> cancelling along the left
    x, y = c("resting", 0, .8)
    s.path(f"M{x},{y} H230 V{c('cancelling')[1]} H{box['cancelling'][0]}", "muted", 1.6, arrow=True)
    s.text(222, 330, "timeout,", "muted", 12, anchor="end")
    s.text(222, 346, "Fill the rest now,", "muted", 12, anchor="end")
    s.text(222, 362, "or Stop", "muted", 12, anchor="end")
    arrow("feed lost", "cancelling", "", ad=(.5, 1), bd=(.5, 0))
    s.text(c("feed lost")[0] + 8, 386, "timeout: no IOC", "muted", 12)
    arrow("cancelling", "reread", "cancelled", ly=-8)
    arrow("reread", "ioc", "rest to fill", ly=-8)

    # done
    dx, dy, dw, dh = 290, 490, 600, 52
    s.rect(dx, dy, dw, dh, stroke="fg", width=1.6)
    s.text(dx + 16, dy + 22, "done", "fg", 15, 700)
    s.text(dx + 16, dy + 41, "outcome: filled · not filled · stopped · liquidated · ended (tool restart)", "muted", 13)
    s.line(c("cancelling", .5, 1)[0], c("cancelling", .5, 1)[1], c("cancelling", .5, 1)[0], dy, "muted", 1.6, arrow=True)
    s.text(c("cancelling")[0] - 8, 480, "Stop, or cancel rejected 3 times", "muted", 12, anchor="end")
    s.line(c("reread", .5, 1)[0], c("reread", .5, 1)[1], c("reread", .5, 1)[0], dy, "muted", 1.6, arrow=True)
    s.text(c("reread")[0] + 8, 480, "nothing to send", "muted", 12)
    s.line(c("ioc", .5, 1)[0], c("ioc", .5, 1)[1], c("ioc", .5, 1)[0], dy, "ioc", 1.6, arrow=True)

    for name, (x, y) in box.items():
        col = colour.get(name, "order")
        s.rect(x, y, W, H, stroke=col, width=1.6)
        s.text(x + 14, y + 22, name, col, 15, 700)
        s.text(x + 14, y + 41, sub[name], "muted", 12)
    s.text(780, 128, "Margin: if Kraken refuses", "muted", 12)
    s.text(780, 144, "an amend, the tool cancels", "muted", 12)
    s.text(780, 160, "and places a new order", "muted", 12)
    s.text(780, 176, "for the rest instead.", "muted", 12)
    return s


# ---------------------------------------------------------------- margin flow
MARGIN_ALT = ("The margin flow of the dry run, with sample data. 1: open a long or a short at 2x to 5x with a "
              "chase. 2: each open is its own position in the simulated account of 5,000 USD, with its own "
              "leverage, entry and collateral. 3: close with a reduce-only order, oldest position first (FIFO). "
              "Example: positions of 0.0100 BTC at 2x and 0.0200 BTC at 4x; a close of 0.0150 BTC takes all of "
              "the 2x position and 0.0050 BTC of the 4x position; 0.0150 BTC at 4x stays open. Below: the margin "
              "level, equity divided by used margin times 100. Kraken calls margin at 80%. At the pair's margin "
              "stop, 40% today, the simulated exchange liquidates every position at the mark and a running "
              "chase ends.")


def margin(t):
    s = Svg(t, 960, 540, "Margin flow", MARGIN_ALT)
    s.text(32, 44, "Margin in the dry run", "fg", 22, 700)
    s.text(32, 68, "Simulated account of 5,000 USD. Sample data. No order goes to Kraken.", "muted", 14)
    cards = [("order", "Open long or short", ["2x to 5x, only the levels", "the pair allows.", "A chase opens it."]),
             ("cap", "Positions", ["Each open is its own position:", "own leverage, entry, collateral.",
                                   "Never long and short together."]),
             ("bid", "Close a position", ["Reduce-only: it can only make", "the position smaller.",
                                          "Oldest position first (FIFO)."])]
    for i, (c, title, lines) in enumerate(cards):
        x = 32 + i * 304
        s.rect(x, 90, 288, 112, stroke=c, width=1.6)
        s.badge(x + 26, 118, i + 1, c)
        s.text(x + 48, 123, title, "fg", 16, 700)
        for j, ln in enumerate(lines):
            s.text(x + 18, 152 + j * 19, ln, "muted", 13)
        if i < 2:
            s.line(x + 290, 146, x + 302, 146, "muted", 1.6, arrow=True)

    # FIFO bar
    s.text(32, 236, "A close of 0.0150 BTC, oldest first", "fg", 15, 700)
    bx, bw, by = 32, 600, 250
    unit = bw / 0.0300
    w2 = 0.0100 * unit
    w4a = 0.0050 * unit
    s.rect(bx, by, w2, 34, stroke="bid", fill="card", rx=4, width=2)
    s.rect(bx + w2, by, w4a, 34, stroke="bid", fill="card", rx=4, width=2)
    s.rect(bx + w2 + w4a, by, bw - w2 - w4a, 34, stroke="cap", fill="card", rx=4, width=2, dash="6 4")
    s.text(bx + w2 / 2, by + 22, "2x · 0.0100 · 12:59", "bid", 13, 600, "middle")
    s.text(bx + w2 + w4a / 2, by + 22, "4x · 0.0050", "bid", 13, 600, "middle")
    s.text(bx + w2 + w4a + (bw - w2 - w4a) / 2, by + 22, "4x · 0.0150 · 13:00", "cap", 13, 600, "middle")
    s.text(bx, by + 56, "closes (reduce-only)", "bid", 13, 600)
    s.text(bx + bw, by + 56, "stays open: 0.0150 BTC at 4x", "cap", 13, 600, "end")
    s.text(660, by + 14, "The form shows this plan", "muted", 13)
    s.text(660, by + 32, "before you start, with the", "muted", 13)
    s.text(660, by + 50, "margin level after the close.", "muted", 13)

    # margin level gauge
    s.text(32, 352, "Margin level = equity ÷ used margin × 100", "fg", 15, 700)
    gx, gw, gy = 32, 896, 392
    vmax = 250

    def G(v):
        return gx + gw * min(v, vmax) / vmax

    s.rect(gx, gy, G(40) - gx, 12, stroke="ask", fill="ask", rx=3)
    s.rect(G(40), gy, G(80) - G(40), 12, stroke="cap", fill="cap", rx=0)
    s.rect(G(80), gy, gx + gw - G(80), 12, stroke="bid", fill="bid", rx=3)
    for v, lab, col in ((40, "40% margin stop: liquidation", "ask"), (80, "80% margin call", "cap")):
        s.line(G(v), gy - 10, G(v), gy + 22, "fg", 2)
    s.text(G(40), gy + 40, "40% margin stop", "ask", 13, 700, "middle")
    s.text(G(80) + 6, gy - 14, "80% margin call", "cap", 13, 700)
    s.circle(G(250), gy + 6, 7, "fg", fill="card")
    s.text(G(250), gy - 14, "sample: 811%", "fg", 13, 600, "end")
    s.text(32, 470, "At the margin stop (the pair's margin_stop, 40% today), the simulated exchange", "fg", 14)
    s.text(32, 490, "liquidates every position at the mark of its pair. A running chase ends as liquidated.", "fg", 14)
    s.text(32, 516, "Mark = the mid of the last valid public book. The form shows a notice of each liquidation.",
           "muted", 13)
    return s


def main():
    rows = check_contrast()
    for theme in THEMES:
        t = THEMES[theme]
        for name, fn in (("hero", hero), ("walkthrough", walkthrough), ("states", states), ("margin", margin)):
            fn(t).save(f"{name}-{theme}.svg")
    worst = min(rows, key=lambda r: r[3])
    print(f"wrote 8 SVGs; lowest text contrast {worst[3]:.2f}:1 ({worst[0]} {worst[1]} on {worst[2]})")


if __name__ == "__main__":
    main()
