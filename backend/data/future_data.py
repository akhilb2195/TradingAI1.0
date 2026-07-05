"""
future_data.py
--------------
Live futures data subscriber for TradingAi - Module 1
Tracks spot + current month + next month futures for NIFTY / BANKNIFTY.

Calculates in real-time:
  - OI Change
  - Futures Premium / Discount vs Spot
  - Long Buildup / Short Buildup / Long Unwinding / Short Covering

Run from the backend/ folder:
    cd TradingAi/backend
    python data/future_data.py
"""

import sys
import os
import calendar
from datetime import datetime, date

# ── Credentials ───────────────────────────────────────────────────────────────
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

from fyers_apiv3.FyersWebsocket import data_ws

# ── Config ────────────────────────────────────────────────────────────────────
INSTRUMENT = "NIFTY"          # Change to "BANKNIFTY" if needed

SPOT_SYMBOL = {
    "NIFTY":     "NSE:NIFTY50-INDEX",
    "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
}[INSTRUMENT]

LOT_SIZE = {
    "NIFTY":     75,
    "BANKNIFTY": 35,
}[INSTRUMENT]

# ── Symbol Builder ────────────────────────────────────────────────────────────
MONTH_ABBR = ["JAN","FEB","MAR","APR","MAY","JUN",
               "JUL","AUG","SEP","OCT","NOV","DEC"]

def _last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of the given month (Fyers F&O expiry day)."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    offset = (d.weekday() - 3) % 7
    return date(year, month, last_day - offset)

def futures_symbol(instrument: str, year: int, month: int) -> str:
    """Build Fyers futures symbol e.g. NSE:NIFTY26JULFUT"""
    yy  = str(year)[-2:]
    mon = MONTH_ABBR[month - 1]
    return f"NSE:{instrument}{yy}{mon}FUT"

def get_active_futures(instrument: str):
    """
    Return (current_symbol, current_expiry, next_symbol, next_expiry).
    Automatically rolls over if today is past the current month expiry.
    """
    today = date.today()
    y, m  = today.year, today.month

    expiry_cur = _last_thursday(y, m)

    if today > expiry_cur:
        # Current month expired — shift forward
        m1 = m % 12 + 1
        y1 = y + (1 if m == 12 else 0)
        m2 = m1 % 12 + 1
        y2 = y1 + (1 if m1 == 12 else 0)
    else:
        m1, y1 = m, y
        m2 = m % 12 + 1
        y2 = y + (1 if m == 12 else 0)

    expiry_cur  = _last_thursday(y1, m1)
    expiry_next = _last_thursday(y2, m2)

    cur_sym  = futures_symbol(instrument, y1, m1)
    next_sym = futures_symbol(instrument, y2, m2)

    return cur_sym, str(expiry_cur), next_sym, str(expiry_next)

CUR_SYM, CUR_EXPIRY, NEXT_SYM, NEXT_EXPIRY = get_active_futures(INSTRUMENT)

SYMBOLS = [SPOT_SYMBOL, CUR_SYM, NEXT_SYM]

# ── Shared Market State ───────────────────────────────────────────────────────
market_state = {
    "spot": {
        "symbol":     SPOT_SYMBOL,
        "ltp":        0.0,
        "open":       0.0,
        "high":       0.0,
        "low":        0.0,
        "prev_close": 0.0,
    },
    "current_future": {
        "symbol":    CUR_SYM,
        "expiry":    CUR_EXPIRY,
        "lot_size":  LOT_SIZE,
        "ltp":       0.0,
        "open":      0.0,
        "high":      0.0,
        "low":       0.0,
        "volume":    0,
        "oi":        0,
        "oi_change": 0,
        "premium":   0.0,
        "signal":    "",
    },
    "next_future": {
        "symbol":    NEXT_SYM,
        "expiry":    NEXT_EXPIRY,
        "lot_size":  LOT_SIZE,
        "ltp":       0.0,
        "open":      0.0,
        "high":      0.0,
        "low":       0.0,
        "volume":    0,
        "oi":        0,
        "oi_change": 0,
        "premium":   0.0,
        "signal":    "",
    },
    "last_update": "",
}

# ── Signal Detection ──────────────────────────────────────────────────────────
_prev_ltp = {CUR_SYM: 0.0, NEXT_SYM: 0.0}

