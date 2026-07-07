"""
consumer.py
------------
Reads live ticks continuously from Redis. Doesn't talk to Fyers at all.

Can be run:
  - standalone: python consumer.py                (asks for symbols interactively)
  - via launcher: python consumer.py NSE:TCS-EQ,NSE:SBIN-EQ   (symbols passed in)

Every tick now prints its exact exchange timestamp AND the time this
script received it, so you can always tell precisely when a price is
from - no more guessing whether a number is "live" or stale. Any
connection gap logged by producer.py (GAP_LOG_KEY in Redis) is also
picked up and printed here the moment it appears, so a silence in the
tick stream is clearly explained instead of looking like "nothing
happened".
"""

import sys
import json
import time
import traceback
from datetime import datetime

from redis_config import get_redis_client, stream_key
from symbol_selector import choose_symbols

GAP_LOG_KEY = "ticks:gap_log"   # must match producer.py

try:
    r = get_redis_client()
except Exception:
    print("[CONSUMER] FATAL: could not create Redis client at startup:")
    traceback.print_exc()
    sys.exit(1)

MAX_BACKOFF_SECONDS = 30
RECONNECT_AFTER_FAILURES = 5   # recreate the client itself after this many consecutive errors


def fmt_ts(epoch_val):
    if not epoch_val:
        return "?"
    try:
        return datetime.fromtimestamp(float(epoch_val)).strftime("%H:%M:%S")
    except (ValueError, TypeError, OSError):
        return "?"


def print_new_gaps(last_gap_len):
    """Check Redis for any gap-log entries we haven't printed yet."""
    try:
        total = r.llen(GAP_LOG_KEY)
    except Exception:
        return last_gap_len
    if total > last_gap_len:
        try:
            new_entries = r.lrange(GAP_LOG_KEY, last_gap_len, -1)
            for raw in new_entries:
                g = json.loads(raw)
                print(f"[CONSUMER] *** DATA GAP *** {g['gap_start']} -> {g['gap_end']} "
                      f"({g['duration_sec']}s missing, session #{g['session_id']}) - "
                      f"no prices exist for this window, don't treat it as 'flat'.")
        except Exception as e:
            print(f"[CONSUMER] Could not read gap log: {e}")
        return total
    return last_gap_len


def follow_symbols(symbols):
    global r
    print(f"[CONSUMER] Watching: {', '.join(symbols)}")
    last_ids = {stream_key(sym): "$" for sym in symbols}
    backoff = 1
    consecutive_failures = 0
    last_gap_len = 0
    last_gap_len = print_new_gaps(last_gap_len)  # don't replay old gaps as if new

    while True:
        try:
            response = r.xread(last_ids, block=5000, count=100)
            backoff = 1  # reset backoff after any successful read
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            print(f"[CONSUMER] Redis read error: {e} - retrying in {backoff}s "
                  f"(failure {consecutive_failures})")
            if consecutive_failures >= RECONNECT_AFTER_FAILURES:
                # A retry on the same broken connection object isn't always
                # enough (e.g. after the socket itself died) - get a brand
                # new client instance instead of just retrying the old one.
                print("[CONSUMER] Too many consecutive failures, recreating Redis client ...")
                try:
                    r = get_redis_client()
                    consecutive_failures = 0
                except Exception as e2:
                    print(f"[CONSUMER] Could not recreate Redis client: {e2}")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue

        last_gap_len = print_new_gaps(last_gap_len)

        if not response:
            continue

        for stream_name, entries in response:
            for entry_id, fields in entries:
                try:
                    tick = json.loads(fields["data"])
                    exch_t = fmt_ts(tick.get("exch_feed_time"))
                    recv_t = fmt_ts(tick.get("received_at"))
                    resumed_tag = "  [FIRST TICK AFTER GAP]" if tick.get("resumed_after_gap") else ""
                    print(
                        f"[CONSUMER] exch={exch_t} recv={recv_t} | "
                        f"{tick.get('symbol')} | LTP={tick.get('ltp')} | "
                        f"Vol={tick.get('volume')} | Bid={tick.get('bid')} | Ask={tick.get('ask')}"
                        f"{resumed_tag}"
                    )
                except Exception as e:
                    print(f"[CONSUMER] Bad tick on {stream_name}: {e}")
                # Always advance the cursor even if this particular tick
                # was malformed, so one bad entry can't block all future ones.
                last_ids[stream_name] = entry_id


if __name__ == "__main__":
    if len(sys.argv) > 1:
        symbols = [s.strip() for s in sys.argv[1].split(",") if s.strip()]
    else:
        symbols = choose_symbols()

    if not symbols:
        print("[CONSUMER] No symbols given/selected. Nothing to watch. Exiting.")
        sys.exit(1)

    while True:
        try:
            follow_symbols(symbols)
            break
        except KeyboardInterrupt:
            print("\n[CONSUMER] Stopped.")
            break
        except Exception:
            print("[CONSUMER] Crashed unexpectedly:")
            traceback.print_exc()
            print("[CONSUMER] Restarting in 5 seconds ... (Ctrl+C to quit)\n")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("\n[CONSUMER] Stopped.")
                break