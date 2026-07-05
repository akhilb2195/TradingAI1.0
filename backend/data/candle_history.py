"""
Fyers Historical Data Fetcher — Interactive Menu
Run: python fyers_history.py
"""

from fyers_apiv3 import fyersModel
from datetime import datetime, timedelta
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
# Fyers REST Client
# --------------------------------------------------
fyers = fyersModel.FyersModel(
    client_id=client_id,
    is_async=False,
    token=access_token,
    log_path=""
)

# --------------------------------------------------
# Candle Resolutions  (label, api_value, is_intraday)
# --------------------------------------------------
CANDLE_TYPES = {
    # Seconds
    "1":  ("5 Seconds",  "5S",   True),
    "2":  ("10 Seconds", "10S",  True),
    "3":  ("15 Seconds", "15S",  True),
    "4":  ("30 Seconds", "30S",  True),
    "5":  ("45 Seconds", "45S",  True),
    # Minutes
    "6":  ("1 Min",      "1",    True),
    "7":  ("2 Min",      "2",    True),
    "8":  ("3 Min",      "3",    True),
    "9":  ("5 Min",      "5",    True),
    "10": ("10 Min",     "10",   True),
    "11": ("15 Min",     "15",   True),
    "12": ("20 Min",     "20",   True),
    "13": ("30 Min",     "30",   True),
    "14": ("60 Min",     "60",   True),
    "15": ("120 Min",    "120",  True),
    "16": ("240 Min",    "240",  True),
    # Daily / Weekly / Monthly
    "17": ("Daily",      "D",    False),
    "18": ("Weekly",     "1W",   False),
    "19": ("Monthly",    "1M",   False),
}

# --------------------------------------------------
# Period Presets
# --------------------------------------------------
PERIODS = {
    "1": ("Today",    1),
    "2": ("3 Days",   3),
    "3": ("1 Week",   7),
    "4": ("2 Weeks",  14),
    "5": ("1 Month",  30),
    "6": ("3 Months", 90),
    "7": ("6 Months", 180),
    "8": ("1 Year",   365),
    "9": ("2 Years",  730),
}

# --------------------------------------------------
# Symbol Quick Reference
# --------------------------------------------------
SYMBOL_GUIDE = [
    ("Nifty 50 Index",    "NSE:NIFTY50-INDEX"),
    ("BankNifty Index",   "NSE:NIFTYBANK-INDEX"),
    ("India VIX",         "NSE:INDIAVIX-INDEX"),
    ("FinNifty Index",    "NSE:FINNIFTY-INDEX"),
    ("Midcap Nifty",      "NSE:MIDCPNIFTY-INDEX"),
    ("Nifty Jun Futures", "NSE:NIFTY26JUNFUT"),
    ("BNF Jun Futures",   "NSE:BANKNIFTY26JUNFUT"),
    ("SBIN",              "NSE:SBIN-EQ"),
    ("Reliance",          "NSE:RELIANCE-EQ"),
    ("HDFC Bank",         "NSE:HDFCBANK-EQ"),
    ("Infosys",           "NSE:INFY-EQ"),
    ("Adani Ent",         "NSE:ADANIENT-EQ"),
]

# --------------------------------------------------
# Helpers
# --------------------------------------------------
def fmt(val, decimals=2):
    try:
        return f"{val:.{decimals}f}"
    except Exception:
        return "N/A"

def fmt_vol(val):
    try:
        return f"{int(val):,}"
    except Exception:
        return "N/A"

def sep(char="-", width=80):
    print(char * width)

def ask(prompt, choices: dict):
    while True:
        val = input(f"\n  {prompt}: ").strip()
        if val in choices:
            return val
        print(f"  [!] Enter a number between 1 and {max(choices, key=lambda x: int(x))}")

# --------------------------------------------------
# Menus
# --------------------------------------------------
def menu_symbol():
    sep("=")
    print("  FYERS HISTORICAL DATA FETCHER")
    sep("=")
    print("\n  SYMBOL REFERENCE (copy-paste):\n")
    for name, sym in SYMBOL_GUIDE:
        print(f"    {sym:<32} {name}")
    print()
    print("  Equity format  :  NSE:<TICKER>-EQ       e.g.  NSE:TCS-EQ")
    print("  Futures format :  NSE:<NAME><DDMMMFUT>  e.g.  NSE:NIFTY26JUNFUT")
    print()
    symbol = input("  Enter symbol: ").strip().upper()
    if not symbol:
        symbol = "NSE:NIFTY50-INDEX"
        print(f"  (defaulting to {symbol})")
    return symbol


