"""
symbol_selector.py
--------------------
Shared symbol-picking menu used by producer.py, consumer.py, and
candle_builder.py when run standalone, and by launcher.py when starting
all three together.

    1. Indices
    2. Equity
    3. Others (type your own manually)
"""

INDICES = [
    "NSE:NIFTY50-INDEX",
    "NSE:NIFTYBANK-INDEX",
    "NSE:FINNIFTY-INDEX",
    "NSE:INDIAVIX-INDEX",
]

EQUITY = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:SBIN-EQ",
    "NSE:ADANIENT-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:INFY-EQ",
]


def choose_symbols():
    print("\nWhat kind of symbol(s) do you want?")
    print("  1. Indices")
    print("  2. Equity")
    print("  3. Others (enter manually)")

    choice = input("Enter 1, 2, or 3: ").strip()

    if choice == "1":
        options = INDICES
    elif choice == "2":
        options = EQUITY
    else:
        raw = input(
            "\nEnter symbol(s) manually, comma-separated "
            "(e.g. NSE:TCS-EQ,NSE:NIFTY26JUNFUT): "
        ).strip()
        chosen = [s.strip() for s in raw.split(",") if s.strip()]
        return chosen or ["NSE:NIFTY50-INDEX"]

    label = "Indices" if choice == "1" else "Equity"
    print(f"\n{label}:")
    for i, sym in enumerate(options, start=1):
        print(f"  {i}. {sym}")

    raw = input("Select number(s), comma-separated (e.g. 1,3): ").strip()
    chosen = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(options):
            chosen.append(options[int(part) - 1])

    return chosen or ["NSE:NIFTY50-INDEX"]