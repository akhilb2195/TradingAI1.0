"""
Nifty 50 constituent-weighted terminal tracker.

Instead of watching the NIFTY50-INDEX symbol directly, this subscribes to
the individual heavyweight stocks, tracks each one's live % change, and
computes a WEIGHTED composite score using their actual index weightage.
That weighted score is your own DIY "is Nifty going up or down" indicator,
built the same way NSE builds the real index -- just on fewer stocks.

Run:
    python weighted_tracker.py
"""

import os
import sys
from datetime import datetime

from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id  # noqa: E402

# --------------------------------------------------
# Weights (%) -- source: NSE Indices official factsheet, June 30 2026
# Stocks marked (est.) use third-party free-float estimates since NSE
# only publishes the top 10 for free. Update these on each semi-annual
# rebalance (Jan 31 / Jul 31).
# --------------------------------------------------
WEIGHTS = {
    "NSE:HDFCBANK-EQ":   11.18,  # official
    "NSE:ICICIBANK-EQ":   9.01,  # official
    "NSE:RELIANCE-EQ":    8.00,  # official
    "NSE:BHARTIARTL-EQ":  5.15,  # official
    "NSE:LT-EQ":           4.44,  # official
    "NSE:SBIN-EQ":         3.88,  # official
    "NSE:AXISBANK-EQ":     3.54,  # official
    "NSE:INFY-EQ":         3.21,  # official
    "NSE:KOTAKBANK-EQ":    2.64,  # official
    "NSE:ITC-EQ":          2.53,  # official
    "NSE:TCS-EQ":          2.20,  # est.
    "NSE:BAJFINANCE-EQ":   2.10,  # est.
    "NSE:HINDUNILVR-EQ":   1.90,  # est.
    "NSE:SUNPHARMA-EQ":    1.70,  # est.
    "NSE:MARUTI-EQ":       1.60,  # est.
    "NSE:TITAN-EQ":        1.50,  # est.
    "NSE:MM-EQ":           1.40,  # est. (Mahindra & Mahindra)
    "NSE:NTPC-EQ":         1.30,  # est.
    "NSE:ULTRACEMCO-EQ":   1.25,  # est.
    "NSE:HCLTECH-EQ":      1.20,  # est.
}

TOTAL_WEIGHT_COVERED = sum(WEIGHTS.values())  # how much of the real index this represents
SYMBOLS = list(WEIGHTS.keys())

BAR_WIDTH = 24

# --------------------------------------------------
# ANSI colors
# --------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
GRAY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"
CLEAR_SCREEN = "\033[2J\033[H"

# --------------------------------------------------
# Access Token
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

fyers_rest = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path="",
)

# --------------------------------------------------
# State
# --------------------------------------------------
latest = {}  # symbol -> tick dict


def fmt(n, d=2):
    if n is None:
        return "N/A"
    try:
        return f"{n:,.{d}f}"
    except Exception:
        return "N/A"


def bar(pct_of_range, width=BAR_WIDTH, color=GREEN):
    pct_of_range = max(0, min(100, pct_of_range))
    filled = int(width * pct_of_range / 100)
    return f"{color}{'#' * filled}{GRAY}{'-' * (width - filled)}{RESET}"


def compute_weighted_score():
    """
    Weighted composite = sum(weight * chp) / total_weight_covered.
    This mirrors how the real index reacts -- a stock's % move only
    matters as much as its weight -- just scoped to the stocks you're
    subscribed to.
    """
    total = 0.0
    covered = 0.0
    for symbol, t in latest.items():
        if t["chp"] is None:
            continue
        w = WEIGHTS.get(symbol, 0)
        total += w * t["chp"]
        covered += w
    if covered == 0:
        return None
    return total / covered  # weighted average % change


def render():
    lines = [
        f"{BOLD}{CYAN}NIFTY 50 -- WEIGHTED CONSTITUENT TRACKER{RESET}   "
        f"{GRAY}{datetime.now().strftime('%H:%M:%S')}{RESET}",
        f"{GRAY}Tracking {len(WEIGHTS)} stocks, covering {fmt(TOTAL_WEIGHT_COVERED, 1)}% of real index weight{RESET}",
        "",
    ]

    # sort by weight, heaviest first
    ordered = sorted(WEIGHTS.items(), key=lambda kv: -kv[1])

    up_count = 0
    down_count = 0

    for symbol, weight in ordered:
        t = latest.get(symbol)
        name = symbol.replace("NSE:", "").replace("-EQ", "")
        if not t:
            lines.append(f"  {name:<12} {GRAY}waiting...{RESET}")
            continue

        up = t["direction"] == "up"
        color = GREEN if up else RED
        arrow = "▲" if up else "▼"
        if up:
            up_count += 1
        else:
            down_count += 1

        # bar sized by |chp|, capped at 3% move = full bar
        pct_of_range = min(100, abs(t["chp"] or 0) / 3 * 100)
        lines.append(
            f"  {BOLD}{name:<12}{RESET} "
            f"w={fmt(weight,1):>5}%  "
            f"{color}{arrow} {fmt(t['chp']):>6}%{RESET}  "
            f"[{bar(pct_of_range, color=color)}]  "
            f"LTP {fmt(t['ltp'])}"
        )

    lines.append("")
    score = compute_weighted_score()
    if score is not None:
        up = score >= 0
        color = GREEN if up else RED
        arrow = "▲" if up else "▼"
        lines.append(f"{BOLD}Breadth{RESET}   {GREEN}{up_count} up{RESET}  /  {RED}{down_count} down{RESET}")
        lines.append(
            f"{BOLD}Weighted composite move{RESET}   {BOLD}{color}{arrow} {fmt(score)}%{RESET}   "
            f"{GRAY}(estimate -- Nifty likely {'up' if up else 'down'} directionally){RESET}"
        )
    else:
        lines.append(f"{GRAY}Computing composite...{RESET}")

    sys.stdout.write(CLEAR_SCREEN + "\n".join(lines) + "\n")
    sys.stdout.flush()


# --------------------------------------------------
# WebSocket callbacks
# --------------------------------------------------
def onmessage(message):
    ltp = message.get("ltp")
    if ltp is None:
        return

    raw_symbol = message.get("symbol", "")
    prev = message.get("prev_close_price")
    ch = message.get("ch")
    chp = message.get("chp")

    if ch is None and prev:
        ch = ltp - prev
    if chp is None and prev and ch is not None:
        chp = (ch / prev) * 100

    latest[raw_symbol] = {
        "ltp": ltp,
        "ch": ch,
        "chp": chp,
        "direction": "up" if (ch or 0) >= 0 else "down",
    }
    render()


def onerror(message):
    print(f"{RED}Error:{RESET}", message)


def onclose(message):
    print(f"{GRAY}Closed:{RESET}", message)


def onopen():
    fyers.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
    fyers.keep_running()


fyers = data_ws.FyersDataSocket(
    access_token=access_token,
    log_path="",
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=onopen,
    on_close=onclose,
    on_error=onerror,
    on_message=onmessage,
)


if __name__ == "__main__":
    try:
        fyers.connect()
    except KeyboardInterrupt:
        print("\nStopped.")