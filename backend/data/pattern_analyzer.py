"""
Fyers Candlestick Pattern Analyzer
===================================
Reads the same historical data your fyers_history.py fetcher uses, then
detects candlestick patterns (per-candle, shape-based):

    Doji, Hammer, Inverted Hammer, Shooting Star, Hanging Man,
    Engulfing, Morning Star, Evening Star, Harami,
    Three White Soldiers, Three Black Crows, Marubozu,
    Inside Bar, Outside Bar, Tweezer Top, Tweezer Bottom

No TA-Lib required — every rule is plain pandas/numpy so you can see and
tune every threshold below (search for "TUNE:" comments).

Note: this only detects candlestick shapes, not market structure (trend,
breakout, consolidation, etc.) — those need multi-candle context that a
single candle's shape can't give you.

Run: python pattern_analyzer.py
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

# --------------------------------------------------
# Access Token / Client  (same as fyers_history.py)
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

fyers = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path=""
)

# --------------------------------------------------
# Symbol quick-pick menu (option to type manually too)
# --------------------------------------------------
SYMBOL_OPTIONS = {
    "1": ("Nifty 50 Index",  "NSE:NIFTY50-INDEX"),
    "2": ("BankNifty Index", "NSE:NIFTYBANK-INDEX"),
    "3": ("FinNifty Index",  "NSE:FINNIFTY-INDEX"),
    "4": ("Midcap Nifty",    "NSE:MIDCPNIFTY-INDEX"),
    "5": ("India VIX",       "NSE:INDIAVIX-INDEX"),
}

# --------------------------------------------------
# Candle Resolutions  (label, api_value, is_intraday)
# --------------------------------------------------
CANDLE_TYPES = {
    "1":  ("1 Min",   "1",   True),
    "2":  ("5 Min",   "5",   True),
    "3":  ("15 Min",  "15",  True),
    "4":  ("30 Min",  "30",  True),
    "5":  ("60 Min",  "60",  True),
    "6":  ("Daily",   "D",   False),
    "7":  ("Weekly",  "1W",  False),
    "8":  ("Monthly", "1M",  False),
}

PERIODS = {
    "1": ("1 Month",  30),
    "2": ("3 Months", 90),
    "3": ("6 Months", 180),
    "4": ("1 Year",   365),
}

# ==================================================
# 1. FETCH
# ==================================================
def fetch_candles(symbol, resolution, days):
    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1",
        "oi_flag": "0",
    }
    resp = fyers.history(data=data)
    if resp.get("s") != "ok":
        raise RuntimeError(f"Fyers error [{resp.get('code')}]: {resp.get('message')}")

    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError("No candles returned for this range.")

    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="s")
    df = df[["datetime", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return df


# ==================================================
# 2. BASE MEASUREMENTS  (used by everything below)
# ==================================================
def add_base_columns(df, atr_period=14, trend_lookback=10):
    df = df.copy()
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["bullish"] = df["close"] > df["open"]
    df["bearish"] = df["close"] < df["open"]

    # True Range / ATR (Wilder-ish, simple rolling mean version)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_period, min_periods=1).mean()

    # Short-term trend context: is price making higher highs/lows or lower highs/lows
    # over the last `trend_lookback` candles? Used to disambiguate Hammer/Hanging Man etc.
    roll_high = df["high"].rolling(trend_lookback, min_periods=trend_lookback)
    roll_low = df["low"].rolling(trend_lookback, min_periods=trend_lookback)
    df["prior_uptrend"] = df["close"] > roll_high.max().shift(1) * 0.0 + df["close"].rolling(trend_lookback).mean().shift(1)
    # simpler, more robust version: close vs its own SMA over lookback, shifted so it reflects
    # the trend *going into* this candle, not including it
    sma = df["close"].rolling(trend_lookback, min_periods=trend_lookback).mean()
    df["prior_uptrend"] = df["close"].shift(1) > sma.shift(1)
    df["prior_downtrend"] = df["close"].shift(1) < sma.shift(1)

    return df


# ==================================================
# 3. CANDLESTICK PATTERNS  (per-candle, shape rules)
# ==================================================
def detect_candlestick_patterns(df):
    df = df.copy()
    n = len(df)
    patterns = [[] for _ in range(n)]

    body = df["body"]
    rng = df["range"]
    up_w = df["upper_wick"]
    low_w = df["lower_wick"]
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    # TUNE: how small a body must be, relative to range, to count as "small"
    SMALL_BODY = 0.35
    DOJI_BODY = 0.1
    LONG_WICK_MULT = 2.0
    MARUBOZU_WICK_MAX = 0.05

    small_body = body <= SMALL_BODY * rng
    tiny_body = body <= DOJI_BODY * rng

    # --- Doji: body is almost nothing relative to the day's range
    doji = tiny_body
    for i in np.where(doji)[0]:
        patterns[i].append("Doji")

    # --- Hammer / Hanging Man: small body near TOP, long lower wick, tiny upper wick
    hammer_shape = small_body & (low_w >= LONG_WICK_MULT * body) & (up_w <= 0.15 * rng)
    for i in np.where(hammer_shape & df["prior_downtrend"])[0]:
        patterns[i].append("Hammer")
    for i in np.where(hammer_shape & df["prior_uptrend"])[0]:
        patterns[i].append("Hanging Man")

    # --- Inverted Hammer / Shooting Star: small body near BOTTOM, long upper wick
    inv_hammer_shape = small_body & (up_w >= LONG_WICK_MULT * body) & (low_w <= 0.15 * rng)
    for i in np.where(inv_hammer_shape & df["prior_downtrend"])[0]:
        patterns[i].append("Inverted Hammer")
    for i in np.where(inv_hammer_shape & df["prior_uptrend"])[0]:
        patterns[i].append("Shooting Star")

    # --- Marubozu: body takes up almost the entire range, negligible wicks
    marubozu = (up_w <= MARUBOZU_WICK_MAX * rng) & (low_w <= MARUBOZU_WICK_MAX * rng) & (body >= 0.9 * rng)
    for i in np.where(marubozu & df["bullish"])[0]:
        patterns[i].append("Bullish Marubozu")
    for i in np.where(marubozu & df["bearish"])[0]:
        patterns[i].append("Bearish Marubozu")

    # --- 2-candle patterns ---
    for i in range(1, n):
        po, ph, pl, pc = o[i-1], h[i-1], l[i-1], c[i-1]
        co, ch, cl, cc = o[i], h[i], l[i], c[i]
        prev_body = body[i-1]
        cur_body = body[i]

        # Engulfing: current body fully engulfs previous body, opposite colors
        if df["bearish"][i-1] and df["bullish"][i] and co <= pc and cc >= po and cur_body > prev_body:
            patterns[i].append("Bullish Engulfing")
        if df["bullish"][i-1] and df["bearish"][i] and co >= pc and cc <= po and cur_body > prev_body:
            patterns[i].append("Bearish Engulfing")

        # Harami: current body sits entirely inside previous body (opposite colors, smaller)
        if cur_body < prev_body * 0.6:
            if df["bearish"][i-1] and df["bullish"][i] and co >= pc and cc <= po:
                patterns[i].append("Bullish Harami")
            if df["bullish"][i-1] and df["bearish"][i] and co <= pc and cc >= po:
                patterns[i].append("Bearish Harami")

        # Inside Bar: current range fully inside previous range
        if ch <= ph and cl >= pl:
            patterns[i].append("Inside Bar")

        # Outside Bar: current range fully engulfs previous range
        if ch >= ph and cl <= pl:
            patterns[i].append("Outside Bar")

        # Tweezer Top / Bottom: matching highs or lows, opposite-ish candles, at a trend extreme
        HIGH_LOW_TOL = 0.1  # TUNE: how close highs/lows must be (fraction of ATR)
        tol = df["atr"][i] * HIGH_LOW_TOL if not np.isnan(df["atr"][i]) else 0
        if abs(ch - ph) <= tol and df["prior_uptrend"][i]:
            patterns[i].append("Tweezer Top")
        if abs(cl - pl) <= tol and df["prior_downtrend"][i]:
            patterns[i].append("Tweezer Bottom")

    # --- 3-candle patterns ---
    for i in range(2, n):
        b0, b1, b2 = body[i-2], body[i-1], body[i]

        # Morning Star: long bearish, small indecisive middle, long bullish closing into candle 1
        if (df["bearish"][i-2] and b0 > df["range"][i-2] * 0.5
                and b1 <= 0.35 * df["range"][i-1]
                and df["bullish"][i] and b2 > df["range"][i] * 0.5
                and c[i] > (o[i-2] + c[i-2]) / 2):
            patterns[i].append("Morning Star")

        # Evening Star: mirror image
        if (df["bullish"][i-2] and b0 > df["range"][i-2] * 0.5
                and b1 <= 0.35 * df["range"][i-1]
                and df["bearish"][i] and b2 > df["range"][i] * 0.5
                and c[i] < (o[i-2] + c[i-2]) / 2):
            patterns[i].append("Evening Star")

        # Three White Soldiers: 3 consecutive long bullish candles, each opening within
        # prior body and closing higher than prior close
        if (df["bullish"][i-2] and df["bullish"][i-1] and df["bullish"][i]
                and o[i-1] > o[i-2] and o[i-1] < c[i-2]
                and o[i] > o[i-1] and o[i] < c[i-1]
                and c[i-1] > c[i-2] and c[i] > c[i-1]
                and b0 > df["range"][i-2] * 0.5 and b1 > df["range"][i-1] * 0.5 and b2 > df["range"][i] * 0.5):
            patterns[i].append("Three White Soldiers")

        # Three Black Crows: mirror image
        if (df["bearish"][i-2] and df["bearish"][i-1] and df["bearish"][i]
                and o[i-1] < o[i-2] and o[i-1] > c[i-2]
                and o[i] < o[i-1] and o[i] > c[i-1]
                and c[i-1] < c[i-2] and c[i] < c[i-1]
                and b0 > df["range"][i-2] * 0.5 and b1 > df["range"][i-1] * 0.5 and b2 > df["range"][i] * 0.5):
            patterns[i].append("Three Black Crows")

    df["candlestick_patterns"] = patterns
    return df


# ==================================================
# 4. MENUS + MAIN
# ==================================================
def ask(prompt, choices):
    while True:
        val = input(f"\n  {prompt}: ").strip()
        if val in choices:
            return val
        print(f"  [!] Enter a valid option number")


def choose_symbol():
    print("\n  Symbol:")
    for k, (label, sym) in SYMBOL_OPTIONS.items():
        print(f"    {k}. {label}  ({sym})")
    print(f"    6. Other — type your own symbol")

    val = input("\n  Select: ").strip()
    if val in SYMBOL_OPTIONS:
        return SYMBOL_OPTIONS[val][1]

    # "6", or anything else typed, falls through to manual entry
    manual = input("  Enter symbol (e.g. NSE:SBIN-EQ): ").strip().upper()
    return manual or "NSE:NIFTY50-INDEX"


def main():
    print("=" * 80)
    print("  FYERS CANDLESTICK PATTERN ANALYZER")
    print("=" * 80)

    symbol = choose_symbol()

    print("\n  Candle type:")
    for k, (label, _, _) in CANDLE_TYPES.items():
        print(f"    {k}. {label}")
    ck = ask("Select candle type", CANDLE_TYPES)
    _, resolution, _ = CANDLE_TYPES[ck]

    print("\n  Period:")
    for k, (label, _) in PERIODS.items():
        print(f"    {k}. {label}")
    pk = ask("Select period", PERIODS)
    _, days = PERIODS[pk]

    print("\n  Fetching data...")
    df = fetch_candles(symbol, resolution, days)
    print(f"  Got {len(df)} candles.\n")

    df = add_base_columns(df)
    df = detect_candlestick_patterns(df)

    # --- Candlestick pattern hits (most recent first) ---
    print("-" * 80)
    print("  CANDLESTICK PATTERNS DETECTED (most recent 30 with a hit)")
    print("-" * 80)
    hits = df[df["candlestick_patterns"].apply(len) > 0]
    for _, row in hits.tail(30).iterrows():
        print(f"  {row['datetime']}  |  {', '.join(row['candlestick_patterns'])}")
    if hits.empty:
        print("  (none found in this range)")

    # --- Summary counts, so coverage is easy to sanity-check at a glance ---
    print("\n" + "-" * 80)
    print("  PATTERN COUNTS (over this full range)")
    print("-" * 80)
    counts = {}
    for row_patterns in df["candlestick_patterns"]:
        for p in row_patterns:
            counts[p] = counts.get(p, 0) + 1
    if counts:
        for name, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"  {name:<22} : {n}")
    else:
        print("  (no patterns found in this range)")

    print()


if __name__ == "__main__":
    main()