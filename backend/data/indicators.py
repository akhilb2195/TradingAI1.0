"""
indicators.py
-------------
Stateless-input / streaming-friendly technical indicator primitives.

Design goals:
  - Zero I/O, zero Redis/Fyers/symbol knowledge -> usable from ANY project,
    any candle source (Fyers, another broker, backtests, CSV replay...).
  - O(1) per-candle updates (no re-scanning full history every time a new
    candle closes), so a live stream never falls behind ("no delay").
  - Every indicator explicitly reports whether it has enough history yet
    (`ready`) instead of silently returning a misleading number during
    warm-up (e.g. at 09:15 there is no 21-candle history for EMA21 yet).

Feed each of these one CLOSED candle at a time, in chronological order.
"""

from collections import deque


def _get(candle, key):
    """Allow candles to be plain dicts (this codebase) or objects."""
    if isinstance(candle, dict):
        return candle[key]
    return getattr(candle, key)


# ----------------------------------------------------------------------
# Single-candle geometry (no history needed at all)
# ----------------------------------------------------------------------

def candle_body(candle):
    return abs(_get(candle, "close") - _get(candle, "open"))


def candle_range(candle):
    r = _get(candle, "high") - _get(candle, "low")
    return r if r > 0 else 1e-9  # avoid div-by-zero for downstream ratios


def upper_wick(candle):
    return _get(candle, "high") - max(_get(candle, "open"), _get(candle, "close"))


def lower_wick(candle):
    return min(_get(candle, "open"), _get(candle, "close")) - _get(candle, "low")


def candle_geometry(candle):
    """Convenience bundle of all four single-candle metrics at once."""
    return {
        "body": candle_body(candle),
        "range": candle_range(candle),
        "upper_wick": upper_wick(candle),
        "lower_wick": lower_wick(candle),
    }


# ----------------------------------------------------------------------
# EMA (Exponential Moving Average) - incremental, O(1) per update
# ----------------------------------------------------------------------

class EMA:
    """
    Standard EMA, seeded with a Simple Moving Average of the first `period`
    closes (the conventional seeding method), then updated incrementally
    forever after - each new candle costs one multiply-add, never a
    re-scan of history.
    """

    def __init__(self, period):
        self.period = period
        self.k = 2 / (period + 1)
        self._seed_buffer = []
        self.value = None
        self.ready = False

    def update(self, price):
        if self.ready:
            self.value = price * self.k + self.value * (1 - self.k)
            return self.value

        self._seed_buffer.append(price)
        if len(self._seed_buffer) < self.period:
            return None  # still warming up

        self.value = sum(self._seed_buffer) / self.period
        self.ready = True
        self._seed_buffer = None
        return self.value

    def candles_until_ready(self):
        if self.ready:
            return 0
        return self.period - len(self._seed_buffer)


# ----------------------------------------------------------------------
# RSI (14) - Wilder's smoothing method (the original/standard RSI formula)
# ----------------------------------------------------------------------

class WilderRSI:
    def __init__(self, period=14):
        self.period = period
        self._prev_close = None
        self._gains = []
        self._losses = []
        self.avg_gain = None
        self.avg_loss = None
        self.value = None
        self.ready = False

    def update(self, close):
        if self._prev_close is None:
            self._prev_close = close
            return None  # need at least 2 closes to have one change

        change = close - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._prev_close = close

        if self.ready:
            # Wilder smoothing: fold the new value in, weighted by (period-1)
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        else:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None  # still warming up
            self.avg_gain = sum(self._gains) / self.period
            self.avg_loss = sum(self._losses) / self.period
            self.ready = True
            self._gains = None
            self._losses = None

        if self.avg_loss == 0:
            self.value = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100 - (100 / (1 + rs))
        return self.value

    def candles_until_ready(self):
        if self.ready:
            return 0
        have = 0 if self._gains is None else len(self._gains)
        return self.period - have


# ----------------------------------------------------------------------
# ATR (14) - Wilder's smoothing of True Range
# ----------------------------------------------------------------------

class WilderATR:
    def __init__(self, period=14):
        self.period = period
        self._prev_close = None
        self._trs = []
        self.value = None
        self.ready = False

    def update(self, high, low, close):
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close

        if self.ready:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        else:
            self._trs.append(tr)
            if len(self._trs) < self.period:
                return None
            self.value = sum(self._trs) / self.period
            self.ready = True
            self._trs = None
        return self.value

    def candles_until_ready(self):
        if self.ready:
            return 0
        have = 0 if self._trs is None else len(self._trs)
        return self.period - have


# ----------------------------------------------------------------------
# Rolling High / Low / Avg Volume over a fixed candle window
# ----------------------------------------------------------------------

class RollingWindow:
    def __init__(self, period=20):
        self.period = period
        self._buf = deque(maxlen=period)

    def update(self, high, low, volume):
        self._buf.append((high, low, volume))
        if len(self._buf) < self.period:
            return None
        highs = [h for h, _, _ in self._buf]
        lows = [l for _, l, _ in self._buf]
        vols = [v for _, _, v in self._buf]
        return {
            "rolling_high": max(highs),
            "rolling_low": min(lows),
            "avg_volume": sum(vols) / self.period,
        }

    @property
    def ready(self):
        return len(self._buf) >= self.period

    def candles_until_ready(self):
        return max(0, self.period - len(self._buf))


# ----------------------------------------------------------------------
# VWAP - cumulative from session start (09:15), resets every new day.
#
# This is fundamentally different from EMA/RSI/ATR/RollingWindow above:
# those are fixed-length lookbacks that carry forward across sessions.
# VWAP is a running total that MUST reset to zero at the start of every
# trading day by definition - it answers "what has the average traded
# price been so far today", not "over the last N candles".
# ----------------------------------------------------------------------

class SessionVWAP:
    """
    Volume-Weighted Average Price = cumulative(typical_price * volume) /
    cumulative(volume), reset every time `session_date` changes.

    Needs real traded volume to mean anything. If a symbol never reports
    volume (a calculated index has none - it isn't a traded instrument),
    this will never become ready. After `no_volume_grace_candles` candles
    with zero cumulative volume, it explicitly reports why via
    `unavailable_reason` instead of silently returning a wrong number
    (e.g. from treating missing volume as volume=1).
    """

    def __init__(self, no_volume_grace_candles=5):
        self.session_date = None
        self.cum_price_volume = 0.0
        self.cum_volume = 0.0
        self.value = None
        self.candles_this_session = 0
        self.no_volume_grace_candles = no_volume_grace_candles
        self.unavailable_reason = None

    def update(self, high, low, close, volume, session_date):
        if session_date != self.session_date:
            self._reset_for_new_session(session_date)

        self.candles_this_session += 1

        if volume:  # None or 0 both mean "no contribution this candle"
            typical_price = (high + low + close) / 3
            self.cum_price_volume += typical_price * volume
            self.cum_volume += volume

        if self.cum_volume > 0:
            self.value = self.cum_price_volume / self.cum_volume
            self.unavailable_reason = None
        elif self.candles_this_session > self.no_volume_grace_candles:
            self.value = None
            self.unavailable_reason = (
                "no traded volume seen this session - typically means this "
                "is an index symbol (indices aren't traded, so they carry "
                "no volume); VWAP cannot be computed without real volume"
            )
        return self.value

    def _reset_for_new_session(self, session_date):
        self.session_date = session_date
        self.cum_price_volume = 0.0
        self.cum_volume = 0.0
        self.value = None
        self.candles_this_session = 0
        self.unavailable_reason = None

    @property
    def ready(self):
        return self.value is not None