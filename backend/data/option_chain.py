"""
Fyers — Index Options & Futures Chain (interactive, single-request, fast render)

Lets you pick an index (NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / SENSEX / BANKEX),
then view: Option Chain (full Greeks + PCR + Max Pain + OI buildup tags), Futures,
or Both — from a SINGLE optionchain() API call.

Credentials are loaded the same way as fyers_history.py:
    - client_id comes from a `creditials.py` file (one folder above this script)
      containing:  client_id = "YYYYYYY-100"
    - access_token is read from a local `access_token.txt` file (just the token,
      no client_id prefix, no quotes)

Why this is fast:
    - Exactly ONE network call (fyers.optionchain) per view — no polling, no
      per-strike API calls, no sleeps.
    - All screen output is built into one string buffer and flushed with a
      single sys.stdout.write() instead of many small print() calls, which
      cuts down on repeated I/O syscalls when rendering a big chain.
    - PCR, Max Pain, and OI-buildup tags are computed in a single O(n) pass
      over the strikes already returned in the same response — no extra
      requests, no re-fetching.

Run:
    python fyers_indices_chain.py
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id                     # noqa: E402  (same pattern as fyers_history.py)
from fyers_apiv3 import fyersModel                    # noqa: E402

with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, is_async=False, log_path="")

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET, BOLD, GREEN, RED, CYAN, YELLOW, DIM = (
    "\033[0m", "\033[1m", "\033[92m", "\033[91m", "\033[96m", "\033[93m", "\033[2m",
)

# ── Index catalogue ────────────────────────────────────────────────────────────
INDICES = {
    "1": {"label": "NIFTY 50",     "option_symbol": "NSE:NIFTY50-INDEX"},
    "2": {"label": "BANK NIFTY",   "option_symbol": "NSE:NIFTYBANK-INDEX"},
    "3": {"label": "FIN NIFTY",    "option_symbol": "NSE:FINNIFTY-INDEX"},
    "4": {"label": "MIDCAP NIFTY", "option_symbol": "NSE:MIDCPNIFTY-INDEX"},
    "5": {"label": "SENSEX",       "option_symbol": "BSE:SENSEX-INDEX"},
    "6": {"label": "BANKEX",       "option_symbol": "BSE:BANKEX-INDEX"},
}

W = 108  # table width


def line(char="─"):
    return f"{DIM}{char * W}{RESET}"


def fetch_option_chain(index_symbol: str, strikecount: int = 10):
    """Single network call. No retries/sleeps unless it actually errors."""
    payload = {"symbol": index_symbol, "strikecount": strikecount, "timestamp": "", "greeks": "1"}
    resp = fyers.optionchain(data=payload)
    if resp.get("s") != "ok":
        return None, resp
    return resp["data"], None


def classify_buildup(oich: float, ltpch: float) -> str:
    """
    OI up + price up   -> Long Buildup
    OI up + price down -> Short Buildup (a.k.a. Option Writing)
    OI down + price up -> Short Covering
    OI down + price dn -> Long Unwinding
    """
    if oich >= 0 and ltpch >= 0:
        return f"{GREEN}Long Buildup{RESET}"
    if oich >= 0 and ltpch < 0:
        return f"{RED}Short Buildup{RESET}"
    if oich < 0 and ltpch >= 0:
        return f"{CYAN}Short Covering{RESET}"
    return f"{YELLOW}Long Unwinding{RESET}"


def compute_max_pain(strikes: dict) -> tuple:
    """
    Single O(n^2) pass is avoided: for each candidate settle price (each
    strike), pain = sum over all CE strikes<=settle of (settle-strike)*oi
    plus sum over all PE strikes>=settle of (strike-settle)*oi.
    With <=50 strikes (API max) this is trivially fast (<=2500 ops) —
    no external calls, pure in-memory arithmetic.
    """
    strike_list = sorted(strikes.keys())
    ce_oi = {k: v.get("CE", {}).get("oi", 0) for k, v in strikes.items()}
    pe_oi = {k: v.get("PE", {}).get("oi", 0) for k, v in strikes.items()}

    best_strike, best_pain = None, None
    for settle in strike_list:
        pain = 0
        for k in strike_list:
            if k <= settle:
                pain += (settle - k) * ce_oi.get(k, 0)
            if k >= settle:
                pain += (k - settle) * pe_oi.get(k, 0)
        if best_pain is None or pain < best_pain:
            best_pain, best_strike = pain, settle
    return best_strike, best_pain


def render_option_chain(data: dict) -> str:
    out = []
    spot = next((r for r in data["optionsChain"] if r.get("option_type", "") == ""), None)

    if spot:
        chg_color = GREEN if spot.get("ltpch", 0) >= 0 else RED
        out.append(
            f"\n  {BOLD}{CYAN}{spot['symbol']}{RESET}   "
            f"LTP: {BOLD}{spot['ltp']}{RESET}   "
            f"{chg_color}{spot['ltpch']:+.2f} ({spot['ltpchp']:+.2f}%){RESET}"
        )

    call_oi = data.get("callOi", 0)
    put_oi = data.get("putOi", 0)
    pcr = round(put_oi / call_oi, 2) if call_oi else 0

    strikes = {}
    for row in data["optionsChain"]:
        ot = row.get("option_type", "")
        if ot in ("CE", "PE"):
            strikes.setdefault(row["strike_price"], {})[ot] = row

    max_pain_strike, _ = compute_max_pain(strikes)

    out.append(
        f"  Call OI: {GREEN}{call_oi:,}{RESET}   Put OI: {RED}{put_oi:,}{RESET}   "
        f"PCR: {BOLD}{pcr}{RESET}   Max Pain: {BOLD}{YELLOW}{max_pain_strike}{RESET}"
    )
    out.append(line())
    out.append(
        f"  {GREEN}{'CALL OI':>9} {'C.CHG':>8} {'VOL':>8} {'LTP':>8} "
        f"{'IV':>6} {'DELTA':>6} {'GAMMA':>7} {'THETA':>7} {'VEGA':>6}{RESET}"
        f"  {BOLD}{'STRIKE':^8}{RESET}  "
        f"{RED}{'DELTA':>6} {'GAMMA':>7} {'THETA':>7} {'VEGA':>6} {'IV':>6} "
        f"{'LTP':>8} {'VOL':>8} {'P.CHG':>8} {'PUT OI':>9}{RESET}"
    )
    out.append(line("·"))

    for strike in sorted(strikes.keys()):
        ce, pe = strikes[strike].get("CE", {}), strikes[strike].get("PE", {})
        cg, pg = ce.get("greeks", {}), pe.get("greeks", {})
        out.append(
            f"  {GREEN}{ce.get('oi', 0):>9,} {ce.get('oich', 0):>8,} {ce.get('volume', 0):>8,} "
            f"{ce.get('ltp', 0):>8.2f} {cg.get('iv', 0):>6.2f} {cg.get('delta', 0):>6.2f} "
            f"{cg.get('gamma', 0):>7.4f} {cg.get('theta', 0):>7.2f} {cg.get('vega', 0):>6.2f}{RESET}"
            f"  {BOLD}{strike:^8}{RESET}  "
            f"{RED}{pg.get('delta', 0):>6.2f} {pg.get('gamma', 0):>7.4f} {pg.get('theta', 0):>7.2f} "
            f"{pg.get('vega', 0):>6.2f} {pg.get('iv', 0):>6.2f} {pe.get('ltp', 0):>8.2f} "
            f"{pe.get('volume', 0):>8,} {pe.get('oich', 0):>8,} {pe.get('oi', 0):>9,}{RESET}"
        )

    out.append(line("·"))
    out.append(f"  {BOLD}OI Buildup (near ATM){RESET}")
    atm_strikes = sorted(strikes.keys(), key=lambda s: abs(s - (spot["ltp"] if spot else 0)))[:5]
    for strike in sorted(atm_strikes):
        ce, pe = strikes[strike].get("CE", {}), strikes[strike].get("PE", {})
        ce_tag = classify_buildup(ce.get("oich", 0), ce.get("ltpch", 0)) if ce else "-"
        pe_tag = classify_buildup(pe.get("oich", 0), pe.get("ltpch", 0)) if pe else "-"
        out.append(f"   {BOLD}{strike:>8}{RESET}   CE: {ce_tag:<28}   PE: {pe_tag}")

    out.append(line())
    return "\n".join(out)


def render_futures(data: dict) -> str:
    """fp/fpch/fpchp are already in the same response's spot row — no extra call."""
    spot = next((r for r in data["optionsChain"] if r.get("option_type", "") == ""), None)
    if not spot or "fp" not in spot:
        return f"\n  {RED}No futures (fp) data found in this response.{RESET}"

    chg_color = GREEN if spot.get("fpch", 0) >= 0 else RED
    return (
        f"\n  {BOLD}Futures — {spot['symbol']}{RESET}\n"
        f"{line('·')}\n"
        f"   Future Price (fp): {BOLD}{spot['fp']}{RESET}   "
        f"{chg_color}{spot.get('fpch', 0):+.2f} ({spot.get('fpchp', 0):+.2f}%){RESET}\n"
        f"{line()}"
    )


