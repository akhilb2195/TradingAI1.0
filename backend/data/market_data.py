from fyers_apiv3.FyersWebsocket import data_ws
from fyers_apiv3 import fyersModel
from datetime import datetime
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

# --------------------------------------------------
# Access Token
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

# --------------------------------------------------
# REST Client
# --------------------------------------------------
fyers_rest = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path=""
)

# --------------------------------------------------
# Symbols
# --------------------------------------------------
SYMBOLS = [
    # "NSE:SBIN-EQ",
    # "NSE:ADANIENT-EQ",
    "NSE:NIFTY50-INDEX",
    # "NSE:INDIAVIX-INDEX",
    # "NSE:NIFTY26JUNFUT",
]

# --------------------------------------------------
# Header
# --------------------------------------------------
HEADER = (
    f"{'Time':<10} | {'Symbol':<22} | {'Exch':<5} | {'LTP':>10} | "
    f"{'Prev':>10} | {'Open':>10} | {'High':>10} | {'Low':>10} | "
    f"{'Volume':>14} | {'Bid':>10} | {'Ask':>10} | "
    f"{'Buy Qty':>12} | {'Sell Qty':>12} | "
    f"{'Change':>10} | {'%Change':>10}"
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
    except:
        return "N/A"

def fmt_int(val):
    if val is None:
        return "N/A"
    try:
        return f"{int(val):,}"
    except:
        return "N/A"

def parse_symbol(raw):
    try:
        exchange, symbol = raw.split(":", 1)
        return exchange, symbol
    except:
        return "N/A", raw

# --------------------------------------------------
# WebSocket Callbacks
# --------------------------------------------------
def onmessage(message):
    ltp = message.get("ltp")
    if ltp is None:
        return

    raw_symbol = message.get("symbol", "")
    exchange, symbol = parse_symbol(raw_symbol)

    prev        = message.get("prev_close_price")
    open_       = message.get("open_price")
    high        = message.get("high_price")
    low         = message.get("low_price")
    ch          = message.get("ch")
    chp         = message.get("chp")
    volume      = message.get("vol_traded_today")
    bid         = message.get("bid_price")       # fixed
    ask         = message.get("ask_price")       # fixed
    tot_buy     = message.get("tot_buy_qty")
    tot_sell    = message.get("tot_sell_qty")
    ts = message.get("last_traded_time") or message.get("exch_feed_time")


    time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "N/A"
    ch_str   = f"{ch:+.2f}"   if ch  is not None else "N/A"
    chp_str  = f"{chp:+.2f}%" if chp is not None else "N/A"

    print(
        f"{time_str:<10} | {symbol:<22} | {exchange:<5} | {fmt(ltp):>10} | "
        f"{fmt(prev):>10} | {fmt(open_):>10} | {fmt(high):>10} | {fmt(low):>10} | "
        f"{fmt_int(volume):>14} | {fmt(bid):>10} | {fmt(ask):>10} | "
        f"{fmt_int(tot_buy):>12} | {fmt_int(tot_sell):>12} | "
        f"{ch_str:>10} | {chp_str:>10}"
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
    litemode=False,
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