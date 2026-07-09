"""
candle_indicators.py
--------------------
Live technical-indicator dashboard built on top of the exact same Redis
tick stream and candle-building logic as candle_patterns.py (CandleBuilder,
gap handling, synthetic "no trade" candles, backfill-from-09:15 -> then
live with no artificial delay). This file does NOT duplicate that
candle-building logic - it imports it - so a fix made in one place can
never drift out of sync with the other.

Adds: EMA9, EMA21, RSI(14), ATR(14), rolling high/low/avg-volume(20), and
per-candle body/range/wick, via indicator_engine.py (fully reusable, no
Redis/Fyers knowledge in that file at all - see its docstring).

ACCURATE FROM 09:15 (no "warming up" wait):
Before the live Redis loop starts, this script makes ONE REST call per
symbol to Fyers' history API (via historical_seed.py) to pull the last
~30 already-closed candles from prior trading days, and feeds them into
the indicator engine first. EMA/RSI/ATR are meant to carry forward across
sessions on any real chart anyway, so this isn't a hack - it's what a
broker terminal already does. If that call fails for any reason (network,
holiday-only lookback window, bad token) it falls back safely to warming
up live from today's first candle - it never guesses or fabricates a value.

Run:
    python candle_indicators.py                                   (asks for symbols)
    python candle_indicators.py NSE:TCS-EQ,NSE:SBIN-EQ             (symbols passed in)
    python candle_indicators.py NSE:TCS-EQ,NSE:SBIN-EQ 5           (5-minute candles)
"""

import sys
import os
import json
import time
import traceback
from datetime import datetime

from fyers_apiv3 import fyersModel

from redis_config import get_redis_client, stream_key
from symbol_selector import choose_symbols
from candle_pattren import (
    CandleBuilder, parse_tick_time, choose_candle_minutes, MARKET_CLOSE,
)
from indicator_engine import IndicatorEngineRegistry
from historical_seed import fetch_seed_candles

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

try:
    r = get_redis_client()
except Exception:
    print("[INDICATORS] FATAL: could not create Redis client at startup:")
    traceback.print_exc()
    sys.exit(1)

# --------------------------------------------------------------------
# Fyers REST client - used ONLY for the one-time historical seed pull
# below. The live loop after this stays 100% Redis reads, same as before.
# --------------------------------------------------------------------
_fyers_client = None
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for _candidate in ("access_token.txt", os.path.join(_BASE_DIR, "access_token.txt")):
    try:
        with open(_candidate, "r") as f:
            _access_token = f.read().strip()
        _fyers_client = fyersModel.FyersModel(
            client_id=client_id, is_async=False, token=_access_token, log_path=""
        )
        break
    except FileNotFoundError:
        continue

if _fyers_client is None:
    print("[INDICATORS] WARNING: access_token.txt not found - skipping historical "
          "seeding. Indicators will warm up live from today's first candle instead.")

HEARTBEAT_SECONDS = 30
RECONNECT_AFTER_FAILURES = 5

# --------------------------------------------------------------------
# INDEX VOLUME PROBLEM AND FIX
#
# Indices (NSE:NIFTY50-INDEX, NSE:NIFTYBANK-INDEX, ...) are calculated
# values, not traded instruments - Fyers' websocket has no volume field
# to send for them because none exists. That's not fixable in code; VWAP
# is mathematically undefined without volume.
#
# The standard real-world workaround: use the matching futures contract's
# volume instead (it trades with real volume and tracks the index
# closely). Fill this dict to enable it, e.g.:
#     VOLUME_PROXY_SYMBOLS = {
#         "NSE:NIFTY50-INDEX":   "NSE:NIFTY26JULFUT",
#         "NSE:NIFTYBANK-INDEX": "NSE:BANKNIFTY26JULFUT",
#     }
# IMPORTANT: the proxy symbol must ALSO be in producer.py's tracked symbol
# list (so its ticks actually land in Redis) - this script only reads
# from Redis, it never subscribes to Fyers itself for live ticks.
# Update the contract month here whenever the front-month future rolls.
# --------------------------------------------------------------------
VOLUME_PROXY_SYMBOLS = {}


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------

