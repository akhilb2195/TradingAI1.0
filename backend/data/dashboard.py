"""
Fyers Candlestick Pattern Analyzer — Streamlit UI
==================================================
Simple visual dashboard on top of your existing fetch + pattern-detection
logic. Shows:

    - Symbol / candle-type / period controls in the sidebar
    - A candlestick chart with pattern hits marked
    - A table of detected patterns (most recent first)
    - Summary counts of each pattern over the selected range

Run:
    streamlit run streamlit_app.py

Requires (in addition to what fyers_history.py / pattern_analyzer.py need):
    pip install streamlit plotly
"""

import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from fyers_apiv3 import fyersModel

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

# ==================================================
# Page setup
# ==================================================
st.set_page_config(page_title="Candlestick Pattern Analyzer", layout="wide")

# ==================================================
# Fixed option maps (same as pattern_analyzer.py)
# ==================================================
SYMBOL_OPTIONS = {
    "Nifty 50 Index":  "NSE:NIFTY50-INDEX",
    "BankNifty Index": "NSE:NIFTYBANK-INDEX",
    "FinNifty Index":  "NSE:FINNIFTY-INDEX",
    "Midcap Nifty":    "NSE:MIDCPNIFTY-INDEX",
    "India VIX":       "NSE:INDIAVIX-INDEX",
    "Other (type below)": None,
}

CANDLE_TYPES = {
    "1 Min":   "1",
    "5 Min":   "5",
    "15 Min":  "15",
    "30 Min":  "30",
    "60 Min":  "60",
    "Daily":   "D",
    "Weekly":  "1W",
    "Monthly": "1M",
}

PERIODS = {
    "1 Month":  30,
    "3 Months": 90,
    "6 Months": 180,
    "1 Year":   365,
}

# ==================================================
# Fyers client (cached so we don't rebuild every rerun)
# ==================================================
@st.cache_resource
def get_fyers_client():
    with open("access_token.txt", "r") as f:
        access_token = f.read().strip()
    return fyersModel.FyersModel(
        client_id=client_id,
        is_async=False,
        token=access_token,
        log_path="",
    )


