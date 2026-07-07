"""
candle_patterns.py
-------------------
Reads live + historical ticks from Redis (same streams producer.py writes to),
builds OHLC candles starting from 09:15 up to "now", and detects classic
candlestick patterns on every CLOSED candle. Keeps running live afterwards
with no artificial delay - each pattern is printed the instant its candle closes.

Does NOT touch Fyers at all (same philosophy as consumer.py).

Run:
    python candle_patterns.py                                   (asks for symbols)
    python candle_patterns.py NSE:TCS-EQ,NSE:SBIN-EQ             (symbols passed in)
    python candle_patterns.py NSE:TCS-EQ,NSE:SBIN-EQ 5           (5-minute candles)

Second CLI arg is the candle size in MINUTES (default = 1).
"""

import sys
import json
import time
import traceback
from datetime import datetime, timedelta, time as dtime

from redis_config import get_redis_client, stream_key
from symbol_selector import choose_symbols

try:
    r = get_redis_client()
except Exception:
    print("[CANDLES] FATAL: could not create Redis client at startup:")
    traceback.print_exc()
    sys.exit(1)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

DEFAULT_CANDLE_MINUTES = 1
TREND_LOOKBACK = 3          # how many prior candles define "uptrend/downtrend"
TOLERANCE_PCT = 0.05        # 5% of avg body/range used as "close enough" tolerance
HEARTBEAT_SECONDS = 30      # how often to print an "I'm alive" status line
CANDLE_MENU = [1, 3, 5, 15] # options offered in the interactive menu
SANE_WINDOW_SECONDS = 20    # exchange feed time only trusted if within this many seconds of now
RECONNECT_AFTER_FAILURES = 5  # recreate the Redis client after this many consecutive read errors


# ----------------------------------------------------------------------
# Candle bucketing helpers
# ----------------------------------------------------------------------