def fmt(v, decimals=2):
    if v is None:
        return "warming up"
    try:
        return f"{v:.{decimals}f}"
    except Exception:
        return str(v)


def print_dashboard(symbol, snap):
    ts_range = f"{snap['start'].strftime('%H:%M')}-{snap['end'].strftime('%H:%M')}"
    print("=" * 88)
    if snap["skipped"]:
        print(f"[{symbol}] {ts_range}  *** candle skipped (synthetic/gap) - "
              f"indicators not updated ***")
        print("=" * 88, flush=True)
        return

    e9, e21 = snap["ema9"], snap["ema21"]
    rsi, atr, roll = snap["rsi14"], snap["atr14"], snap["rolling20"]
    vwap = snap["vwap"]

    def line(label, ind, decimals=2):
        note = "" if ind["ready"] else f"   (needs {ind['candles_until_ready']} more candles)"
        return f"  {label:<9} | {fmt(ind['value'], decimals)}{note}"

    print(f"[{symbol}] {ts_range}")
    print(f"  Candle    | body={fmt(snap['candle_body'])}  range={fmt(snap['candle_range'])}  "
          f"upper_wick={fmt(snap['upper_wick'])}  lower_wick={fmt(snap['lower_wick'])}  "
          f"volume={fmt(snap['volume'], 0)}")
    print(line("EMA9", e9))
    print(line("EMA21", e21))
    print(line("RSI14", rsi))
    print(line("ATR14", atr))
    if vwap["ready"]:
        print(f"  VWAP      | {fmt(vwap['value'])}   (session volume so far: {fmt(vwap['session_volume'], 0)})")
    elif vwap["unavailable_reason"]:
        print(f"  VWAP      | unavailable - {vwap['unavailable_reason']}")
    else:
        print(f"  VWAP      | warming up (no volume yet this session)")
    if roll["ready"]:
        print(f"  Rolling20 | high={fmt(roll['rolling_high'])}  low={fmt(roll['rolling_low'])}  "
              f"avg_vol={fmt(roll['avg_volume'], 0)}")
    else:
        print(f"  Rolling20 | (needs {roll['candles_until_ready']} more candles)")
    print("=" * 88, flush=True)


# ----------------------------------------------------------------------
# Backfill: same approach as candle_patterns.backfill_symbol, but feeds
# each closed candle into the indicator engine instead of pattern detection.
# ----------------------------------------------------------------------

def backfill_symbol(symbol, builder, engine, retries=3, volume_override_fn=None):
    entries = None
    for attempt in range(1, retries + 1):
        try:
            entries = r.xrange(stream_key(symbol), min="-", max="+")
            break
        except Exception as e:
            print(f"[INDICATORS] Redis error during backfill for {symbol} "
                  f"(attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(1)
    if entries is None:
        print(f"[INDICATORS] Giving up on backfill for {symbol} after {retries} attempts - "
              f"it will still pick up live ticks going forward.")
        return "0-0"

    last_id = "0-0"
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields["data"])
            ts = parse_tick_time(payload)
            builder.add_tick(payload.get("ltp"), payload.get("volume"), ts,
                              exch_high=payload.get("high"), exch_low=payload.get("low"))
            if payload.get("resumed_after_gap"):
                builder.mark_gap(ts)
        except Exception as e:
            print(f"[INDICATORS] Bad historical tick for {symbol}: {e}")
        last_id = entry_id

    now = datetime.now()
    builder.fill_gaps(now)
    for c in builder.ordered_closed_candles(now):
        if c["start"] not in builder.printed:
            override = volume_override_fn(c["start"]) if volume_override_fn else None
            snap = engine.update(c, volume_override=override)
            print_dashboard(symbol, snap)
            builder.printed.add(c["start"])

    return last_id


