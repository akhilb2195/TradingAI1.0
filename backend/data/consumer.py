"""
consumer.py
------------
Reads live ticks continuously from Redis. Doesn't talk to Fyers at all.

Can be run:
  - standalone: python consumer.py                (asks for symbols interactively)
  - via launcher: python consumer.py NSE:TCS-EQ,NSE:SBIN-EQ   (symbols passed in)

DESIGN NOTE: the original version had zero error handling - a single
Redis hiccup would crash it outright with no retry, which is exactly the
"it just stopped" symptom that showed up in candle_patterns.py before it
was hardened. This version applies the same treatment: retry with
backoff on Redis errors, per-tick error isolation, and a top-level
restart loop that prints the full traceback instead of dying silently.
"""

import sys
import json
import time
import traceback

from redis_config import get_redis_client, stream_key
from symbol_selector import choose_symbols

try:
    r = get_redis_client()
except Exception:
    print("[CONSUMER] FATAL: could not create Redis client at startup:")
    traceback.print_exc()
    sys.exit(1)

MAX_BACKOFF_SECONDS = 30
RECONNECT_AFTER_FAILURES = 5   # recreate the client itself after this many consecutive errors


def follow_symbols(symbols):
    global r
    print(f"[CONSUMER] Watching: {', '.join(symbols)}")
    last_ids = {stream_key(sym): "$" for sym in symbols}
    backoff = 1
    consecutive_failures = 0

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

        if not response:
            continue

        for stream_name, entries in response:
            for entry_id, fields in entries:
                try:
                    tick = json.loads(fields["data"])
                    print(
                        f"[CONSUMER] {tick.get('symbol')} | LTP={tick.get('ltp')} | "
                        f"Vol={tick.get('volume')} | Bid={tick.get('bid')} | Ask={tick.get('ask')}"
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