"""
indicator_engine.py
-------------------
Per-symbol composition of the primitives in indicators.py into the exact
indicator set requested: EMA9, EMA21, RSI(14), ATR(14), rolling
high/low/avg-volume(20), session VWAP (cumulative from 09:15, resets
daily), and per-candle body/range/wick geometry.

Reusable anywhere a stream of CLOSED candles is available - this file has
no knowledge of Redis, Fyers, or how candles are built. Feed it candle
dicts shaped like the ones CandleBuilder in candle_patterns.py already
produces:
    {"open":.., "high":.., "low":.., "close":.., "start":.., "end":..,
     "last_vol":.., "first_vol":.., "synthetic":.., "gap_affected":..}

At market open (09:15) there is obviously no prior history, so every
indicator here reports ready=False and value=None until it has
accumulated enough candles:
    EMA9      needs   9 candles  (~45 min of 5-min candles)
    EMA21     needs  21 candles  (~105 min)
    RSI14     needs  15 candles  (14 changes -> needs 15 closes)
    ATR14     needs  14 candles
    Rolling20 needs  20 candles  (~100 min)
    VWAP      resets to empty every new trading day (by definition - it's
              a session total, not a lookback), and needs real traded
              volume; a symbol with no volume (e.g. an index) will report
              `unavailable_reason` instead of a fabricated number. Pass
              `volume_override` to .update() to feed volume from a proxy
              instrument instead (see candle_indicators.py).

Callers MUST check `ready` per indicator and must never treat None as
zero/neutral - that would be silently wrong, not just "less accurate".
"""

from indicators import EMA, WilderRSI, WilderATR, RollingWindow, SessionVWAP, candle_geometry


def _candle_volume(candle):
    """
    Works with the candle dicts produced by CandleBuilder in
    candle_patterns.py (which store first_vol/last_vol - cumulative
    day-counters from the exchange feed - rather than a plain per-candle
    volume field). Falls back to a plain "volume" key for any other
    candle source.
    """
    if "volume" in candle:
        return candle["volume"]
    v = candle.get("last_vol", 0) - candle.get("first_vol", 0)
    return v if v > 0 else candle.get("last_vol", 0)


class SymbolIndicatorEngine:
    """One of these per symbol. Call .update(candle) once per CLOSED
    candle, in chronological order, and get back a full snapshot dict."""

    def __init__(self, ema_fast=9, ema_slow=21, rsi_period=14,
                 atr_period=14, rolling_period=20):
        self.ema_fast = EMA(ema_fast)
        self.ema_slow = EMA(ema_slow)
        self.rsi = WilderRSI(rsi_period)
        self.atr = WilderATR(atr_period)
        self.rolling = RollingWindow(rolling_period)
        self.vwap = SessionVWAP()
        self.candles_seen = 0
        self.last_snapshot = None

    def update(self, candle, volume_override=None):
        # Synthetic ("no trade this minute") and gap-affected candles are
        # skipped for EMA/RSI/ATR/rolling - same rule candle_patterns.py
        # already applies to pattern detection. Feeding a fabricated flat
        # candle into ATR/RSI would quietly corrupt volatility/momentum.
        #
        # VWAP is treated separately: it still needs to see every candle
        # (even skipped ones) purely to detect a new trading session, but
        # a skipped candle always contributes zero volume/price to it.
        skip = bool(candle.get("synthetic") or candle.get("gap_affected"))
        self.candles_seen += 1

        close = candle["close"]
        high = candle["high"]
        low = candle["low"]

        # volume_override lets a caller supply volume from elsewhere (e.g.
        # a proxy futures contract's volume, when `candle` itself is for an
        # index that has no volume of its own - see candle_indicators.py).
        volume = volume_override if volume_override is not None else _candle_volume(candle)
        session_date = candle["start"].date() if candle.get("start") else None
        vwap_val = (self.vwap.update(high, low, close, 0 if skip else volume, session_date)
                    if session_date else self.vwap.value)

        if skip:
            snap = self._build_snapshot(candle, skipped=True, volume=volume, vwap=vwap_val)
            self.last_snapshot = snap
            return snap

        ema_fast_val = self.ema_fast.update(close)
        ema_slow_val = self.ema_slow.update(close)
        rsi_val = self.rsi.update(close)
        atr_val = self.atr.update(high, low, close)
        rolling_val = self.rolling.update(high, low, volume)

        snap = self._build_snapshot(
            candle, skipped=False,
            ema_fast=ema_fast_val, ema_slow=ema_slow_val,
            rsi=rsi_val, atr=atr_val, rolling=rolling_val,
            volume=volume, vwap=vwap_val,
        )
        self.last_snapshot = snap
        return snap

    def _build_snapshot(self, candle, skipped, ema_fast=None, ema_slow=None,
                         rsi=None, atr=None, rolling=None, volume=None, vwap=None):
        geometry = candle_geometry(candle)
        rolling = rolling or {}
        return {
            "start": candle.get("start"),
            "end": candle.get("end"),
            "skipped": skipped,  # True => synthetic/gap candle, EMA/RSI/ATR/rolling not updated
            "volume": volume,
            "candle_body": geometry["body"],
            "candle_range": geometry["range"],
            "upper_wick": geometry["upper_wick"],
            "lower_wick": geometry["lower_wick"],
            "ema9": {
                "value": ema_fast, "ready": self.ema_fast.ready,
                "candles_until_ready": self.ema_fast.candles_until_ready(),
            },
            "ema21": {
                "value": ema_slow, "ready": self.ema_slow.ready,
                "candles_until_ready": self.ema_slow.candles_until_ready(),
            },
            "rsi14": {
                "value": rsi, "ready": self.rsi.ready,
                "candles_until_ready": self.rsi.candles_until_ready(),
            },
            "atr14": {
                "value": atr, "ready": self.atr.ready,
                "candles_until_ready": self.atr.candles_until_ready(),
            },
            "rolling20": {
                "rolling_high": rolling.get("rolling_high"),
                "rolling_low": rolling.get("rolling_low"),
                "avg_volume": rolling.get("avg_volume"),
                "ready": self.rolling.ready,
                "candles_until_ready": self.rolling.candles_until_ready(),
            },
            "vwap": {
                "value": vwap,
                "session_volume": self.vwap.cum_volume,   # cumulative volume since 09:15 today
                "ready": self.vwap.ready,
                "unavailable_reason": self.vwap.unavailable_reason,
            },
        }


class IndicatorEngineRegistry:
    """Keeps one SymbolIndicatorEngine per symbol - mirrors the `builders`
    dict pattern already used in candle_patterns.py, so scripts tracking
    many symbols don't need their own bookkeeping."""

    def __init__(self, symbols, **engine_kwargs):
        self.engines = {sym: SymbolIndicatorEngine(**engine_kwargs) for sym in symbols}

    def update(self, symbol, candle):
        return self.engines[symbol].update(candle)

    def get(self, symbol):
        return self.engines[symbol]