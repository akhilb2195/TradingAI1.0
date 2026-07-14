"""
constituent_momentum.py
------------------------
Tracks the top Nifty-50 weighted constituent stocks individually in real time.

For each stock this maintains:
  - LTP, % change (chp)
  - Volume delta per tick (incremental volume since last tick)
  - Rolling volume-surge z-score (is this stock trading abnormally heavy volume
    RIGHT NOW compared to its own recent baseline?)

Why this matters: NIFTY50-INDEX has no real traded volume of its own. But the
50 stocks that MAKE UP the index do. If a heavy-weight stock (Reliance, HDFC
Bank, etc.) suddenly sees a volume spike + directional move, that's often the
first sign of a move that will show up in the index a few seconds/ticks later.

Run this standalone to eyeball each constituent. For the combined index-level
signal, use nifty_composite_signal.py instead (it reuses the same weight table).

Run from backend/ folder, same as your other feed scripts.
"""

from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from datetime import datetime
from collections import deque
import statistics
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

# --------------------------------------------------
# Access Token
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

# --------------------------------------------------
# REST Client (not strictly needed here, kept for parity with your other scripts)
# --------------------------------------------------
fyers_rest = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path=""
)

# --------------------------------------------------
# Top Nifty-50 weighted constituents (as of Jul 10, 2026 weightage)
# Update this table whenever NSE does the semi-annual rebalance (Mar / Sep).
# --------------------------------------------------
WEIGHTS = {
    "NSE:RELIANCE-EQ":   9.19,
    "NSE:HDFCBANK-EQ":   6.59,
    "NSE:BHARTIARTL-EQ": 6.22,
    "NSE:ICICIBANK-EQ":  5.22,
    "NSE:SBIN-EQ":       4.96,
    "NSE:TCS-EQ":        3.88,
    "NSE:BAJFINANCE-EQ": 3.30,
    "NSE:LT-EQ":         2.82,
    "NSE:HINDUNILVR-EQ": 2.62,
    "NSE:SUNPHARMA-EQ":  2.41,
    "NSE:MARUTI-EQ":     2.26,
    "NSE:INFY-EQ":       2.25,
}

SYMBOLS = list(WEIGHTS.keys())

# How many volume-delta samples to keep per stock for the surge baseline
VOL_HISTORY_LEN = 30
MIN_SAMPLES_FOR_ZSCORE = 6

# --------------------------------------------------
# Per-symbol rolling state
# --------------------------------------------------
state = {
    sym: {
        "ltp": None,
        "chp": None,
        "last_volume": None,
        "vol_deltas": deque(maxlen=VOL_HISTORY_LEN),
    }
    for sym in SYMBOLS
}

HEADER = (
    f"{'Time':<10} | {'Symbol':<14} | {'Wt%':>5} | {'LTP':>10} | "
    f"{'%Chg':>8} | {'VolDelta':>10} | {'VolZ':>7} | {'Signal':<10}"
)
DIVIDER = "-" * len(HEADER)


def fmt(val, decimals=2):
    if val is None:
        return "N/A"
    try:
        return f"{val:.{decimals}f}"
    except Exception:
        return "N/A"


def classify(vol_z, chp):
    """Simple per-stock read: heavy volume + direction = early signal."""
    if vol_z is None or chp is None:
        return "-"
    if vol_z > 2.0 and chp > 0:
        return "VOL-UP"
    if vol_z > 2.0 and chp < 0:
        return "VOL-DOWN"
    return "-"


def parse_symbol(raw):
    try:
        exchange, symbol = raw.split(":", 1)
        return exchange, symbol
    except Exception:
        return "N/A", raw


print(DIVIDER)
print(HEADER)
print(DIVIDER)


def onmessage(message):
    raw_symbol = message.get("symbol", "")
    if raw_symbol not in state:
        return

    ltp = message.get("ltp")
    if ltp is None:
        return

    chp = message.get("chp")
    volume = message.get("vol_traded_today")
    ts = message.get("last_traded_time") or message.get("exch_feed_time")
    time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "N/A"

    s = state[raw_symbol]
    s["ltp"] = ltp
    s["chp"] = chp

    vol_z = None
    if volume is not None:
        if s["last_volume"] is not None:
            delta = max(volume - s["last_volume"], 0)  # guard against resets
            s["vol_deltas"].append(delta)

            if len(s["vol_deltas"]) >= MIN_SAMPLES_FOR_ZSCORE:
                mean_d = statistics.mean(s["vol_deltas"])
                std_d = statistics.pstdev(s["vol_deltas"])
                if std_d > 0:
                    vol_z = (delta - mean_d) / std_d
        s["last_volume"] = volume

    _, sym_name = parse_symbol(raw_symbol)
    weight = WEIGHTS.get(raw_symbol, 0.0)
    delta_disp = s["vol_deltas"][-1] if s["vol_deltas"] else None
    signal = classify(vol_z, chp)

    chp_str = f"{chp:+.2f}%" if chp is not None else "N/A"

    print(
        f"{time_str:<10} | {sym_name:<14} | {weight:>5.2f} | {fmt(ltp):>10} | "
        f"{chp_str:>8} | {fmt(delta_disp, 0):>10} | {fmt(vol_z):>7} | {signal:<10}"
    )


def onerror(message):
    print("Error:", message)


def onclose(message):
    print("Closed:", message)


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
    on_message=onmessage
)

if __name__ == "__main__":
    try:
        fyers.connect()
    except KeyboardInterrupt:
        print("\nStopped.")