def bucket_start(ts, interval_seconds):
    epoch = ts.timestamp()
    start_epoch = int(epoch // interval_seconds) * interval_seconds
    return datetime.fromtimestamp(start_epoch)


def parse_tick_time(payload):
    """
    Bucketing timestamp, in priority order:

    1. Exchange feed time (exch_feed_time / last_traded_time) - this is
       what TradingView also aligns candles to, so it gives the most
       accurate boundary matching. BUT it's only trusted if it's sane -
       i.e. within SANE_WINDOW_SECONDS of "now". For some symbols
       (notably indices) this field can go stale or stop updating,
       which previously caused ticks to pile into an old bucket and
       made the live candle look frozen.
    2. received_at - wall-clock time producer.py actually received the
       tick. Used whenever exchange time is missing or fails the
       sanity check. Includes network/processing latency, so a tick
       landing exactly on a boundary can occasionally be filed one
       bucket late - this is the standard trade-off when the exchange
       timestamp can't be trusted, and is the main source of the 1-2
       point differences you'll see vs a chart that aligns purely on
       exchange time.
    3. now() - last resort if payload has neither field.
    """
    now = datetime.now()

    for key in ("exch_feed_time", "last_traded_time"):
        val = payload.get(key)
        if val:
            try:
                ts = datetime.fromtimestamp(int(val))
                if abs((now - ts).total_seconds()) <= SANE_WINDOW_SECONDS:
                    return ts
            except (ValueError, TypeError, OSError):
                pass

    val = payload.get("received_at")
    if val:
        try:
            return datetime.fromtimestamp(float(val))
        except (ValueError, TypeError, OSError):
            pass

    return now


# ----------------------------------------------------------------------
# Per-symbol candle builder
# ----------------------------------------------------------------------

class CandleBuilder:
    def __init__(self, symbol, interval_seconds):
        self.symbol = symbol
        self.interval_seconds = interval_seconds
        self.candles = {}       # bucket_start(datetime) -> candle dict
        self.order = []         # sorted list of bucket_start keys
        self.printed = set()    # bucket_starts already analysed/printed

        self.last_price = None       # last known LTP, used to gap-fill quiet minutes
        self.last_tick_at = None     # wall-clock time of the last tick received

        # Exchange-reported running day high/low (from the tick payload's
        # own "high"/"low" fields, NOT computed from our LTP samples).
        # Fyers' websocket only pushes an update when LTP *changes*, so a
        # brief price touch can happen and revert before our next tick -
        # our own high/low tracking would miss it, but the exchange's own
        # day-high/day-low field will have already moved to reflect it.
        # We watch for that field *increasing* (for high) or *decreasing*
        # (for low) and credit the new extreme to whichever candle is open
        # at that moment - this recovers wicks our LTP sampling missed,
        # without ever using a stale/repeated value (we only act on a
        # genuine change, never on a value that's just being repeated).
        self.last_day_high = None
        self.last_day_low = None

    def add_tick(self, ltp, volume, ts, exch_high=None, exch_low=None):
        if ltp is None:
            return
        self.last_price = ltp
        self.last_tick_at = datetime.now()
        b = bucket_start(ts, self.interval_seconds)
        c = self.candles.get(b)
        if c is None:
            c = {
                "start": b,
                "end": b + timedelta(seconds=self.interval_seconds),
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "first_vol": volume if volume is not None else 0,
                "last_vol": volume if volume is not None else 0,
                "synthetic": False,
            }
            self.candles[b] = c
            self.order.append(b)
            self.order.sort()
        else:
            c["high"] = max(c["high"], ltp)
            c["low"] = min(c["low"], ltp)
            c["close"] = ltp
            if volume is not None:
                c["last_vol"] = volume

        # Exchange day-high/day-low delta detection (see comment above).
        if exch_high is not None:
            if self.last_day_high is None:
                self.last_day_high = exch_high
            elif exch_high > self.last_day_high:
                c["high"] = max(c["high"], exch_high)
                self.last_day_high = exch_high
        if exch_low is not None:
            if self.last_day_low is None:
                self.last_day_low = exch_low
            elif exch_low < self.last_day_low:
                c["low"] = min(c["low"], exch_low)
                self.last_day_low = exch_low

    def fill_gaps(self, now):
        """
        If minutes passed with zero ticks (thin/quiet symbol), create flat
        'no trade' candles using the last known price so the candle series
        has no holes and pattern context (trend lookback) stays continuous.
        These are clearly flagged as synthetic so they're never confused
        with a real price move.
        """
        if self.last_price is None or not self.order:
            return
        current_bucket = bucket_start(now, self.interval_seconds)
        last_bucket = self.order[-1]
        b = last_bucket + timedelta(seconds=self.interval_seconds)
        while b < current_bucket:
            c = {
                "start": b,
                "end": b + timedelta(seconds=self.interval_seconds),
                "open": self.last_price,
                "high": self.last_price,
                "low": self.last_price,
                "close": self.last_price,
                "first_vol": 0,
                "last_vol": 0,
                "synthetic": True,
            }
            self.candles[b] = c
            self.order.append(b)
            b += timedelta(seconds=self.interval_seconds)

    def ordered_closed_candles(self, now):
        """All buckets strictly before the bucket 'now' currently sits in."""
        current_bucket = bucket_start(now, self.interval_seconds)
        return [self.candles[b] for b in self.order if b < current_bucket]

    def volume_of(self, c):
        v = c["last_vol"] - c["first_vol"]
        return v if v > 0 else c["last_vol"]


# ----------------------------------------------------------------------
# Candle geometry helpers
# ----------------------------------------------------------------------

def body(c):
    return abs(c["close"] - c["open"])


def rng(c):
    r_ = c["high"] - c["low"]
    return r_ if r_ > 0 else 1e-9


def upper_shadow(c):
    return c["high"] - max(c["open"], c["close"])


def lower_shadow(c):
    return min(c["open"], c["close"]) - c["low"]


def is_bullish(c):
    return c["close"] > c["open"]


def is_bearish(c):
    return c["close"] < c["open"]


def is_uptrend(seq, lookback=TREND_LOOKBACK):
    if len(seq) < lookback:
        return False
    closes = [x["close"] for x in seq[-lookback:]]
    return closes[-1] > closes[0]


def is_downtrend(seq, lookback=TREND_LOOKBACK):
    if len(seq) < lookback:
        return False
    closes = [x["close"] for x in seq[-lookback:]]
    return closes[-1] < closes[0]


def close_enough(a, b, scale):
    return abs(a - b) <= TOLERANCE_PCT * scale


# ----------------------------------------------------------------------
# Pattern detection - operates on the full chronological list of candles
# for one symbol, checking the LAST candle in `history` (index -1) as the
# "just closed" candle, using history[-2], history[-3] for context.
# ----------------------------------------------------------------------

def detect_patterns(history):
    if not history:
        return []
    if history[-1].get("synthetic"):
        return []

    patterns = []
    c0 = history[-1]                       # candle just closed
    prev = history[:-1]                    # everything before it

    b0 = body(c0)
    r0 = rng(c0)
    up0 = upper_shadow(c0)
    lo0 = lower_shadow(c0)

    # ---------- single-candle patterns ----------
    if b0 <= 0.1 * r0:
        patterns.append("Doji")

    if b0 >= 0.9 * r0 and up0 <= 0.05 * r0 and lo0 <= 0.05 * r0:
        patterns.append("Marubozu")

    small_body_top = (b0 <= 0.35 * r0) and (min(c0["open"], c0["close"]) - c0["low"]) >= 2 * b0 and up0 <= 0.15 * r0
    small_body_bottom = (b0 <= 0.35 * r0) and (c0["high"] - max(c0["open"], c0["close"])) >= 2 * b0 and lo0 <= 0.15 * r0

    if small_body_top:
        if is_downtrend(prev):
            patterns.append("Hammer")
        elif is_uptrend(prev):
            patterns.append("Hanging Man")

    if small_body_bottom:
        if is_downtrend(prev):
            patterns.append("Inverted Hammer")
        elif is_uptrend(prev):
            patterns.append("Shooting Star")

    # ---------- two-candle patterns ----------
    if len(history) >= 2:
        c1 = history[-2]                   # previous candle
        b1 = body(c1)

        # Engulfing
        if is_bearish(c1) and is_bullish(c0) and c0["open"] <= c1["close"] and c0["close"] >= c1["open"]:
            patterns.append("Bullish Engulfing")
        if is_bullish(c1) and is_bearish(c0) and c0["open"] >= c1["close"] and c0["close"] <= c1["open"]:
            patterns.append("Bearish Engulfing")

        # Harami (opposite of engulfing: current body inside previous body)
        hi_body1, lo_body1 = max(c1["open"], c1["close"]), min(c1["open"], c1["close"])
        hi_body0, lo_body0 = max(c0["open"], c0["close"]), min(c0["open"], c0["close"])
        if hi_body0 <= hi_body1 and lo_body0 >= lo_body1 and b1 > 0 and b0 < b1:
            if is_bearish(c1) and is_bullish(c0):
                patterns.append("Bullish Harami")
            elif is_bullish(c1) and is_bearish(c0):
                patterns.append("Bearish Harami")

        # Inside Bar / Outside Bar (based on full range, not just body)
        if c0["high"] <= c1["high"] and c0["low"] >= c1["low"]:
            patterns.append("Inside Bar")
        if c0["high"] >= c1["high"] and c0["low"] <= c1["low"]:
            patterns.append("Outside Bar")

        # Tweezer Top / Bottom
        avg_price = (c0["close"] + c1["close"]) / 2
        if close_enough(c0["high"], c1["high"], avg_price) and is_bullish(c1) and is_bearish(c0):
            if is_uptrend(prev[:-1] if len(prev) > 1 else prev):
                patterns.append("Tweezer Top")
        if close_enough(c0["low"], c1["low"], avg_price) and is_bearish(c1) and is_bullish(c0):
            if is_downtrend(prev[:-1] if len(prev) > 1 else prev):
                patterns.append("Tweezer Bottom")

    # ---------- three-candle patterns ----------
    if len(history) >= 3:
        c2, c1, c0_ = history[-3], history[-2], history[-1]

        # Morning Star: bearish, small body, bullish closing above midpoint of c2
        mid_c2 = (c2["open"] + c2["close"]) / 2
        if (is_bearish(c2) and body(c2) >= 0.6 * rng(c2)
                and body(c1) <= 0.35 * rng(c1)
                and is_bullish(c0_) and c0_["close"] >= mid_c2):
            patterns.append("Morning Star")

        # Evening Star: bullish, small body, bearish closing below midpoint of c2
        if (is_bullish(c2) and body(c2) >= 0.6 * rng(c2)
                and body(c1) <= 0.35 * rng(c1)
                and is_bearish(c0_) and c0_["close"] <= mid_c2):
            patterns.append("Evening Star")

        # Three White Soldiers
        if (is_bullish(c2) and is_bullish(c1) and is_bullish(c0_)
                and c1["close"] > c2["close"] and c0_["close"] > c1["close"]
                and c1["open"] > c2["open"] and c0_["open"] > c1["open"]
                and upper_shadow(c2) <= 0.2 * rng(c2)
                and upper_shadow(c1) <= 0.2 * rng(c1)
                and upper_shadow(c0_) <= 0.2 * rng(c0_)):
            patterns.append("Three White Soldiers")

        # Three Black Crows
        if (is_bearish(c2) and is_bearish(c1) and is_bearish(c0_)
                and c1["close"] < c2["close"] and c0_["close"] < c1["close"]
                and c1["open"] < c2["open"] and c0_["open"] < c1["open"]
                and lower_shadow(c2) <= 0.2 * rng(c2)
                and lower_shadow(c1) <= 0.2 * rng(c1)
                and lower_shadow(c0_) <= 0.2 * rng(c0_)):
            patterns.append("Three Black Crows")

    return patterns


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------

def fmt_candle_line(symbol, c):
    return (f"{c['start'].strftime('%H:%M')}-{c['end'].strftime('%H:%M')} | "
            f"O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f}")


def announce(symbol, c, patterns):
    print("=" * 72)
    tag = "  (no trades this candle)" if c.get("synthetic") else ""
    print(f"[{symbol}]  {fmt_candle_line(symbol, c)}{tag}")
    if c.get("synthetic"):
        print("   >>> Pattern: skipped (flat/no-trade candle)")
    elif patterns:
        print(f"   >>> PATTERN: {', '.join(patterns)}")
    else:
        print("   >>> Pattern: none")
    print("=" * 72, flush=True)


# ----------------------------------------------------------------------
# Backfill: read everything Redis currently has for a symbol (since
# producer.py clears the stream daily, "everything" = today from 09:15
# up to whatever the last tick is), build candles, and analyse all of
# them that have already closed relative to "now".
# ----------------------------------------------------------------------

def backfill_symbol(symbol, builder, retries=3):
    entries = None
    for attempt in range(1, retries + 1):
        try:
            entries = r.xrange(stream_key(symbol), min="-", max="+")
            break
        except Exception as e:
            print(f"[CANDLES] Redis error during backfill for {symbol} "
                  f"(attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(1)
    if entries is None:
        print(f"[CANDLES] Giving up on backfill for {symbol} after {retries} attempts - "
              f"it will still pick up live ticks going forward.")
        return "0-0"

    last_id = "0-0"
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields["data"])
            ts = parse_tick_time(payload)
            builder.add_tick(payload.get("ltp"), payload.get("volume"), ts,
                              exch_high=payload.get("high"), exch_low=payload.get("low"))
        except Exception as e:
            print(f"[CANDLES] Bad historical tick for {symbol}: {e}")
        last_id = entry_id

    now = datetime.now()
    builder.fill_gaps(now)
    closed_history = []
    for c in builder.ordered_closed_candles(now):
        closed_history.append(c)
        if c["start"] not in builder.printed:
            patterns = detect_patterns(closed_history)
            announce(symbol, c, patterns)
            builder.printed.add(c["start"])

    return last_id


# ----------------------------------------------------------------------
# Live loop
# ----------------------------------------------------------------------

def run(symbols, candle_minutes):
    global r
    interval_seconds = candle_minutes * 60
    builders = {sym: CandleBuilder(sym, interval_seconds) for sym in symbols}
    last_ids = {}
    stream_map = {stream_key(sym): sym for sym in symbols}   # safe reverse lookup

    print(f"[CANDLES] Building {candle_minutes}-minute candles for: {', '.join(symbols)}")
    print(f"[CANDLES] Backfilling from 09:15 up to now ...")

    for sym in symbols:
        last_ids[stream_key(sym)] = backfill_symbol(sym, builders[sym])

    print("[CANDLES] Backfill complete. Now watching live (no delay) ...\n")

    last_heartbeat = datetime.now()
    consecutive_failures = 0

    while True:
        try:
            response = r.xread(last_ids, block=300, count=500)
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            print(f"[CANDLES] Redis read error: {e} (failure {consecutive_failures}, retrying)")
            if consecutive_failures >= RECONNECT_AFTER_FAILURES:
                print("[CANDLES] Too many consecutive failures, recreating Redis client ...")
                try:
                    r = get_redis_client()
                    consecutive_failures = 0
                except Exception as e2:
                    print(f"[CANDLES] Could not recreate Redis client: {e2}")
            time.sleep(min(consecutive_failures, 5))  # brief backoff, never a long stall
            continue

        now = datetime.now()

        if response:
            for stream_name, entries in response:
                sym = stream_map.get(stream_name)
                if sym is None:
                    # Unexpected stream (shouldn't normally happen) - skip, don't crash.
                    continue
                for entry_id, fields in entries:
                    try:
                        payload = json.loads(fields["data"])
                        ts = parse_tick_time(payload)
                        builders[sym].add_tick(payload.get("ltp"), payload.get("volume"), ts,
                                                exch_high=payload.get("high"), exch_low=payload.get("low"))
                    except Exception as e:
                        print(f"[CANDLES] Bad tick for {sym}: {e}")
                    last_ids[stream_name] = entry_id

        # After processing whatever new ticks arrived, gap-fill any quiet
        # minutes and check every symbol for candles that just closed.
        # This runs every loop iteration (up to every ~1s), so there is no
        # artificial delay - a pattern is announced the moment its candle closes.
        for sym in symbols:
            b = builders[sym]
            b.fill_gaps(now)
            closed_history = b.ordered_closed_candles(now)
            new_ones = [c for c in closed_history if c["start"] not in b.printed]
            for c in new_ones:
                idx = closed_history.index(c)
                patterns = detect_patterns(closed_history[: idx + 1])
                announce(sym, c, patterns)
                b.printed.add(c["start"])

        # Heartbeat so it's obvious the script is alive even when a symbol is quiet.
        if (now - last_heartbeat).total_seconds() >= HEARTBEAT_SECONDS:
            statuses = []
            for sym in symbols:
                b = builders[sym]
                last_seen = b.last_tick_at.strftime("%H:%M:%S") if b.last_tick_at else "no ticks yet"
                statuses.append(f"{sym} (last tick {last_seen})")
            print(f"[HEARTBEAT] {now.strftime('%H:%M:%S')} | " + " | ".join(statuses), flush=True)
            last_heartbeat = now

        if now.time() >= MARKET_CLOSE:
            # Market's done for the day; nothing more will arrive from producer.
            pass


def choose_candle_minutes():
    print("\nChoose candle size:")
    for i, m in enumerate(CANDLE_MENU, start=1):
        print(f"  {i}. {m} minute")
    print(f"  {len(CANDLE_MENU) + 1}. Custom (enter minutes manually)")
    try:
        choice = input(f"Enter choice [1-{len(CANDLE_MENU) + 1}] (default 1): ").strip()
    except EOFError:
        print(f"[CANDLES] No interactive input available, using default {DEFAULT_CANDLE_MINUTES} minute(s).")
        return DEFAULT_CANDLE_MINUTES
    if not choice:
        return CANDLE_MENU[0]
    try:
        idx = int(choice)
        if 1 <= idx <= len(CANDLE_MENU):
            return CANDLE_MENU[idx - 1]
        if idx == len(CANDLE_MENU) + 1:
            try:
                custom = input("Enter custom candle size in minutes: ").strip()
            except EOFError:
                return DEFAULT_CANDLE_MINUTES
            return max(1, int(custom))
    except ValueError:
        pass
    print(f"[CANDLES] Invalid choice, using default {DEFAULT_CANDLE_MINUTES} minute(s).")
    return DEFAULT_CANDLE_MINUTES


if __name__ == "__main__":
    if len(sys.argv) > 1:
        symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    else:
        symbols = choose_symbols()

    if not symbols:
        print("[CANDLES] No symbols given/selected. Nothing to track. Exiting.")
        sys.exit(1)

    if len(sys.argv) > 2:
        try:
            candle_minutes = max(1, int(sys.argv[2]))
        except ValueError:
            print(f"[CANDLES] Invalid minute value '{sys.argv[2]}', using default {DEFAULT_CANDLE_MINUTES}")
            candle_minutes = DEFAULT_CANDLE_MINUTES
    else:
        candle_minutes = choose_candle_minutes()

    while True:
        try:
            run(symbols, candle_minutes)
            break
        except KeyboardInterrupt:
            print("\n[CANDLES] Stopped.")
            break
        except Exception:
            print("\n[CANDLES] Crashed with an unexpected error:")
            traceback.print_exc()
            print("[CANDLES] Restarting in 5 seconds ... (Ctrl+C to quit)\n")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("\n[CANDLES] Stopped.")
                break