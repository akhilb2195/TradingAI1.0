"""
nifty_composite_signal.py
---------------------------
Combines price momentum AND volume surge across the top Nifty-50 weighted
constituents into ONE composite leading indicator: the VWMI
(Volume-Weighted Momentum Index).

Two pieces are blended per tick, both index-weight-normalized:

  1. Weighted Price Momentum (WPM)
     sum(normalized_weight_i * %change_i)
     -> approximates Nifty's own %change using only the heavyweight basket.
        Because it's computed stock-by-stock as ticks arrive, it often moves
        a beat before the NIFTY50-INDEX tick itself catches up.

  2. Weighted Volume Surge (WVS)
     sum(normalized_weight_i * volume_zscore_i)
     -> are the heavyweights trading abnormally heavy volume RIGHT NOW,
        relative to their own recent baseline? Spikes here without price
        follow-through yet are the "smart money is moving" signal.

Composite score:
     VWMI = ALPHA * WPM + BETA * WVS

This gets written into a shared `market_state` style dict at the bottom
(`composite_state`) so you can later import it straight into your
data_manager.py alongside live_feed.py / futures_feed.py, exactly like your
existing market_state pattern.

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

fyers_rest = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path=""
)

# --------------------------------------------------
# Top Nifty-50 weighted constituents (as of Jul 10, 2026 weightage).
# These 12 stocks cover ~51.7% of the index. Update this table whenever NSE
# does the semi-annual rebalance (Mar / Sep) or weights drift materially.
# --------------------------------------------------
RAW_WEIGHTS = {
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

# Normalize weights within the basket so WPM is directly comparable in scale
# to Nifty's own %chg (otherwise it would read ~2x too low since the basket
# is only ~52% of the index).
_total_w = sum(RAW_WEIGHTS.values())
WEIGHTS_NORM = {sym: w / _total_w for sym, w in RAW_WEIGHTS.items()}

SYMBOLS = list(RAW_WEIGHTS.keys())

# --------------------------------------------------
# Tunables
# --------------------------------------------------
ALPHA = 0.7   # weight on price momentum
BETA = 0.3    # weight on volume surge
VOL_HISTORY_LEN = 30
MIN_SAMPLES_FOR_ZSCORE = 6
SURGE_THRESHOLD = 0.35   # |VWMI| above this -> flag a directional surge

# --------------------------------------------------
# Per-symbol rolling state
# --------------------------------------------------
_stock_state = {
    sym: {
        "ltp": None,
        "chp": None,
        "last_volume": None,
        "vol_deltas": deque(maxlen=VOL_HISTORY_LEN),
        "vol_z": None,
    }
    for sym in SYMBOLS
}

# Shared composite state - import this dict from data_manager.py if you want
# to feed VWMI into your pattern-detection / decision-intelligence layers.
composite_state = {
    "timestamp": None,
    "wpm": None,          # weighted price momentum (%)
    "wvs": None,           # weighted volume surge (z-score space)
    "vwmi": None,          # combined composite score
    "signal": "NEUTRAL",
    "coverage_pct": round(_total_w, 2),  # how much of Nifty this basket represents
}

HEADER = (
    f"{'Time':<10} | {'WPM(%)':>8} | {'WVS':>7} | {'VWMI':>7} | {'Signal':<12} | {'Ticks':>5}"
)
DIVIDER = "-" * len(HEADER)


def fmt(val, decimals=3):
    if val is None:
        return "N/A"
    try:
        return f"{val:.{decimals}f}"
    except Exception:
        return "N/A"


def classify(vwmi):
    if vwmi is None:
        return "NEUTRAL"
    if vwmi > SURGE_THRESHOLD:
        return "BULLISH SURGE"
    if vwmi < -SURGE_THRESHOLD:
        return "BEARISH SURGE"
    return "NEUTRAL"


print(DIVIDER)
print(HEADER)
print(DIVIDER)


def recompute_composite(ts):
    """Recompute WPM / WVS / VWMI from current per-stock state."""
    wpm_sum = 0.0
    wvs_sum = 0.0
    wpm_has_data = False
    wvs_has_data = False

    for sym in SYMBOLS:
        s = _stock_state[sym]
        w = WEIGHTS_NORM[sym]

        if s["chp"] is not None:
            wpm_sum += w * s["chp"]
            wpm_has_data = True

        if s["vol_z"] is not None:
            wvs_sum += w * s["vol_z"]
            wvs_has_data = True

    wpm = wpm_sum if wpm_has_data else None
    wvs = wvs_sum if wvs_has_data else None

    vwmi = None
    if wpm is not None and wvs is not None:
        vwmi = ALPHA * wpm + BETA * wvs
    elif wpm is not None:
        vwmi = ALPHA * wpm  # volume baseline still warming up

    composite_state["timestamp"] = ts
    composite_state["wpm"] = wpm
    composite_state["wvs"] = wvs
    composite_state["vwmi"] = vwmi
    composite_state["signal"] = classify(vwmi)

    ticks_ready = sum(1 for sym in SYMBOLS if _stock_state[sym]["chp"] is not None)

    print(
        f"{ts:<10} | {fmt(wpm):>8} | {fmt(wvs):>7} | {fmt(vwmi):>7} | "
        f"{composite_state['signal']:<12} | {ticks_ready:>2}/{len(SYMBOLS)}"
    )


def onmessage(message):
    raw_symbol = message.get("symbol", "")
    if raw_symbol not in _stock_state:
        return

    ltp = message.get("ltp")
    if ltp is None:
        return

    chp = message.get("chp")
    volume = message.get("vol_traded_today")
    ts_epoch = message.get("last_traded_time") or message.get("exch_feed_time")
    time_str = datetime.fromtimestamp(ts_epoch).strftime("%H:%M:%S") if ts_epoch else "N/A"

    s = _stock_state[raw_symbol]
    s["ltp"] = ltp
    s["chp"] = chp

    if volume is not None:
        if s["last_volume"] is not None:
            delta = max(volume - s["last_volume"], 0)
            s["vol_deltas"].append(delta)
            if len(s["vol_deltas"]) >= MIN_SAMPLES_FOR_ZSCORE:
                mean_d = statistics.mean(s["vol_deltas"])
                std_d = statistics.pstdev(s["vol_deltas"])
                s["vol_z"] = (delta - mean_d) / std_d if std_d > 0 else 0.0
        s["last_volume"] = volume

    recompute_composite(time_str)


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