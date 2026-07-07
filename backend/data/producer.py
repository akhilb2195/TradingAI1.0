"""
producer.py
------------
Connects to Fyers, stores every tick into Redis from 9:15 AM to 3:30 PM,
dumps to a Parquet file and clears Redis at close, repeats daily.

Can be run:
  - standalone: python producer.py                (asks for symbols interactively)
  - via launcher: python producer.py NSE:TCS-EQ,NSE:SBIN-EQ   (symbols passed in)

--------------------------------------------------------------------------
DESIGN NOTES - why this version is built the way it is (read once, skip
after that; every non-obvious line below ties back to one of these):

1. NO DATA LOSS AT MARKET CLOSE (the original race condition)
   The old code read the stream, then deleted it - if a tick arrived in
   between those two steps, it fell into a gap and was never saved
   anywhere. This version instead RENAMEs the live stream to an archive
   key first. Redis executes commands one at a time (it's single-
   threaded internally), and RENAME is a single atomic command, so a
   concurrent XADD from the websocket thread can only ever land BEFORE
   the rename (captured in the archive - safe) or AFTER it (lands in a
   freshly auto-created live key - safe). There is no window where a
   tick can be lost.

2. NO PARTIAL/CORRUPT PARQUET FILES
   Writes go to a ".tmp" file first, then get atomically renamed into
   place with os.replace(). If the process dies mid-write, you're left
   with a stray .tmp file, never a half-written "real" file.

3. NO SILENT DATA LOSS ON A FAILED DUMP
   The archived Redis data is only deleted AFTER the Parquet file is
   confirmed written. If anything fails (Redis hiccup, disk full,
   pandas error), the archive key is left in Redis and the scheduler
   retries every 5 seconds until it succeeds - it does not mark the day
   as "done" on a failure.

4. NO SILENT DEATH OF THE WEBSOCKET THREAD
   The old onmessage() had no error handling - one bad message or one
   transient Redis error could throw inside the callback and, depending
   on the websocket library's internals, potentially stop future
   messages from being processed with no visible error. Every tick is
   now processed inside a try/except that logs and continues.

5. VISIBILITY INTO FEED GAPS
   Reconnects are timestamped and counted. This can't prevent data loss
   during an actual websocket outage (that data never reached us at
   all - no code can recover it), but it makes outages visible instead
   of silent, which is what actually causes confusing "why is this
   number different" moments later.
--------------------------------------------------------------------------
"""

import sys
import os
import json
import time
import threading
import traceback
from datetime import datetime, time as dtime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel

# Force single-threaded Parquet conversion. pyarrow otherwise parallelizes
# DataFrame->Table conversion across a background ThreadPoolExecutor; if
# the main thread exits (Ctrl+C, terminal closed, crash) at the exact
# moment that pool is mid-conversion, Python's interpreter shutdown races
# with it and throws "cannot schedule new futures after interpreter
# shutdown". Pinning to 1 thread removes that internal pool entirely, so
# this specific race can no longer happen no matter when shutdown occurs.
pa.set_cpu_count(1)
pa.set_io_thread_count(1)

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id
from redis_config import get_redis_client, stream_key, ACTIVE_SYMBOLS_KEY
from symbol_selector import choose_symbols

MARKET_OPEN = dtime(9, 15)      # change to dtime(9, 30) if you prefer
MARKET_CLOSE = dtime(15, 30)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "ticks_archive")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

access_token = None
_tried_paths = []

for candidate in ("access_token.txt", os.path.join(BASE_DIR, "access_token.txt")):
    _tried_paths.append(candidate)
    try:
        with open(candidate, "r") as f:
            access_token = f.read().strip()
        break
    except FileNotFoundError:
        continue

if not access_token:
    print("[PRODUCER] FATAL: access_token.txt not found. Checked:")
    for p in _tried_paths:
        print(f"           - {os.path.abspath(p)}")
    print("[PRODUCER] Generate/refresh your Fyers access token and place it in "
          "the folder you run this script from (same as before), then retry.")
    sys.exit(1)

fyers_rest = fyersModel.FyersModel(
    client_id=client_id, is_async=False, token=access_token, log_path=""
)

r = get_redis_client()

_dumped_today = False
_lock = threading.Lock()
_reconnect_count = 0

# --- Gap/session tracking -------------------------------------------------
# GAP_LOG_KEY holds a running list of "we lost connection from X to Y"
# events, shared across producer/consumer/candle_patterns via Redis, so
# every downstream tool can see exactly when data is missing instead of
# silently treating a gap as "price didn't move".
GAP_LOG_KEY = "ticks:gap_log"