def get_volume_override(sym, candle_start, builders, proxy_for):
    """
    If `sym` has a configured volume proxy (see VOLUME_PROXY_SYMBOLS),
    look up that proxy's candle for the SAME time bucket and return its
    volume. Returns None if no proxy is configured, or if the proxy's
    candle for that bucket isn't available yet - in which case the caller
    falls back to the symbol's own volume (0 for an index, which VWAP
    will correctly report as "unavailable" rather than guessing).
    """
    proxy_sym = proxy_for.get(sym)
    if not proxy_sym:
        return None
    proxy_builder = builders.get(proxy_sym)
    if proxy_builder is None:
        return None
    proxy_candle = proxy_builder.candles.get(candle_start)
    if proxy_candle is None:
        return None
    return proxy_builder.volume_of(proxy_candle)


def backfill_volume_only(symbol, builder, retries=3):
    """
    Same tick backfill as backfill_symbol(), but for a pure volume-proxy
    symbol that isn't one of the user's requested symbols - it exists only
    to answer get_volume_override() lookups, so it has no engine and
    prints no dashboard.
    """
    entries = None
    for attempt in range(1, retries + 1):
        try:
            entries = r.xrange(stream_key(symbol), min="-", max="+")
            break
        except Exception as e:
            print(f"[INDICATORS] Redis error during proxy backfill for {symbol} "
                  f"(attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(1)
    if entries is None:
        print(f"[INDICATORS] Giving up on proxy backfill for {symbol} - "
              f"volume override for it will be unavailable until live ticks arrive.")
        return "0-0"

    last_id = "0-0"
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields["data"])
            ts = parse_tick_time(payload)
            builder.add_tick(payload.get("ltp"), payload.get("volume"), ts,
                              exch_high=payload.get("high"), exch_low=payload.get("low"))
            if payload.get("resumed_after_gap"):
                builder.mark_gap(ts)
        except Exception as e:
            print(f"[INDICATORS] Bad historical tick for proxy {symbol}: {e}")
        last_id = entry_id

    builder.fill_gaps(datetime.now())
    return last_id


# ----------------------------------------------------------------------
# Live loop - mirrors candle_patterns.run(), no artificial delay: a
# candle's indicators are printed the instant that candle closes.
# ----------------------------------------------------------------------

