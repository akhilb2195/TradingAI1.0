"""
Fyers WebSocket — Market Depth (Level 2) Feed
Zero-latency design: on_message only queues data; a background
thread handles all I/O so the WebSocket loop is never blocked.

Run:
    python fyers_depth_ws.py
"""

from fyers_apiv3.FyersWebsocket import data_ws
from datetime import datetime
from queue import Queue, Empty
import threading
import sys
import os

# ── Credentials (same pattern as fyers_history.py) ───────────────────────────
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from creditials import client_id

with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

FULL_TOKEN = f"{client_id}:{access_token}"   # "APPID:token" format Fyers expects

# ── Configuration ─────────────────────────────────────────────────────────────
SYMBOLS   = ["NSE:SBIN-EQ", "NSE:ADANIENT-EQ"]
DATA_TYPE = "DepthUpdate"    # "SymbolUpdate" for full quote; "DepthUpdate" for L2
LITE_MODE = False            # True → lighter payload (no order counts)

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
DIM    = "\033[2m"

# ── Print queue — decouples WebSocket thread from stdout I/O ──────────────────
_print_queue: Queue = Queue()

def _printer_loop() -> None:
    """Background thread: drain the queue and write to stdout."""
    while True:
        try:
            line = _print_queue.get(timeout=1)
            if line is None:          # sentinel → shutdown
                break
            sys.stdout.write(line)
        except Empty:
            continue
    sys.stdout.flush()

def qprint(*args, sep=" ", end="\n") -> None:
    """Drop-in for print() that never blocks the calling thread."""
    _print_queue.put(sep.join(str(a) for a in args) + end)

# Start the printer thread immediately
_printer_thread = threading.Thread(target=_printer_loop, daemon=True)
_printer_thread.start()

# ── Helpers ───────────────────────────────────────────────────────────────────
def sep(char="─", width=72) -> None:
    qprint(f"{DIM}{char * width}{RESET}")

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

# ── Depth renderer ────────────────────────────────────────────────────────────
def render_depth(msg: dict) -> None:
    symbol = msg.get("symbol", "—")
    lines = []

    lines.append(f"{DIM}{'─' * 72}{RESET}")
    lines.append(f"  {BOLD}{CYAN}{symbol}{RESET}  {DIM}{ts()}{RESET}")
    lines.append(f"{DIM}{'─' * 72}{RESET}")
    lines.append(
        f"  {DIM}{'LEVEL':<7}"
        f"{'BID QTY':>10}  {'BID PRICE':>10}  "
        f"{'ASK PRICE':>10}  {'ASK QTY':>10}  "
        f"{'BID ORD':>8}  {'ASK ORD':>8}{RESET}"
    )
    lines.append(f"{DIM}{'·' * 72}{RESET}")

    for i in range(1, 6):
        bp  = msg.get(f"bid_price{i}", 0)
        ap  = msg.get(f"ask_price{i}", 0)
        bsz = msg.get(f"bid_size{i}", 0)
        asz = msg.get(f"ask_size{i}", 0)
        bo  = msg.get(f"bid_order{i}", 0)
        ao  = msg.get(f"ask_order{i}", 0)
        lines.append(
            f"  {'L' + str(i):<7}"
            f"{GREEN}{bsz:>10,}  {bp:>10.2f}{RESET}  "
            f"{RED}{ap:>10.2f}  {asz:>10,}{RESET}  "
            f"{DIM}{bo:>8}  {ao:>8}{RESET}"
        )

    lines.append(f"{DIM}{'─' * 72}{RESET}")

    # Push entire block as one write — avoids interleaving from multiple symbols
    _print_queue.put("\n".join(lines) + "\n")

# ── WebSocket callbacks ───────────────────────────────────────────────────────
def on_message(message: dict) -> None:
    """Called on every WebSocket frame. Must return fast — only queues work."""
    msg_type = message.get("type", "")
    if msg_type == "dp":
        render_depth(message)
    elif msg_type == "error":
        qprint(f"\n  {RED}[WS ERROR]{RESET}  {message}\n")
    else:
        qprint(f"  {DIM}[{ts()}] {msg_type}: {message}{RESET}")

def on_error(message: dict) -> None:
    qprint(f"\n  {RED}[ERROR]{RESET}  {message}\n")

def on_close(message) -> None:
    qprint(f"\n  {YELLOW}[CLOSED]{RESET}  Connection closed — {message}\n")

def on_open() -> None:
    qprint(f"\n  {GREEN}[CONNECTED]{RESET}  Subscribing to {DATA_TYPE} ...")
    for sym in SYMBOLS:
        qprint(f"    • {sym}")
    qprint()
    fyers_ws.subscribe(symbols=SYMBOLS, data_type=DATA_TYPE)
    fyers_ws.keep_running()

# ── Build socket ──────────────────────────────────────────────────────────────
fyers_ws = data_ws.FyersDataSocket(
    access_token=FULL_TOKEN,
    log_path="",
    litemode=LITE_MODE,
    write_to_file=False,
    reconnect=True,
    on_connect=on_open,
    on_close=on_close,
    on_error=on_error,
    on_message=on_message,
)

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    qprint(f"\n  {BOLD}FYERS DEPTH FEED{RESET}  {DIM}(Ctrl-C to quit){RESET}\n")
    try:
        fyers_ws.connect()
    except KeyboardInterrupt:
        _print_queue.put(None)        # stop printer thread cleanly
        _printer_thread.join(timeout=2)
        print(f"\n  {YELLOW}Stopped.{RESET}\n")