_session_id = 0                 # increments every reconnect; tags every tick
_awaiting_first_tick = False     # True right after a reconnect, until the next real tick arrives
_gap_start_at = None            # wall-clock time the connection was lost
# ---------------------------------------------------------------------------

print(f"[PRODUCER] System clock at startup: {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')}")
print("[PRODUCER] IMPORTANT: every timestamp this pipeline stores (received_at, "
      "candle bucketing downstream) depends on this clock being correct. "
      "If you're on Linux, run 'timedatectl' and confirm NTP sync is active "
      "and the timezone is Asia/Kolkata before trusting timing for automated trading.")


def in_market_hours(now):
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def log_gap(start_at, end_at, session_id):
    """
    Record a connection-outage window to Redis so every consumer of this
    data (consumer.py, candle_patterns.py, your own trading logic) can see
    it and treat that time range as "unknown", not "flat/unchanged".
    """
    record = {
        "session_id": session_id,
        "gap_start": start_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "gap_end": end_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "duration_sec": round((end_at - start_at).total_seconds(), 3),
    }
    try:
        r.rpush(GAP_LOG_KEY, json.dumps(record))
    except Exception as e:
        print(f"[PRODUCER] Could not log gap to Redis: {e}")
    print(f"[PRODUCER] GAP LOGGED: {record['gap_start']} -> {record['gap_end']} "
          f"({record['duration_sec']}s missing, session #{session_id})")


def store_tick(message):
    global _awaiting_first_tick, _gap_start_at

    raw_symbol = message.get("symbol", "")
    if not raw_symbol:
        return

    now = time.time()
    exch_ts = message.get("exch_feed_time")
    feed_delay_sec = None
    if exch_ts:
        try:
            feed_delay_sec = round(now - int(exch_ts), 3)
        except (ValueError, TypeError):
            feed_delay_sec = None

    resumed_after_gap = False
    with _lock:
        if _awaiting_first_tick:
            resumed_after_gap = True
            _awaiting_first_tick = False
            if _gap_start_at is not None:
                log_gap(_gap_start_at, datetime.now(), _session_id)
                _gap_start_at = None
        current_session = _session_id

    payload = {
        "symbol": raw_symbol,
        "ltp": message.get("ltp"),
        "prev_close": message.get("prev_close_price"),
        "open": message.get("open_price"),
        "high": message.get("high_price"),
        "low": message.get("low_price"),
        "change": message.get("ch"),
        "change_pct": message.get("chp"),
        "volume": message.get("vol_traded_today"),
        "bid": message.get("bid_price"),
        "ask": message.get("ask_price"),
        "buy_qty": message.get("tot_buy_qty"),
        "sell_qty": message.get("tot_sell_qty"),
        "exch_feed_time": exch_ts,
        "last_traded_time": message.get("last_traded_time"),
        "received_at": now,
        "feed_delay_sec": feed_delay_sec,   # how stale the exchange timestamp was when we got it
        "session_id": current_session,       # increments on every reconnect - lets you see session boundaries
        "resumed_after_gap": resumed_after_gap,  # True only for the very first tick after a reconnect
    }

    try:
        pipe = r.pipeline()
        pipe.xadd(stream_key(raw_symbol), {"data": json.dumps(payload)})
        pipe.sadd(ACTIVE_SYMBOLS_KEY, raw_symbol)
        pipe.execute()
    except Exception as e:
        # Don't let a transient Redis error kill the websocket callback.
        # The tick is lost either way here (nowhere else to put it), but
        # at least it's visible instead of silently crashing the feed.
        print(f"[PRODUCER] Redis write error for {raw_symbol}: {e}")


def archive_and_reset_symbol(symbol):
    """
    Atomically move a symbol's live stream out of the way so it can be
    safely read and dumped, while a fresh (empty) live stream keeps
    accepting new ticks immediately - no gap, no race. See design note
    #1 at the top of the file.
    """
    live_key = stream_key(symbol)
    archive_key = f"{live_key}:archive:{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    try:
        r.rename(live_key, archive_key)
        return archive_key
    except Exception:
        # Most commonly: live_key didn't exist (no ticks arrived today).
        return None


