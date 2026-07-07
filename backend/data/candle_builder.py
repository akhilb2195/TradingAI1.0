"""
candle_builder.py
------------------
Builds live OHLC candles from the ticks in Redis. Doesn't talk to Fyers.

Can be run:
  - standalone: python candle_builder.py                       (asks interactively)
  - via launcher: python candle_builder.py NSE:TCS-EQ,NSE:SBIN-EQ 1min
  - from an archived day: python candle_builder.py --from-file ticks_archive/ticks_2026-07-05.parquet --timeframe 5min
"""

import sys
import json
import argparse
from datetime import datetime

import pandas as pd

from redis_config import get_redis_client, stream_key, candle_key
from symbol_selector import choose_symbols

r = get_redis_client()

TIMEFRAME_SECONDS = {
    "1min": 60,
    "3min": 180,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1hour": 3600,
}


def choose_timeframe():
    options = list(TIMEFRAME_SECONDS.keys())
    print("\nChoose a candle timeframe:")
    for i, tf in enumerate(options, start=1):
        print(f"  {i}. {tf}")
    choice = input(f"Enter 1-{len(options)} (default 1min): ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(options):
        return options[int(choice) - 1]
    return "1min"


def bucket_start(ts, interval_seconds):
    return int(ts // interval_seconds * interval_seconds)


class CandleAggregator:
    def __init__(self, interval_seconds):
        self.interval_seconds = interval_seconds
        self._open_candles = {}

    def add_tick(self, tick):
        symbol = tick["symbol"]
        ltp = tick.get("ltp")
        volume = tick.get("volume") or 0
        ts = tick.get("exch_feed_time") or tick.get("last_traded_time") or tick.get("received_at")
        if ltp is None or ts is None:
            return None

        start = bucket_start(float(ts), self.interval_seconds)
        current = self._open_candles.get(symbol)

        if current is None or current["bucket_start"] != start:
            finished = current
            self._open_candles[symbol] = {
                "symbol": symbol,
                "bucket_start": start,
                "time": datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S"),
                "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "volume": volume,
            }
            return finished

        current["high"] = max(current["high"], ltp)
        current["low"] = min(current["low"], ltp)
        current["close"] = ltp
        current["volume"] = volume
        return None

    def flush(self):
        finished = list(self._open_candles.values())
        self._open_candles.clear()
        return finished


def save_candle(candle, timeframe):
    key = candle_key(candle["symbol"], timeframe)
    r.rpush(key, json.dumps(candle))
    print(
        f"[CANDLE][{timeframe}] {candle['symbol']} {candle['time']} "
        f"O={candle['open']} H={candle['high']} L={candle['low']} C={candle['close']} V={candle['volume']}"
    )


def run_live(symbols, timeframe):
    interval_seconds = TIMEFRAME_SECONDS[timeframe]
    aggregator = CandleAggregator(interval_seconds)

    last_ids = {stream_key(sym): "$" for sym in symbols}
    print(f"[CANDLE] Building {timeframe} candles for: {', '.join(symbols)}")

    while True:
        response = r.xread(last_ids, block=5000, count=100)
        if not response:
            continue

        for stream_name, entries in response:
            for entry_id, fields in entries:
                tick = json.loads(fields["data"])
                finished = aggregator.add_tick(tick)
                if finished:
                    save_candle(finished, timeframe)
                last_ids[stream_name] = entry_id


def run_from_file(path, timeframe):
    interval_seconds = TIMEFRAME_SECONDS[timeframe]
    aggregator = CandleAggregator(interval_seconds)

    df = pd.read_parquet(path, engine="pyarrow")
    sort_col = "exch_feed_time" if "exch_feed_time" in df.columns else "received_at"
    df = df.sort_values(["symbol", sort_col])

    for _, row in df.iterrows():
        finished = aggregator.add_tick(row.to_dict())
        if finished:
            save_candle(finished, timeframe)

    for finished in aggregator.flush():
        save_candle(finished, timeframe)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="?", help="Comma-separated symbols (from launcher)")
    parser.add_argument("timeframe", nargs="?", help="Timeframe (from launcher)")
    parser.add_argument("--from-file", type=str, help="Build candles from an archived Parquet file")
    args = parser.parse_args()

    try:
        if args.from_file:
            run_from_file(args.from_file, args.timeframe or "1min")
        elif args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            timeframe = args.timeframe or "1min"
            run_live(symbols, timeframe)
        else:
            symbols = choose_symbols()
            timeframe = choose_timeframe()
            run_live(symbols, timeframe)
    except KeyboardInterrupt:
        print("\n[CANDLE] Stopped.")