# ==================================================
# 1. FETCH  (cached per symbol/resolution/days combo)
# ==================================================
@st.cache_data(ttl=300, show_spinner=False)
def fetch_candles(symbol, resolution, days):
    fyers = get_fyers_client()
    now = datetime.now()
    end = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    data = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": start,
        "range_to": end,
        "cont_flag": "1",
        "oi_flag": "0",
    }
    resp = fyers.history(data=data)
    if resp.get("s") != "ok":
        raise RuntimeError(f"Fyers error [{resp.get('code')}]: {resp.get('message')}")

    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError("No candles returned for this range.")

    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="s")
    df = df[["datetime", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return df


# ==================================================
# 2. BASE MEASUREMENTS
# ==================================================
def add_base_columns(df, atr_period=14, trend_lookback=10):
    df = df.copy()
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["bullish"] = df["close"] > df["open"]
    df["bearish"] = df["close"] < df["open"]

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_period, min_periods=1).mean()

    sma = df["close"].rolling(trend_lookback, min_periods=trend_lookback).mean()
    df["prior_uptrend"] = df["close"].shift(1) > sma.shift(1)
    df["prior_downtrend"] = df["close"].shift(1) < sma.shift(1)

    return df


# ==================================================
# 3. CANDLESTICK PATTERNS  (identical rules to pattern_analyzer.py)
# ==================================================
def detect_candlestick_patterns(df):
    df = df.copy()
    n = len(df)
    patterns = [[] for _ in range(n)]

    body = df["body"]
    rng = df["range"]
    up_w = df["upper_wick"]
    low_w = df["lower_wick"]
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    SMALL_BODY = 0.35
    DOJI_BODY = 0.1
    LONG_WICK_MULT = 2.0
    MARUBOZU_WICK_MAX = 0.05

    small_body = body <= SMALL_BODY * rng
    tiny_body = body <= DOJI_BODY * rng

    doji = tiny_body
    for i in np.where(doji)[0]:
        patterns[i].append("Doji")

    hammer_shape = small_body & (low_w >= LONG_WICK_MULT * body) & (up_w <= 0.15 * rng)
    for i in np.where(hammer_shape & df["prior_downtrend"])[0]:
        patterns[i].append("Hammer")
    for i in np.where(hammer_shape & df["prior_uptrend"])[0]:
        patterns[i].append("Hanging Man")

    inv_hammer_shape = small_body & (up_w >= LONG_WICK_MULT * body) & (low_w <= 0.15 * rng)
    for i in np.where(inv_hammer_shape & df["prior_downtrend"])[0]:
        patterns[i].append("Inverted Hammer")
    for i in np.where(inv_hammer_shape & df["prior_uptrend"])[0]:
        patterns[i].append("Shooting Star")

    marubozu = (up_w <= MARUBOZU_WICK_MAX * rng) & (low_w <= MARUBOZU_WICK_MAX * rng) & (body >= 0.9 * rng)
    for i in np.where(marubozu & df["bullish"])[0]:
        patterns[i].append("Bullish Marubozu")
    for i in np.where(marubozu & df["bearish"])[0]:
        patterns[i].append("Bearish Marubozu")

    for i in range(1, n):
        po, ph, pl, pc = o[i-1], h[i-1], l[i-1], c[i-1]
        co, ch, cl, cc = o[i], h[i], l[i], c[i]
        prev_body = body[i-1]
        cur_body = body[i]

        if df["bearish"][i-1] and df["bullish"][i] and co <= pc and cc >= po and cur_body > prev_body:
            patterns[i].append("Bullish Engulfing")
        if df["bullish"][i-1] and df["bearish"][i] and co >= pc and cc <= po and cur_body > prev_body:
            patterns[i].append("Bearish Engulfing")

        if cur_body < prev_body * 0.6:
            if df["bearish"][i-1] and df["bullish"][i] and co >= pc and cc <= po:
                patterns[i].append("Bullish Harami")
            if df["bullish"][i-1] and df["bearish"][i] and co <= pc and cc >= po:
                patterns[i].append("Bearish Harami")

        if ch <= ph and cl >= pl:
            patterns[i].append("Inside Bar")

        if ch >= ph and cl <= pl:
            patterns[i].append("Outside Bar")

        HIGH_LOW_TOL = 0.1
        tol = df["atr"][i] * HIGH_LOW_TOL if not np.isnan(df["atr"][i]) else 0
        if abs(ch - ph) <= tol and df["prior_uptrend"][i]:
            patterns[i].append("Tweezer Top")
        if abs(cl - pl) <= tol and df["prior_downtrend"][i]:
            patterns[i].append("Tweezer Bottom")

    for i in range(2, n):
        b0, b1, b2 = body[i-2], body[i-1], body[i]

        if (df["bearish"][i-2] and b0 > df["range"][i-2] * 0.5
                and b1 <= 0.35 * df["range"][i-1]
                and df["bullish"][i] and b2 > df["range"][i] * 0.5
                and c[i] > (o[i-2] + c[i-2]) / 2):
            patterns[i].append("Morning Star")

        if (df["bullish"][i-2] and b0 > df["range"][i-2] * 0.5
                and b1 <= 0.35 * df["range"][i-1]
                and df["bearish"][i] and b2 > df["range"][i] * 0.5
                and c[i] < (o[i-2] + c[i-2]) / 2):
            patterns[i].append("Evening Star")

        if (df["bullish"][i-2] and df["bullish"][i-1] and df["bullish"][i]
                and o[i-1] > o[i-2] and o[i-1] < c[i-2]
                and o[i] > o[i-1] and o[i] < c[i-1]
                and c[i-1] > c[i-2] and c[i] > c[i-1]
                and b0 > df["range"][i-2] * 0.5 and b1 > df["range"][i-1] * 0.5 and b2 > df["range"][i] * 0.5):
            patterns[i].append("Three White Soldiers")

        if (df["bearish"][i-2] and df["bearish"][i-1] and df["bearish"][i]
                and o[i-1] < o[i-2] and o[i-1] > c[i-2]
                and o[i] < o[i-1] and o[i] > c[i-1]
                and c[i-1] < c[i-2] and c[i] < c[i-1]
                and b0 > df["range"][i-2] * 0.5 and b1 > df["range"][i-1] * 0.5 and b2 > df["range"][i] * 0.5):
            patterns[i].append("Three Black Crows")

    df["candlestick_patterns"] = patterns
    df["pattern_str"] = df["candlestick_patterns"].apply(lambda p: ", ".join(p) if p else "")
    return df


# --------------------------------------------------
# Simple bullish / bearish / neutral classification,
# used only to color-code the signal strip below the chart.
# --------------------------------------------------
PATTERN_BIAS = {
    "Hammer": "Bullish", "Inverted Hammer": "Bullish", "Bullish Engulfing": "Bullish",
    "Bullish Harami": "Bullish", "Bullish Marubozu": "Bullish", "Morning Star": "Bullish",
    "Three White Soldiers": "Bullish", "Tweezer Bottom": "Bullish",
    "Hanging Man": "Bearish", "Shooting Star": "Bearish", "Bearish Engulfing": "Bearish",
    "Bearish Harami": "Bearish", "Bearish Marubozu": "Bearish", "Evening Star": "Bearish",
    "Three Black Crows": "Bearish", "Tweezer Top": "Bearish",
    "Doji": "Neutral", "Inside Bar": "Neutral", "Outside Bar": "Neutral",
}
BIAS_COLOR = {"Bullish": "#22c55e", "Bearish": "#ef4444", "Neutral": "#9ca3af", "Mixed": "#f59e0b"}


def row_bias(pattern_list):
    """One overall bias label for a row that may hold several pattern names."""
    biases = {PATTERN_BIAS.get(p, "Neutral") for p in pattern_list}
    if "Bullish" in biases and "Bearish" in biases:
        return "Mixed"
    if "Bullish" in biases:
        return "Bullish"
    if "Bearish" in biases:
        return "Bearish"
    return "Neutral"


# ==================================================
# 4. SIDEBAR CONTROLS
# ==================================================
st.sidebar.title("⚙️ Settings")

theme_choice = st.sidebar.selectbox("Theme", ["Light", "Dark"], index=0)
plotly_template = "plotly_white" if theme_choice == "Light" else "plotly_dark"

if theme_choice == "Dark":
    st.markdown(
        """
        <style>
        .stApp { background-color: #0e1117; color: #fafafa; }
        section[data-testid="stSidebar"] { background-color: #161a23; }
        </style>
        """,
        unsafe_allow_html=True,
    )

st.sidebar.divider()

symbol_choice = st.sidebar.selectbox("Symbol", list(SYMBOL_OPTIONS.keys()))
if SYMBOL_OPTIONS[symbol_choice] is None:
    symbol = st.sidebar.text_input("Enter symbol", "NSE:SBIN-EQ").strip().upper()
else:
    symbol = SYMBOL_OPTIONS[symbol_choice]

candle_choice = st.sidebar.selectbox("Candle Type", list(CANDLE_TYPES.keys()), index=5)
resolution = CANDLE_TYPES[candle_choice]

period_choice = st.sidebar.selectbox("Period", list(PERIODS.keys()), index=1)
days = PERIODS[period_choice]

run = st.sidebar.button("🔄 Fetch & Analyze", type="primary")

st.title("🕯️ Candlestick Pattern Analyzer")
st.caption(f"{symbol}  •  {candle_choice}  •  {period_choice}")

# ==================================================
# 5. MAIN LOGIC
# ==================================================
if run or "df" in st.session_state:
    if run:
        with st.spinner("Fetching candles..."):
            try:
                raw = fetch_candles(symbol, resolution, days)
                df = add_base_columns(raw)
                df = detect_candlestick_patterns(df)
                st.session_state["df"] = df
            except Exception as e:
                st.error(f"⚠️ {e}")
                st.stop()

    df = st.session_state["df"]

    # --- Basic details / KPIs ---
    latest = df.iloc[-1]
    prev_close = df["close"].iloc[-2] if len(df) > 1 else latest["open"]
    change = latest["close"] - prev_close
    pct = (change / prev_close * 100) if prev_close else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Candles", len(df))
    c2.metric("Last Close", f"{latest['close']:.2f}", f"{change:+.2f} ({pct:+.2f}%)")
    c3.metric("Period High", f"{df['high'].max():.2f}")
    c4.metric("Period Low", f"{df['low'].min():.2f}")
    hits = df[df["candlestick_patterns"].apply(len) > 0]
    c5.metric("Pattern Hits", len(hits))

    st.divider()

    # --- Price chart (clean candles) + Volume + Pattern signal strip ---
    st.subheader("📊 Price Chart")
    st.caption(
        "Top: candlesticks (drag to zoom, use the slider to move around). "
        "Middle: volume. Bottom: a dot for every candle that had a pattern — "
        "🟢 green = bullish signal, 🔴 red = bearish signal, ⚪ gray = neutral, 🟠 orange = mixed."
    )

    is_intraday = resolution not in ("D", "1W", "1M")

    all_patterns = sorted({p for row in df["candlestick_patterns"] for p in row})
    selected_patterns = st.multiselect(
        "Filter which patterns appear in the signal strip (all shown by default)",
        options=all_patterns,
        default=all_patterns,
    )

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.18, 0.17],
        vertical_spacing=0.03,
        specs=[[{"type": "candlestick"}], [{"type": "bar"}], [{"type": "scatter"}]],
    )

    # Row 1: clean candlesticks, no markers on top
    fig.add_trace(go.Candlestick(
        x=df["datetime"],
        open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=symbol, showlegend=False,
    ), row=1, col=1)

    # Row 2: volume, colored by up/down candle
    vol_colors = np.where(df["bullish"], "#22c55e", "#ef4444")
    fig.add_trace(go.Bar(
        x=df["datetime"], y=df["volume"], marker_color=vol_colors,
        name="Volume", showlegend=False,
    ), row=2, col=1)

    # Row 3: one dot per candle-with-a-pattern, colored by bias, in its own lane
    filtered = df["candlestick_patterns"].apply(lambda ps: [p for p in ps if p in selected_patterns])
    sig = df[filtered.apply(len) > 0].copy()
    sig["shown_patterns"] = filtered[filtered.apply(len) > 0].apply(", ".join)
    sig["bias"] = filtered[filtered.apply(len) > 0].apply(row_bias)

    for bias, color in BIAS_COLOR.items():
        subset = sig[sig["bias"] == bias]
        if subset.empty:
            continue
        fig.add_trace(go.Scatter(
            x=subset["datetime"], y=[1] * len(subset),
            mode="markers", marker=dict(size=10, color=color),
            name=bias, text=subset["shown_patterns"],
            hovertemplate="%{x}<br>%{text}<extra>" + bias + "</extra>",
        ), row=3, col=1)

    rangebreaks = [dict(bounds=["sat", "mon"])]  # always skip weekends
    if is_intraday:
        # TUNE: adjust to your exchange's actual trading hours if different
        rangebreaks.append(dict(bounds=[15.5, 9.25], pattern="hour"))

    fig.update_yaxes(visible=False, row=3, col=1, range=[0.5, 1.5])
    fig.update_xaxes(rangebreaks=rangebreaks, row=1, col=1)
    fig.update_xaxes(rangebreaks=rangebreaks, row=2, col=1)
    fig.update_xaxes(
        rangebreaks=rangebreaks, row=3, col=1,
        rangeslider=dict(visible=True, thickness=0.08),
        rangeselector=dict(
            buttons=[
                dict(count=5, label="5d", step="day", stepmode="backward"),
                dict(count=1, label="1m", step="month", stepmode="backward"),
                dict(count=3, label="3m", step="month", stepmode="backward"),
                dict(step="all", label="All"),
            ],
            y=1.16,
        ),
    )

    fig.update_layout(
        height=750,
        template=plotly_template,
        margin=dict(l=10, r=10, t=60, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis_rangeslider_visible=False,  # candlestick row's built-in slider off; row 3 has the real one
        bargap=0.2,
    )
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # --- Recent pattern hits table ---
    left, right = st.columns([3, 2])

    with left:
        st.subheader("🔎 Recent Pattern Hits")
        if hits.empty:
            st.info("No patterns found in this range.")
        else:
            show = hits[["datetime", "open", "high", "low", "close", "pattern_str"]].tail(30).iloc[::-1]
            show = show.rename(columns={"pattern_str": "patterns"})
            st.dataframe(show, use_container_width=True, hide_index=True)

    with right:
        st.subheader("📈 Pattern Counts")
        counts = {}
        for row_patterns in df["candlestick_patterns"]:
            for p in row_patterns:
                counts[p] = counts.get(p, 0) + 1
        if counts:
            counts_df = pd.DataFrame(
                sorted(counts.items(), key=lambda x: -x[1]),
                columns=["Pattern", "Count"],
            )
            st.dataframe(counts_df, use_container_width=True, hide_index=True)
            st.bar_chart(counts_df.set_index("Pattern"))
        else:
            st.info("No patterns found in this range.")

    st.divider()

    with st.expander("📄 Raw candle data", expanded=False):
        st.caption(f"Full dataset — {len(df)} rows. Scroll inside the table below; it's virtualized so all rows are there even though only a portion renders at a time.")
        show_internal = st.checkbox("Show internal calculation columns (ATR, wicks, etc.)", value=False)
        newest_first = st.checkbox("Newest first", value=True)

        base_cols = ["datetime", "open", "high", "low", "close", "volume", "pattern_str"]
        internal_cols = ["body", "range", "upper_wick", "lower_wick", "bullish", "bearish",
                          "atr", "prior_uptrend", "prior_downtrend"]
        cols = base_cols + internal_cols if show_internal else base_cols

        display_df = df[cols].copy()
        if newest_first:
            display_df = display_df.iloc[::-1]
        display_df = display_df.reset_index(drop=True)
        display_df = display_df.rename(columns={"pattern_str": "patterns"})
        display_df["datetime"] = display_df["datetime"].dt.strftime("%Y-%m-%d %H:%M")
        num_cols = display_df.select_dtypes(include="number").columns
        display_df[num_cols] = display_df[num_cols].round(2)

        st.dataframe(display_df, use_container_width=True, hide_index=True, height=600)

        csv = df.drop(columns=["candlestick_patterns"]).to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download full data as CSV", csv, "candle_data.csv", "text/csv")

else:
    st.info("👈 Set your options in the sidebar and click **Fetch & Analyze** to begin.")