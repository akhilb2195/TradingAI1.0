"""
Fyers LIVE Candlestick Pattern Tracker
=======================================
Subscribes to the WebSocket tick feed, aggregates ticks into candles
(1 / 3 / 5 / 15 min — your choice), and re-runs the candlestick pattern
detection from pattern_analyzer.py every time a candle closes.

Requires pattern_analyzer.py to be in the same folder (this script imports
its detection functions rather than duplicating them).

Run: python live_pattern_analyzer.py
"""

from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from datetime import datetime
import pandas as pd
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

from pattern_analyzer import (
    fetch_candles,
    add_base_columns,
    detect_candlestick_patterns,
)

# --------------------------------------------------
# Access Token
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

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

SYMBOLS = []  # populated interactively in main() via choose_symbols()

# --------------------------------------------------
# Candle resolution for live aggregation
# --------------------------------------------------
RESOLUTIONS = {
    "1": ("1 Min",  60,  "1"),   # (label, seconds_per_candle, REST api value for seeding)
    "2": ("3 Min",  180, "3"),
    "3": ("5 Min",  300, "5"),
    "4": ("15 Min", 900, "15"),
}

LOOKBACK_NEEDED = 12      # candles needed (trend-context lookback of 10 + buffer)
MAX_HISTORY_ROWS = 300    # trim history so recompute stays fast
SEED_DAYS = 5             # days of historical candles to pre-load on startup


# ==================================================
# Candle Builder — turns ticks into OHLCV bars
# ==================================================
class CandleBuilder:
    def __init__(self, symbol, resolution_seconds, seed_df):
        self.symbol = symbol
        self.res_sec = resolution_seconds
        self.history = seed_df.copy()
        self.current = None          # dict: bucket_start, o, h, l, c, vol_at_open
        self.last_cum_volume = 0

    def _bucket_start(self, ts):
        return ts - (ts % self.res_sec)

    def on_tick(self, ltp, ts, cum_volume):
        bucket = self._bucket_start(ts)

        if self.current is None:
            self._open_candle(bucket, ltp, cum_volume)
            return None

        if bucket != self.current["bucket"]:
            closed_row = self._close_candle()
            self._open_candle(bucket, ltp, cum_volume)
            return closed_row

        # same candle — update it
        self.current["high"] = max(self.current["high"], ltp)
        self.current["low"] = min(self.current["low"], ltp)
        self.current["close"] = ltp
        self.current["last_cum_volume"] = cum_volume
        return None

    def _open_candle(self, bucket, ltp, cum_volume):
        self.current = {
            "bucket": bucket,
            "open": ltp,
            "high": ltp,
            "low": ltp,
            "close": ltp,
            "vol_at_open": cum_volume,
            "last_cum_volume": cum_volume,
        }

    def _close_candle(self):
        c = self.current
        row = {
            "datetime": datetime.fromtimestamp(c["bucket"]),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": max(0, c["last_cum_volume"] - c["vol_at_open"]),
        }
        self.history = pd.concat([self.history, pd.DataFrame([row])], ignore_index=True)
        if len(self.history) > MAX_HISTORY_ROWS:
            self.history = self.history.iloc[-MAX_HISTORY_ROWS:].reset_index(drop=True)
        return row


builders = {}  # symbol -> CandleBuilder
tick_count = {"n": 0}  # simple mutable counter for the heartbeat print


# ==================================================
# Run detection + print on newly closed candle
# ==================================================
def analyze_and_print(symbol, closed_row):
    df = builders[symbol].history
    if len(df) < LOOKBACK_NEEDED:
        print(f"  [{symbol}] {closed_row['datetime']}  candle closed "
              f"(need {LOOKBACK_NEEDED - len(df)} more candles before trend-context patterns kick in)")
        return

    enriched = add_base_columns(df)
    enriched = detect_candlestick_patterns(enriched)
    last = enriched.iloc[-1]

    patterns = last["candlestick_patterns"]

    print(f"\n  [{symbol}]  {last['datetime']}  |  O:{last['open']:.2f} H:{last['high']:.2f} "
          f"L:{last['low']:.2f} C:{last['close']:.2f}  Vol:{int(last['volume']):,}")
    if patterns:
        print(f"    Candlestick: {', '.join(patterns)}")
    else:
        print("    (no pattern signal this candle)")


