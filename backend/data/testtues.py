"""
Fyers Tuesday 2:30 -> 3:10 PM Move Backtester
--------------------------------------
Answers: "On Tuesdays, how much does price typically move between
2:30 PM and 3:10 PM?"

ASSUMPTION (edit CONFIG below if this isn't what you meant):
  "Move" = % change from the WINDOW_START candle's open to the
  WINDOW_END candle's close. Only Tuesdays with actual trading data
  are counted (holidays/no-data days are skipped automatically).

Requires access_token.txt and creditials.py (client_id) exactly like
your original fyers_history.py script — run this from the same folder.
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, timedelta
import sys, os, time

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

# ============================================================
# CONFIG — edit these to change what's being measured
# ============================================================
SYMBOL          = "NSE:NIFTY50-INDEX"   # symbol to backtest
LOOKBACK_DAYS   = 365                   # how many calendar days back to scan
WINDOW_START    = "14:50"               # start of the afternoon window
WINDOW_END      = "15:05"               # end of the afternoon window
RESOLUTION      = "1"                   # 1-minute candles (needed for exact times)
# ============================================================

with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

fyers = fyersModel.FyersModel(
    client_id=client_id, is_async=False, token=access_token, log_path=""
)


def chunk_ranges(start_date, end_date, max_days=100):
    """1-min resolution allows only 100 days per API call -> split into chunks."""
    chunks = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=max_days - 1), end_date)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_1min_candles(symbol, start_date, end_date):
    """Fetch 1-min candles across the full range, chunked to respect the 100-day limit."""
    all_candles = []
    for c_start, c_end in chunk_ranges(start_date, end_date):
        data = {
            "symbol":      symbol,
            "resolution":  RESOLUTION,
            "date_format": "1",
            "range_from":  c_start.strftime("%Y-%m-%d"),
            "range_to":    c_end.strftime("%Y-%m-%d"),
            "cont_flag":   "1",
            "oi_flag":     "0",
        }
        resp = fyers.history(data=data)
        if resp.get("s") != "ok":
            print(f"  [!] {c_start} -> {c_end}: {resp.get('message', 'error')} (code {resp.get('code')})")
            time.sleep(0.3)
            continue
        all_candles.extend(resp.get("candles", []))
        time.sleep(0.3)  # go easy on the API
    return all_candles


def group_by_day(candles):
    """{ 'YYYY-MM-DD': { 'HH:MM': (open, high, low, close, volume) } }"""
    days = {}
    for ts, o, h, l, cl, vol in candles:
        dt = datetime.fromtimestamp(ts)
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M")
        days.setdefault(date_str, {})[time_str] = (o, h, l, cl, vol)
    return days


def is_tuesday(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() == 1  # Mon=0, Tue=1


def analyze():
    end_date   = datetime.now().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    print(f"Fetching {SYMBOL} 1-min candles: {start_date} -> {end_date} ...")
    candles = fetch_1min_candles(SYMBOL, start_date, end_date)
    if not candles:
        print("No data returned. Check symbol, access token, or date range.")
        return

    days = group_by_day(candles)
    tuesdays = sorted(d for d in days if is_tuesday(d))
    print(f"Found {len(tuesdays)} Tuesdays with data.\n")

    results = []
    for date_str in tuesdays:
        bars = days[date_str]
        start_bar = bars.get(WINDOW_START)
        end_bar   = bars.get(WINDOW_END)
        if not start_bar or not end_bar:
            continue  # holiday / partial session / early close
        window_open  = start_bar[0]
        window_close = end_bar[3]
        pct_move = (window_close - window_open) / window_open * 100
        results.append((date_str, window_open, window_close, pct_move))

    if not results:
        print("No Tuesdays had both the WINDOW_START and WINDOW_END candle.")
        print("Try a shorter LOOKBACK_DAYS or double-check WINDOW_START/WINDOW_END.")
        return

    # ---- Table ----
    hdr_open   = f"Open({WINDOW_START})"
    hdr_target = f"Close({WINDOW_END})"
    print(f"{'Date':<12} | {hdr_open:>14} | {hdr_target:>16} | {'% Move':>8}")
    print("-" * 60)
    for date_str, o, c, pct in results:
        print(f"{date_str:<12} | {o:>14.2f} | {c:>16.2f} | {pct:>7.2f}%")

    # ---- Summary stats ----
    pct_values = [r[3] for r in results]
    avg   = sum(pct_values) / len(pct_values)
    up    = sum(1 for p in pct_values if p > 0)
    down  = sum(1 for p in pct_values if p < 0)
    flat  = len(pct_values) - up - down
    best  = max(pct_values)
    worst = min(pct_values)

    print("\n" + "=" * 60)
    print(f"  Tuesdays analyzed : {len(pct_values)}")
    print(f"  Average move      : {avg:.2f}%")
    print(f"  Best day          : {best:.2f}%")
    print(f"  Worst day         : {worst:.2f}%")
    print(f"  Up days           : {up}  ({up/len(pct_values)*100:.1f}%)")
    print(f"  Down days         : {down}  ({down/len(pct_values)*100:.1f}%)")
    print(f"  Flat days         : {flat}")
    print("=" * 60)


if __name__ == "__main__":
    analyze()