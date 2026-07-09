"""
historical_seed.py
-------------------
Pulls the last N already-closed candles from Fyers' REST history API
(same endpoint fyers_history.py uses) and converts them into the exact
candle-dict shape indicator_engine.py expects.

WHY THIS EXISTS:
EMA21 needs 21 candles, rolling20 needs 20, RSI14/ATR14 need 14-15. Built
purely from live ticks starting at 09:15, that's up to ~105 minutes of
"warming up" every single morning with no real indicator value. But EMA/
RSI/ATR are not supposed to reset every session on a real chart - they
carry forward from prior candles. So instead of waiting, we fetch the
tail end of PRIOR trading days' candles once at startup and feed them
into the engine before today's live candles even begin. By the time the
first live candle closes today, the indicators are already `ready` with
real, continuous values - not fabricated, not guessed, just the same
history a broker terminal would already have baked in.

Deliberately fetches only up through YESTERDAY (range_to = yesterday),
never touching today's date, so there is zero overlap/double-counting
with the live Redis tick backfill that separately handles today from
09:15 onward in candle_indicators.py.

No dependency on Redis/producer/candle_patterns - only needs a live
fyersModel.FyersModel client (same one producer.py / fyers_history.py
already build), so it's reusable in any script that wants accurate
indicators from minute one.
"""

from datetime import datetime, timedelta


def fetch_seed_candles(fyers_client, symbol, resolution_minutes,
                        needed_candles=30, lookback_days=10):
    """
    Returns a chronologically-sorted list of CLOSED candle dicts ending
    strictly before today, ready to be fed straight into
    SymbolIndicatorEngine.update(). Returns [] (never raises) on any
    failure - callers should treat that as "fall back to warming up live"
    rather than a fatal error, since a holiday-heavy lookback window or a
    flaky API call shouldn't crash the whole pipeline.
    """
    today = datetime.now().date()
    range_to = today - timedelta(days=1)              # never touch today
    range_from = range_to - timedelta(days=lookback_days)

    data = {
        "symbol": symbol,
        "resolution": str(resolution_minutes),
        "date_format": "1",
        "range_from": range_from.strftime("%Y-%m-%d"),
        "range_to": range_to.strftime("%Y-%m-%d"),
        "cont_flag": "1",
        "oi_flag": "0",
    }

    try:
        response = fyers_client.history(data=data)
    except Exception as e:
        print(f"[SEED] {symbol}: history API call failed ({e}) - "
              f"will warm up live from 09:15 instead.")
        return []

    if response.get("s") != "ok":
        print(f"[SEED] {symbol}: history API returned "
              f"'{response.get('message', 'unknown error')}' - "
              f"will warm up live from 09:15 instead.")
        return []

    raw = response.get("candles", [])
    if not raw:
        print(f"[SEED] {symbol}: no historical candles in the lookback "
              f"window - will warm up live from 09:15 instead.")
        return []

    candles = []
    for ts, o, h, l, c, vol in raw:
        start_dt = datetime.fromtimestamp(ts)
        candles.append({
            "start": start_dt,
            "end": start_dt + timedelta(minutes=resolution_minutes),
            "open": o, "high": h, "low": l, "close": c,
            "first_vol": 0, "last_vol": vol,
            "synthetic": False,      # these are real closed candles
            "gap_affected": False,
        })

    candles.sort(key=lambda c: c["start"])
    return candles[-needed_candles:] if needed_candles else candles