def menu_candle_type():
    sep()
    print("  SELECT CANDLE TYPE\n")

    print("  ── Seconds (tick-level) ──────────────")
    for k in [str(i) for i in range(1, 6)]:
        label, _, _ = CANDLE_TYPES[k]
        print(f"    {k:>2}.  {label}")

    print("\n  ── Minutes (intraday) ────────────────")
    for k in [str(i) for i in range(6, 17)]:
        label, _, _ = CANDLE_TYPES[k]
        print(f"    {k:>2}.  {label}")

    print("\n  ── End of Day ────────────────────────")
    for k in ["17", "18", "19"]:
        label, _, _ = CANDLE_TYPES[k]
        print(f"    {k:>2}.  {label}")

    choice = ask("Enter candle type number", CANDLE_TYPES)
    label, resolution, intraday = CANDLE_TYPES[choice]
    return label, resolution, intraday


def menu_period():
    sep()
    print("  SELECT TIME PERIOD\n")
    for k, (label, _) in PERIODS.items():
        print(f"    {k}.  {label}")
    choice = ask("Enter period number", PERIODS)
    label, days = PERIODS[choice]
    return label, days


def menu_oi():
    sep()
    print("  OPEN INTEREST (OI) DATA\n")
    print("    1.  No  — standard OHLCV only")
    print("    2.  Yes — include OI column (futures & options only)")
    choices = {"1": False, "2": True}
    choice = ask("Include OI", choices)
    return choices[choice]

# --------------------------------------------------
# Fetch + Display
# --------------------------------------------------
def fetch_and_display(symbol, res_label, resolution, intraday, period_label, days, include_oi):
    now   = datetime.now()
    end   = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    sep()
    print(f"  Symbol    : {symbol}")
    print(f"  Candle    : {res_label}  [{resolution}]")
    print(f"  Period    : {period_label}  ({start}  →  {end})")
    print(f"  OI        : {'Yes' if include_oi else 'No'}")
    sep()
    print("  Fetching...")

    data = {
        "symbol":      symbol,
        "resolution":  resolution,
        "date_format": "1",          # 1 = YYYY-MM-DD (official docs)
        "range_from":  start,
        "range_to":    end,
        "cont_flag":   "1",
        "oi_flag":     "1" if include_oi else "0",
    }

    response = fyers.history(data=data)

    # ── Error handling ──────────────────────────
    if response.get("s") != "ok":
        code = response.get("code", "?")
        msg  = response.get("message", "Unknown error")
        print(f"\n  [ERROR {code}]  {msg}")
        if code == -300:
            print()
            print("  Symbol tips:")
            print("    Index   →  NSE:NIFTY50-INDEX  /  NSE:NIFTYBANK-INDEX")
            print("    Equity  →  NSE:SBIN-EQ")
            print("    Futures →  NSE:NIFTY26JUNFUT")
        print()
        return

    candles = response.get("candles", [])
    if not candles:
        print("\n  [!] No data — market may have been closed in this range.\n")
        return

    # ── Table ───────────────────────────────────
    ts_fmt = "%d %b %Y  %H:%M:%S" if intraday else "%d %b %Y"
    has_oi = include_oi and len(candles[0]) == 7   # API adds OI as 7th element

    if has_oi:
        HDR = (
            f"  {'Date/Time':<22} | {'Open':>10} | {'High':>10} | "
            f"{'Low':>10} | {'Close':>10} | {'Volume':>14} | {'OI':>12}"
        )
    else:
        HDR = (
            f"  {'Date/Time':<22} | {'Open':>10} | {'High':>10} | "
            f"{'Low':>10} | {'Close':>10} | {'Volume':>14}"
        )

    DIV = "  " + "-" * (len(HDR) - 2)

    print(f"\n  {len(candles)} candles  |  {symbol}  |  {res_label}  |  {start} → {end}\n")
    print(HDR)
    print(DIV)

    for c in candles:
        ts, o, h, l, cl, vol = c[:6]
        oi = c[6] if has_oi else None
        dt_str = datetime.fromtimestamp(ts).strftime(ts_fmt)
        row = (
            f"  {dt_str:<22} | {fmt(o):>10} | {fmt(h):>10} | "
            f"{fmt(l):>10} | {fmt(cl):>10} | {fmt_vol(vol):>14}"
        )
        if has_oi:
            row += f" | {fmt_vol(oi):>12}"
        print(row)

    print(DIV)
    print(f"\n  Done — {len(candles)} candles fetched.\n")

# --------------------------------------------------
# Entry Point
# --------------------------------------------------
if __name__ == "__main__":
    try:
        while True:
            symbol                        = menu_symbol()
            res_label, res, intraday      = menu_candle_type()
            period_label, days            = menu_period()
            include_oi                    = menu_oi()

            fetch_and_display(
                symbol, res_label, res, intraday,
                period_label, days, include_oi
            )

            sep()
            again = input("  Fetch another? (y / n): ").strip().lower()
            if again != "y":
                print("\n  Bye!\n")
                break

    except KeyboardInterrupt:
        print("\n\n  Stopped.\n")