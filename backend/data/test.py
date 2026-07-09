"""
Reusable FYERS WebSocket Client
"""

from fyers_apiv3.FyersWebsocket import data_ws
import os
import sys

# --------------------------------------------------
# Import Client ID
# --------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from creditials import client_id

# --------------------------------------------------
# Read Access Token
# --------------------------------------------------
with open("access_token.txt", "r") as f:
    access_token = f.read().strip()

# --------------------------------------------------
# WebSocket Token Format
# client_id:access_token
# --------------------------------------------------
ws_access_token = f"{client_id}:{access_token}"


# --------------------------------------------------
# Callbacks
# --------------------------------------------------

def on_message(message):
    print(message)


def on_error(message):
    print("Error:", message)


def on_close(message):
    print("Connection Closed:", message)


def on_open():
    symbols = [
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTYBANK-INDEX",
    ]

    fyers.subscribe(
        symbols=symbols,
        data_type="SymbolUpdate",
    )

    fyers.keep_running()


# --------------------------------------------------
# Create WebSocket
# --------------------------------------------------

fyers = data_ws.FyersDataSocket(
    access_token=ws_access_token,
    log_path="",
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=on_open,
    on_close=on_close,
    on_error=on_error,
    on_message=on_message,
)


# --------------------------------------------------
# Main
# --------------------------------------------------

if __name__ == "__main__":
    fyers.connect()