def oi_signal(ltp: float, prev_ltp: float, oi_change: int) -> str:
    price_up = ltp >= prev_ltp
    oi_up    = oi_change >= 0
    if price_up and oi_up:         return "Long Buildup"
    if not price_up and oi_up:     return "Short Buildup"
    if price_up and not oi_up:     return "Short Covering"
    return "Long Unwinding"

# ── Dashboard ─────────────────────────────────────────────────────────────────
def print_dashboard() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    s  = market_state["spot"]
    cf = market_state["current_future"]
    nf = market_state["next_future"]
    ts = market_state["last_update"]

    print(f"{'=' * 62}")
    print(f"  TradingAi — Futures Dashboard  [{INSTRUMENT}]   {ts}")
    print(f"{'=' * 62}")

    print(f"  SPOT   {s['symbol']}")
    print(f"  LTP: {s['ltp']:.2f}   O:{s['open']:.2f}  H:{s['high']:.2f}  L:{s['low']:.2f}  PC:{s['prev_close']:.2f}")
    print(f"{'-' * 62}")

    for label, fut in [("CUR FUT", cf), ("NXT FUT", nf)]:
        prem = f"+{fut['premium']:.2f}" if fut['premium'] >= 0 else f"{fut['premium']:.2f}"
        print(f"  {label}  {fut['symbol']}   Expiry: {fut['expiry']}")
        print(f"  LTP: {fut['ltp']:.2f}   O:{fut['open']:.2f}  H:{fut['high']:.2f}  L:{fut['low']:.2f}")
        print(f"  Vol: {fut['volume']:>12,}   OI: {fut['oi']:>12,}   OI Chg: {fut['oi_change']:>+8,}")
        print(f"  Premium: {prem:>8}            Signal: {fut['signal']}")
        print(f"{'-' * 62}")

    print()

# ── WebSocket Callbacks ───────────────────────────────────────────────────────
def on_message(msg: dict) -> None:
    sym = msg.get("symbol", "")

    ltp        = float(msg.get("ltp", 0))
    open_price = float(msg.get("open_price", 0))
    high_price = float(msg.get("high_price", 0))
    low_price  = float(msg.get("low_price", 0))
    prev_close = float(msg.get("prev_close_price", 0))

    if sym == SPOT_SYMBOL:
        s = market_state["spot"]
        s["ltp"]        = ltp
        s["open"]       = open_price
        s["high"]       = high_price
        s["low"]        = low_price
        s["prev_close"] = prev_close

    elif sym in (CUR_SYM, NEXT_SYM):
        key = "current_future" if sym == CUR_SYM else "next_future"
        fut = market_state[key]

        volume  = int(msg.get("vol_traded_today", msg.get("volume", 0)))
        oi_now  = int(msg.get("oi", 0))
        oi_chg  = oi_now - fut["oi"] if fut["oi"] else 0
        spot    = market_state["spot"]["ltp"]
        premium = round(ltp - spot, 2) if spot else 0.0
        signal  = oi_signal(ltp, _prev_ltp[sym], oi_chg)

        fut["ltp"]       = ltp
        fut["open"]      = open_price
        fut["high"]      = high_price
        fut["low"]       = low_price
        fut["volume"]    = volume
        fut["oi"]        = oi_now
        fut["oi_change"] = oi_chg
        fut["premium"]   = premium
        fut["signal"]    = signal

        _prev_ltp[sym] = ltp

    market_state["last_update"] = datetime.now().strftime("%H:%M:%S")
    print_dashboard()


def on_error(msg: dict) -> None:
    print(f"[ERROR] {msg}")


def on_close(msg) -> None:
    print(f"[CLOSED] {msg}")


def on_open() -> None:
    print(f"[CONNECTED] Subscribing to: {SYMBOLS}")
    fyers_ws.subscribe(symbols=SYMBOLS, data_type="SymbolUpdate")
    fyers_ws.keep_running()

# ── Build Socket ──────────────────────────────────────────────────────────────
fyers_ws = data_ws.FyersDataSocket(
    access_token=f"{client_id}:{access_token}",
    log_path="",
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=on_open,
    on_close=on_close,
    on_error=on_error,
    on_message=on_message,
)

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  TradingAi — Futures Data Feed")
    print(f"  Instrument  : {INSTRUMENT}")
    print(f"  Spot        : {SPOT_SYMBOL}")
    print(f"  Current Fut : {CUR_SYM}  (expiry {CUR_EXPIRY})")
    print(f"  Next Fut    : {NEXT_SYM}  (expiry {NEXT_EXPIRY})")
    print(f"  Connecting...\n")

    try:
        fyers_ws.connect()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")