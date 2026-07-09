"""
prev_day_ohlc.py
=================================================================
Fetch the previous trading day's High / Low / Close for any Fyers
symbol. Designed to be REUSABLE (import it) and RUNNABLE (execute
it directly for an interactive menu).

--------------------------------------------------------------
HOW TO USE THIS FROM ANOTHER FILE
--------------------------------------------------------------
    from prev_day_ohlc import get_fyers_client, get_previous_day_ohlc

    fyers = get_fyers_client()                          # create ONCE, reuse
    pdhlc = get_previous_day_ohlc("NSE:SBIN-EQ", fyers)

    if "error" not in pdhlc:
        print(pdhlc["high"], pdhlc["low"], pdhlc["close"])

    # Many symbols? Reuse the same client instead of creating a new one:
    from prev_day_ohlc import get_previous_day_ohlc_multi
    results = get_previous_day_ohlc_multi(
        ["NSE:SBIN-EQ", "NSE:NIFTY50-INDEX"], fyers
    )

--------------------------------------------------------------
RUN DIRECTLY
--------------------------------------------------------------
    python prev_day_ohlc.py
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, timedelta
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id


# --------------------------------------------------
# Client Factory — create once, pass around everywhere
# --------------------------------------------------
def get_fyers_client(token_path: str = "access_token.txt") -> fyersModel.FyersModel:
    """Build and return an authenticated FyersModel client."""
    with open(token_path, "r") as f:
        access_token = f.read().strip()

    return fyersModel.FyersModel(
        client_id=client_id,
        is_async=False,
        token=access_token,
        log_path=""
    )


# --------------------------------------------------
# Core Fetch Function
# --------------------------------------------------
def get_previous_day_ohlc(symbol: str, fyers: fyersModel.FyersModel = None,
                           lookback_days: int = 10) -> dict:
    """
    Fetch the previous COMPLETED trading day's OHLC + Volume.

    Args:
        symbol:        e.g. "NSE:SBIN-EQ", "NSE:NIFTY50-INDEX"
        fyers:         an existing client (reuse across calls). Creates
                       a new one internally if not provided.
        lookback_days: days to search back for the last completed
                       candle (covers weekends/holidays).

    Returns:
        dict with symbol, date, open, high, low, close, volume
        on failure: dict with symbol, error, code
    """
    if fyers is None:
        fyers = get_fyers_client()

    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    data = {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1",
        "oi_flag": "0",
    }

    response = fyers.history(data=data)

    if response.get("s") != "ok":
        return {
            "symbol": symbol,
            "error": response.get("message", "Unknown error"),
            "code": response.get("code", "?"),
        }

    candles = response.get("candles", [])
    if not candles:
        return {"symbol": symbol, "error": "No data returned"}

    # Skip today's candle if it's still forming (market open), so we
    # always return the last FULLY completed trading day.
    today_str = now.strftime("%Y-%m-%d")
    last_date_str = datetime.fromtimestamp(candles[-1][0]).strftime("%Y-%m-%d")
    prev_candle = candles[-2] if (last_date_str == today_str and len(candles) >= 2) else candles[-1]

    ts, o, h, l, c, vol = prev_candle[:6]
    return {
        "symbol": symbol,
        "date": datetime.fromtimestamp(ts).strftime("%d %b %Y"),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
    }


def get_previous_day_ohlc_multi(symbols: list, fyers: fyersModel.FyersModel = None) -> list:
    """Fetch previous-day OHLC for several symbols, reusing one client."""
    if fyers is None:
        fyers = get_fyers_client()
    return [get_previous_day_ohlc(sym, fyers) for sym in symbols]


def is_price_above_prev_high(symbol: str, ltp: float) -> bool:
    """
    Example of building a simple rule on top of the reusable fetcher:
    check whether a live price has broken above the previous day's high.

    `ltp` (last traded price) would come from your live quotes feed —
    not from this module, which only handles historical/previous-day data.
    """
    fyers = get_fyers_client()
    result = get_previous_day_ohlc(symbol, fyers)

    if "error" in result:
        print(f"[!] Skipping {symbol}: {result['error']}")
        return False

    return ltp > result["high"]


# --------------------------------------------------
# Small formatting helpers (reusable elsewhere too)
# --------------------------------------------------
def fmt(val, decimals: int = 2) -> str:
    try:
        return f"{val:.{decimals}f}"
    except Exception:
        return "N/A"


def fmt_vol(val) -> str:
    try:
        return f"{int(val):,}"
    except Exception:
        return "N/A"


def print_result(result: dict) -> None:
    if "error" in result:
        print(f"  [ERROR] {result['symbol']:<24} {result['error']}")
        return
    print(
        f"  {result['symbol']:<24} {result['date']:<14} "
        f"O:{fmt(result['open']):>10} H:{fmt(result['high']):>10} "
        f"L:{fmt(result['low']):>10} C:{fmt(result['close']):>10} "
        f"V:{fmt_vol(result['volume']):>14}"
    )


# --------------------------------------------------
# Interactive Symbol Menu
# --------------------------------------------------
SYMBOL_MENU = {
    "1": ("Nifty 50 Index",  "NSE:NIFTY50-INDEX"),
    "2": ("BankNifty Index", "NSE:NIFTYBANK-INDEX"),
    "3": ("FinNifty Index",  "NSE:FINNIFTY-INDEX"),
    "4": ("India VIX",       "NSE:INDIAVIX-INDEX"),
    "5": ("Equity (custom ticker)", None),   # asks for ticker below
}


def _select_symbol() -> str:
    print("\n  SELECT SYMBOL\n")
    for k, (label, sym) in SYMBOL_MENU.items():
        print(f"    {k}.  {label}" + (f"  [{sym}]" if sym else ""))

    choice = input("\n  Enter choice: ").strip()
    label, symbol = SYMBOL_MENU.get(choice, SYMBOL_MENU["1"])

    if symbol is None:                       # custom equity chosen
        ticker = input("  Enter NSE ticker (e.g. SBIN): ").strip().upper()
        symbol = f"NSE:{ticker}-EQ"

    return symbol


if __name__ == "__main__":
    fyers = get_fyers_client()               # one client for the whole session

    try:
        while True:
            symbol = _select_symbol()
            result = get_previous_day_ohlc(symbol, fyers)
            print()
            print_result(result)

            again = input("\n  Fetch another? (y/n): ").strip().lower()
            if again != "y":
                print("\n  Bye!\n")
                break
    except KeyboardInterrupt:
        print("\n\n  Stopped.\n")