# ==================================================
# WebSocket callbacks
# ==================================================
def parse_symbol(raw):
    try:
        exchange, symbol = raw.split(":", 1)
        return exchange, symbol
    except Exception:
        return "N/A", raw


def onmessage(message):
    ltp = message.get("ltp")
    if ltp is None:
        return

    raw_symbol = message.get("symbol", "")
    if raw_symbol not in builders:
        return

    ts = message.get("last_traded_time") or message.get("exch_feed_time")
    if ts is None:
        return

    cum_volume = message.get("vol_traded_today") or 0

    tick_count["n"] += 1
    if tick_count["n"] % 50 == 0:
        print(f"  ... ({tick_count['n']} ticks received so far, still building current candle)")

    closed_row = builders[raw_symbol].on_tick(ltp, int(ts), cum_volume)
    if closed_row is not None:
        analyze_and_print(raw_symbol, closed_row)


def onerror(message):
    print("Error:", message)


def onclose(message):
    print("Closed:", message)


def onopen():
    fyers_ws.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
    fyers_ws.keep_running()
    print(f"  Connected & subscribed to {len(SYMBOLS)} symbols. Waiting for ticks...")
    print("  (If nothing appears below and it's outside NSE market hours —")
    print("   9:15 AM to 3:30 PM IST, Mon-Fri — that's expected: no ticks flow when the market is closed.)\n")


# ==================================================
# Startup: seed history, then connect websocket
# ==================================================
def choose_symbols():
    print("\n  Symbols to watch live:")
    for k, (label, sym) in SYMBOL_OPTIONS.items():
        print(f"    {k}. {label}  ({sym})")
    print("\n  Enter option numbers separated by commas (e.g. 1,2)")
    print("  and/or type your own symbols separated by commas (e.g. NSE:SBIN-EQ,NSE:TCS-EQ)")
    print("  You can mix both, e.g.: 1,2,NSE:SBIN-EQ")

    raw = input("\n  Select: ").strip()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]

    symbols = []
    for t in tokens:
        if t in SYMBOL_OPTIONS:
            symbols.append(SYMBOL_OPTIONS[t][1])
        else:
            symbols.append(t.upper())

    return symbols or ["NSE:NIFTY50-INDEX"]


def choose_resolution():
    print("  Candle resolution for live detection:")
    for k, (label, _, _) in RESOLUTIONS.items():
        print(f"    {k}. {label}")
    while True:
        val = input("\n  Select: ").strip()
        if val in RESOLUTIONS:
            return RESOLUTIONS[val]
        print("  [!] Invalid choice")


def main():
    print("=" * 70)
    print("  FYERS LIVE CANDLESTICK PATTERN TRACKER")
    print("=" * 70)

    global SYMBOLS
    SYMBOLS = choose_symbols()
    print(f"\n  Watching: {', '.join(SYMBOLS)}")

    label, res_seconds, api_res = choose_resolution()
    print(f"\n  Using {label} candles. Seeding history for {len(SYMBOLS)} symbols...")

    for symbol in SYMBOLS:
        try:
            seed_df = fetch_candles(symbol, api_res, SEED_DAYS)
            print(f"    {symbol}: seeded with {len(seed_df)} historical candles")
        except Exception as e:
            print(f"    {symbol}: could not seed history ({e}) — starting empty")
            seed_df = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        builders[symbol] = CandleBuilder(symbol, res_seconds, seed_df)

    print("\n  Connecting to live feed... (Ctrl+C to stop)\n")
    global fyers_ws
    fyers_ws = data_ws.FyersDataSocket(
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
    try:
        fyers_ws.connect()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()