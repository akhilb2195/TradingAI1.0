"""
nifty_futures_vwap.py

Computes real-time VWAP and Volume for Nifty using the current-month
Nifty futures contract (since NIFTY50-INDEX has no volume data).

Approach:
    - Auto-detect current-month futures symbol (last-Thursday expiry rollover)
    - Subscribe to futures via WebSocket (full mode -> "sf" messages)
    - Fyers sends CUMULATIVE day volume (vol_traded_today) on every tick,
      not per-tick volume. So we derive per-tick volume as the delta
      between consecutive cumulative volume readings.
    - VWAP = cumulative(price * delta_volume) / cumulative(delta_volume)
    - Resets automatically at the start of each trading session (detected
      when vol_traded_today drops below the last seen value, or date changes).

Place this in backend/ alongside future_data.py / creditials.py.
"""

from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from datetime import datetime, timedelta
import calendar
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
# Auto-generate current-month Nifty futures symbol
# (Last Thursday of month = expiry. If today is past
#  expiry, roll to next month's contract.)
# --------------------------------------------------
def get_last_thursday(year, month):
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    offset = (d.weekday() - 3) % 7  # Thursday = 3
    return d - timedelta(days=offset)

def get_current_nifty_futures_symbol():
    today = datetime.now()
    year, month = today.year, today.month
    expiry = get_last_thursday(year, month)

    if today.date() > expiry.date():
        month += 1
        if month > 12:
            month = 1
            year += 1

    yy = str(year)[-2:]
    mmm = datetime(year, month, 1).strftime("%b").upper()
    return f"NSE:NIFTY{yy}{mmm}FUT"

FUT_SYMBOL = get_current_nifty_futures_symbol()
SYMBOLS = [FUT_SYMBOL]

print(f"Using futures contract: {FUT_SYMBOL}")

# --------------------------------------------------
# VWAP / Volume state
# --------------------------------------------------
state = {
    "session_date": None,     # date string, to detect new trading day
    "last_cum_volume": None,  # last seen vol_traded_today
    "cum_pv": 0.0,             # cumulative price * delta_volume
    "cum_vol": 0.0,            # cumulative delta_volume
    "vwap": None,
}

def reset_session(today_str):
    state["session_date"] = today_str
    state["last_cum_volume"] = None
    state["cum_pv"] = 0.0
    state["cum_vol"] = 0.0
    state["vwap"] = None

# --------------------------------------------------
# Header
# --------------------------------------------------
HEADER = (
    f"{'Time':<10} | {'Symbol':<20} | {'LTP':>10} | "
    f"{'Day Vol':>14} | {'Δ Vol':>10} | {'VWAP':>10} | "
    f"{'Bid':>10} | {'Ask':>10} | {'%Chg':>8}"
)
DIVIDER = "-" * len(HEADER)
print(DIVIDER)
print(HEADER)
print(DIVIDER)

# --------------------------------------------------
# Helpers
# --------------------------------------------------
def fmt(val, decimals=2):
    if val is None:
        return "N/A"
    try:
        return f"{val:.{decimals}f}"
    except Exception:
        return "N/A"

def fmt_int(val):
    if val is None:
        return "N/A"
    try:
        return f"{int(val):,}"
    except Exception:
        return "N/A"

# --------------------------------------------------
# WebSocket Callbacks
# --------------------------------------------------
def onmessage(message):
    if message.get("symbol") != FUT_SYMBOL:
        return

    ltp = message.get("ltp")
    cum_volume = message.get("vol_traded_today")
    ts = message.get("last_traded_time") or message.get("exch_feed_time")

    if ltp is None or cum_volume is None:
        return

    now = datetime.fromtimestamp(ts) if ts else datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # Detect new trading session: date changed, or cumulative volume
    # reset lower than last seen (new day's feed restarting from 0)
    if (state["session_date"] != today_str) or (
        state["last_cum_volume"] is not None and cum_volume < state["last_cum_volume"]
    ):
        reset_session(today_str)

    # Compute delta volume for this tick
    if state["last_cum_volume"] is None:
        delta_vol = cum_volume  # first tick of session
    else:
        delta_vol = cum_volume - state["last_cum_volume"]

    delta_vol = max(delta_vol, 0)
    state["last_cum_volume"] = cum_volume

    if delta_vol > 0:
        state["cum_pv"] += ltp * delta_vol
        state["cum_vol"] += delta_vol
        state["vwap"] = state["cum_pv"] / state["cum_vol"] if state["cum_vol"] > 0 else None

    bid = message.get("bid_price")
    ask = message.get("ask_price")
    chp = message.get("chp")
    chp_str = f"{chp:+.2f}%" if chp is not None else "N/A"

    print(
        f"{now.strftime('%H:%M:%S'):<10} | {FUT_SYMBOL:<20} | {fmt(ltp):>10} | "
        f"{fmt_int(cum_volume):>14} | {fmt_int(delta_vol):>10} | {fmt(state['vwap']):>10} | "
        f"{fmt(bid):>10} | {fmt(ask):>10} | {chp_str:>8}"
    )

def onerror(message):
    print("Error:", message)

def onclose(message):
    print("Closed:", message)

def onopen():
    fyers.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
    fyers.keep_running()

# --------------------------------------------------
# WebSocket
# --------------------------------------------------
fyers = data_ws.FyersDataSocket(
    access_token=access_token,
    log_path="",
    litemode=False,      # need full mode for volume/bid/ask
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