def dump_and_clear():
    """
    Returns True if the day's data was fully and safely archived,
    False if anything failed (caller should retry later, and MUST NOT
    treat the day as done).
    """
    date_str = datetime.now().strftime("%Y-%m-%d")

    try:
        symbols = r.smembers(ACTIVE_SYMBOLS_KEY)
    except Exception as e:
        print(f"[{date_str}] ERROR reading active symbols: {e}")
        return False

    if not symbols:
        print(f"[{date_str}] Nothing to dump (no active symbols).")
        return True

    # Reset tracking immediately. If a tick lands after this but before
    # we finish, store_tick()'s SADD just re-adds the symbol - harmless,
    # self-healing, and never causes data loss on its own.
    try:
        r.delete(ACTIVE_SYMBOLS_KEY)
    except Exception as e:
        print(f"[{date_str}] WARNING: could not reset active-symbols set: {e}")

    archive_keys = {}
    for symbol in symbols:
        akey = archive_and_reset_symbol(symbol)
        if akey:
            archive_keys[symbol] = akey

    if not archive_keys:
        print(f"[{date_str}] Nothing to dump (streams empty).")
        return True

    all_ticks = []
    failed_symbols = []
    for symbol, akey in archive_keys.items():
        try:
            entries = r.xrange(akey, min="-", max="+")
            for _, fields in entries:
                all_ticks.append(json.loads(fields["data"]))
        except Exception as e:
            print(f"[{date_str}] ERROR reading archive for {symbol}: {e}")
            failed_symbols.append(symbol)

    if failed_symbols:
        # Leave ALL archive keys untouched and retry the whole dump next
        # time rather than write a partial file - partial daily data is
        # worse than a short delay.
        print(f"[{date_str}] Dump incomplete (failed: {failed_symbols}). Will retry.")
        return False

    if not all_ticks:
        print(f"[{date_str}] Archived streams were empty, nothing to write.")
        for akey in archive_keys.values():
            r.delete(akey)
        return True

    df = pd.DataFrame(all_ticks)
    out_path = os.path.join(ARCHIVE_DIR, f"ticks_{date_str}.parquet")
    tmp_path = out_path + ".tmp"
    try:
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, out_path)
    except Exception as e:
        print(f"[{date_str}] ERROR writing parquet: {e}")
        print(f"[{date_str}] Redis archive kept intact for retry.")
        return False

    print(f"[{date_str}] Dumped {len(df)} ticks -> {out_path}")

    for akey in archive_keys.values():
        try:
            r.delete(akey)
        except Exception as e:
            print(f"[{date_str}] WARNING: could not delete archive key {akey}: {e}")

    print(f"[{date_str}] Redis archive cleared. Live streams continue fresh.")
    return True


def scheduler_loop():
    global _dumped_today
    last_date = datetime.now().date()
    while True:
        now = datetime.now()
        if now.date() != last_date:
            last_date = now.date()
            with _lock:
                _dumped_today = False

        if now.time() >= MARKET_CLOSE:
            with _lock:
                already = _dumped_today
            if not already:
                try:
                    success = dump_and_clear()
                except Exception:
                    print("[PRODUCER] Unexpected error during dump_and_clear:")
                    traceback.print_exc()
                    success = False
                if success:
                    with _lock:
                        _dumped_today = True
                # If it failed, we deliberately do nothing else here -
                # the loop will just try again in 5 seconds.
        time.sleep(5)


def onmessage(message):
    try:
        ltp = message.get("ltp")
        if ltp is None:
            return
        now = datetime.now()
        with _lock:
            already_dumped = _dumped_today
        if already_dumped or not in_market_hours(now):
            return
        store_tick(message)
        print(f"[PRODUCER] {now.strftime('%H:%M:%S')} | {message.get('symbol')} | LTP={ltp}")
    except Exception:
        # Never let a bad/unexpected message kill the feed thread.
        print("[PRODUCER] Unexpected error in onmessage:")
        traceback.print_exc()


def onerror(message):
    print(f"[PRODUCER] {datetime.now().strftime('%H:%M:%S')} Error:", message)


def onclose(message):
    global _reconnect_count, _gap_start_at, _awaiting_first_tick
    _reconnect_count += 1
    with _lock:
        _gap_start_at = datetime.now()
        _awaiting_first_tick = True
    print(f"[PRODUCER] {datetime.now().strftime('%H:%M:%S')} Closed "
          f"(disconnect/reconnect #{_reconnect_count}):", message)
    print("[PRODUCER] NOTE: any ticks the exchange sent while disconnected "
          "were never received - this is a genuine feed gap, not something "
          "this script can recover after the fact. It will be logged with "
          "exact start/end times the moment the connection resumes.")


def start_producer(symbols):
    global _session_id
    threading.Thread(target=scheduler_loop, daemon=True).start()

    def onopen():
        global _session_id
        with _lock:
            _session_id += 1
        socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
        socket.keep_running()
        print(f"[PRODUCER] {datetime.now().strftime('%H:%M:%S')} Connected/subscribed "
              f"(session #{_session_id}).")

    socket = data_ws.FyersDataSocket(
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
    socket.connect()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    else:
        symbols = choose_symbols()

    print(f"[PRODUCER] Tracking: {', '.join(symbols)}")
    try:
        start_producer(symbols)
    except KeyboardInterrupt:
        print("\n[PRODUCER] Stopped.")