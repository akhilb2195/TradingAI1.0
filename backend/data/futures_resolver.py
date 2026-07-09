"""
futures_resolver.py
--------------------
Automatically figures out the CURRENT active futures contract symbol for
a given index, so nobody has to manually edit a contract-month string
every month (e.g. NIFTY26JUNFUT -> NIFTY26JULFUT).

How it decides which month:
  1. Computes this month's expiry (last Thursday of the month - the
     standard NSE monthly-futures expiry day).
  2. If today is already past that expiry, it starts with NEXT month
     instead of the current one (the contract has rolled).
  3. To guard against edge cases (expiry falling on a holiday and
     shifting a day earlier, symbol not listed yet, etc.) it doesn't just
     trust the date math blindly - it actually asks Fyers for a few days
     of history on the candidate symbol. If that comes back empty/invalid,
     it tries the next month instead.
  4. Successful resolutions are cached per (index, calendar day) so this
     only ever costs one or two REST calls per index per day, not per
     candle.

No Redis/candle-building knowledge in this file at all - it only needs a
live fyersModel.FyersModel-like object with a `.history(data=...)` method
(the exact same client producer.py / candle_indicators.py already build).
"""

import calendar
from datetime import date, timedelta

# Map each index symbol to the underlying name used in its futures symbol.
# This mapping itself barely ever changes (unlike the contract month), so
# it's safe to keep static here.
INDEX_UNDERLYING = {
    "NSE:NIFTY50-INDEX":    "NIFTY",
    "NSE:NIFTYBANK-INDEX":  "BANKNIFTY",
    "NSE:FINNIFTY-INDEX":   "FINNIFTY",
    "NSE:MIDCPNIFTY-INDEX": "MIDCPNIFTY",
}

_MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

_resolve_cache = {}   # (index_symbol, date) -> resolved future symbol or None


def last_thursday(year, month):
    """The standard NSE monthly-futures expiry day for that month."""
    last_day_num = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day_num)
    offset = (d.weekday() - 3) % 7   # Thursday == 3
    return d - timedelta(days=offset)


def _next_month(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def candidate_months(today):
    """
    Returns [(year, month), (year, month)] - the most likely current
    contract month first, then the one after, as a fallback if the first
    guess turns out to already be expired/unlisted.
    """
    expiry = last_thursday(today.year, today.month)
    if today > expiry:
        y, m = _next_month(today.year, today.month)
    else:
        y, m = today.year, today.month
    y2, m2 = _next_month(y, m)
    return [(y, m), (y2, m2)]


def build_future_symbol(underlying, year, month):
    yy = str(year)[-2:]
    return f"NSE:{underlying}{yy}{_MONTH_ABBR[month - 1]}FUT"


def _symbol_has_recent_data(fyers_client, symbol):
    """Lightweight existence/liveness check via a few days of daily history."""
    try:
        end = date.today()
        start = end - timedelta(days=5)
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"),
            "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
            "oi_flag": "0",
        }
        response = fyers_client.history(data=data)
        return response.get("s") == "ok" and bool(response.get("candles"))
    except Exception:
        return False


def resolve_active_future(fyers_client, index_symbol):
    """
    Returns the current active futures symbol for `index_symbol` (e.g.
    "NSE:NIFTY26JULFUT"), or None if it can't be resolved - callers should
    treat None as "no volume proxy available", never guess a symbol.
    """
    today = date.today()
    cache_key = (index_symbol, today)
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]

    underlying = INDEX_UNDERLYING.get(index_symbol)
    if underlying is None:
        print(f"[FUTURES] {index_symbol}: not in INDEX_UNDERLYING - add it there "
              f"if you want auto volume-proxy resolution for this symbol.")
        _resolve_cache[cache_key] = None
        return None

    if fyers_client is None:
        _resolve_cache[cache_key] = None
        return None

    for year, month in candidate_months(today):
        symbol = build_future_symbol(underlying, year, month)
        if _symbol_has_recent_data(fyers_client, symbol):
            print(f"[FUTURES] {index_symbol}: auto-resolved volume proxy -> {symbol}")
            _resolve_cache[cache_key] = symbol
            return symbol

    print(f"[FUTURES] {index_symbol}: could not auto-resolve an active futures "
          f"contract - VWAP/volume will be unavailable for this symbol.")
    _resolve_cache[cache_key] = None
    return None