def run(symbols, candle_minutes):
    global r
    interval_seconds = candle_minutes * 60

    # Any configured proxy symbols need their own CandleBuilder too (to
    # supply volume), but they don't get an indicator engine or dashboard.
    proxy_for = {sym: VOLUME_PROXY_SYMBOLS[sym] for sym in symbols if sym in VOLUME_PROXY_SYMBOLS}
    proxy_symbols = [p for p in proxy_for.values() if p not in symbols]
    all_symbols = symbols + proxy_symbols

    builders = {sym: CandleBuilder(sym, interval_seconds) for sym in all_symbols}
    registry = IndicatorEngineRegistry(symbols)
    last_ids = {}
    stream_map = {stream_key(sym): sym for sym in all_symbols}

    print(f"[INDICATORS] Building {candle_minutes}-minute candles + indicators for: "
          f"{', '.join(symbols)}")
    if proxy_for:
        print(f"[INDICATORS] Volume proxies in use: "
              f"{', '.join(f'{k} <- {v}' for k, v in proxy_for.items())}")

    # ------------------------------------------------------------------
    # Seed each symbol's engine with prior-session history BEFORE today's
    # candles start, so EMA21/RSI14/ATR14/rolling20 are already `ready`
    # by the time the very first live candle closes - no "warming up".
    # ------------------------------------------------------------------
    if _fyers_client is not None:
        print("[INDICATORS] Seeding indicators from historical candles ...")
        for sym in symbols:
            seed_candles = fetch_seed_candles(_fyers_client, sym, candle_minutes)
            engine = registry.get(sym)
            for c in seed_candles:
                engine.update(c)
            if seed_candles:
                print(f"[SEED] {sym}: fed {len(seed_candles)} historical candles "
                      f"(up to {seed_candles[-1]['end'].strftime('%Y-%m-%d %H:%M')}) - "
                      f"EMA9={'ready' if engine.ema_fast.ready else 'still warming'}, "
                      f"EMA21={'ready' if engine.ema_slow.ready else 'still warming'}, "
                      f"RSI14={'ready' if engine.rsi.ready else 'still warming'}, "
                      f"ATR14={'ready' if engine.atr.ready else 'still warming'}, "
                      f"Rolling20={'ready' if engine.rolling.ready else 'still warming'}")

    print("[INDICATORS] Backfilling from 09:15 up to now ...")

    for sym in proxy_symbols:
        last_ids[stream_key(sym)] = backfill_volume_only(sym, builders[sym])
    for sym in symbols:
        vol_fn = (lambda c_start, s=sym: get_volume_override(s, c_start, builders, proxy_for)) if sym in proxy_for else None
        last_ids[stream_key(sym)] = backfill_symbol(sym, builders[sym], registry.get(sym), volume_override_fn=vol_fn)

    print("[INDICATORS] Backfill complete. Now watching live (no delay) ...\n")

    last_heartbeat = datetime.now()
    consecutive_failures = 0

    while True:
        try:
            response = r.xread(last_ids, block=300, count=500)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            print(f"[INDICATORS] Redis read error: {e} (failure {consecutive_failures}, retrying)")
            if consecutive_failures >= RECONNECT_AFTER_FAILURES:
                print("[INDICATORS] Too many consecutive failures, recreating Redis client ...")
                try:
                    r = get_redis_client()
                    consecutive_failures = 0
                except Exception as e2:
                    print(f"[INDICATORS] Could not recreate Redis client: {e2}")
            time.sleep(min(consecutive_failures, 5))
            continue

        now = datetime.now()

        if response:
            for stream_name, entries in response:
                sym = stream_map.get(stream_name)
                if sym is None:
                    continue
                for entry_id, fields in entries:
                    try:
                        payload = json.loads(fields["data"])
                        ts = parse_tick_time(payload)
                        builders[sym].add_tick(
                            payload.get("ltp"), payload.get("volume"), ts,
                            exch_high=payload.get("high"), exch_low=payload.get("low"),
                        )
                        if payload.get("resumed_after_gap"):
                            builders[sym].mark_gap(ts)
                    except Exception as e:
                        print(f"[INDICATORS] Bad tick for {sym}: {e}")
                    last_ids[stream_name] = entry_id

        for sym in proxy_symbols:
            builders[sym].fill_gaps(now)

        for sym in symbols:
            b = builders[sym]
            b.fill_gaps(now)
            closed = b.ordered_closed_candles(now)
            new_ones = [c for c in closed if c["start"] not in b.printed]
            engine = registry.get(sym)
            for c in new_ones:
                override = get_volume_override(sym, c["start"], builders, proxy_for) if sym in proxy_for else None
                snap = engine.update(c, volume_override=override)
                print_dashboard(sym, snap)
                b.printed.add(c["start"])

        if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SECONDS:
            statuses = []
            for sym in symbols:
                b = builders[sym]
                last_seen = b.last_tick_at.strftime("%H:%M:%S") if b.last_tick_at else "no ticks yet"
                statuses.append(f"{sym} (last tick {last_seen})")
            print(f"[HEARTBEAT] {now.strftime('%H:%M:%S')} | " + " | ".join(statuses), flush=True)
            last_heartbeat = now

        if now.time() >= MARKET_CLOSE:
            pass


if __name__ == "__main__":
    if len(sys.argv) > 1:
        symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    else:
        symbols = choose_symbols()

    if not symbols:
        print("[INDICATORS] No symbols given/selected. Nothing to track. Exiting.")
        sys.exit(1)

    if len(sys.argv) > 2:
        try:
            candle_minutes = max(1, int(sys.argv[2]))
        except ValueError:
            print(f"[INDICATORS] Invalid minute value '{sys.argv[2]}', using default 5")
            candle_minutes = 5
    else:
        candle_minutes = choose_candle_minutes()

    while True:
        try:
            run(symbols, candle_minutes)
            break
        except KeyboardInterrupt:
            print("\n[INDICATORS] Stopped.")
            break
        except Exception:
            print("\n[INDICATORS] Crashed with an unexpected error:")
            traceback.print_exc()
            print("[INDICATORS] Restarting in 5 seconds ... (Ctrl+C to quit)\n")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("\n[INDICATORS] Stopped.")
                break