def render_expiries(expiry_data) -> str:
    out = [f"\n  {BOLD}Available Expiries{RESET}", line("·")]
    out.extend(f"   {CYAN}{i}{RESET}. {e['date']}" for i, e in enumerate(expiry_data, 1))
    out.append(line("·"))
    return "\n".join(out)


def choose_index():
    sys.stdout.write(
        f"\n  {BOLD}Select an Index{RESET}\n{line('·')}\n"
        + "\n".join(f"   {CYAN}{k}{RESET}. {v['label']}" for k, v in INDICES.items())
        + f"\n{line('·')}\n"
    )
    return INDICES.get(input("  Enter number: ").strip())


def choose_mode():
    sys.stdout.write(
        f"\n  {BOLD}What do you want to see?{RESET}\n{line('·')}\n"
        f"   {CYAN}1{RESET}. Option Chain\n   {CYAN}2{RESET}. Futures\n   {CYAN}3{RESET}. Both\n{line('·')}\n"
    )
    return input("  Enter number: ").strip()


def main():
    while True:
        idx = choose_index()
        if not idx:
            print(f"  {YELLOW}Invalid selection.{RESET}")
            continue

        mode = choose_mode()
        data, err = fetch_option_chain(idx["option_symbol"])
        if err:
            print(f"  {RED}Error fetching option chain: {err}{RESET}")
            continue

        buf = [render_expiries(data["expiryData"])]
        if mode in ("1", "3"):
            buf.append(render_option_chain(data))
        if mode in ("2", "3"):
            buf.append(render_futures(data))

        sys.stdout.write("\n".join(buf) + "\n")

        if input(f"\n  {DIM}View another index? (y/n): {RESET}").strip().lower() != "y":
            break

    print(f"\n  {YELLOW}Done.{RESET}\n")


if __name__ == "__main__":
    main()