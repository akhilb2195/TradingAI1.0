"""
economic_calendar.py
Reads the hardcoded india_calendar.json and tells you if today (or a given date)
is a high-impact event day. No API, no scraping, no rate limits.

Usage:
    from economic_calendar import is_high_impact_day, get_todays_events

    if is_high_impact_day():
        market_state["high_volatility_today"] = True
"""

import json
import os
from datetime import date, datetime

CALENDAR_FILE = os.path.join(os.path.dirname(__file__), "india_calendar.json")


def _load_calendar():
    with open(CALENDAR_FILE, "r") as f:
        return json.load(f)


def get_todays_events(check_date: str = None):
    """
    Returns a list of event descriptions for the given date (YYYY-MM-DD).
    Defaults to today.
    """
    if check_date is None:
        check_date = date.today().isoformat()

    cal = _load_calendar()
    events = []

    for m in cal.get("rbi_mpc_meetings", []):
        if m["decision_date"] == check_date:
            events.append(f"RBI MPC Decision - {m['note']}")
        if m["meeting_start"] == check_date:
            events.append(f"RBI MPC Meeting Starts - {m['note']}")

    for b in cal.get("union_budget", []):
        if b["date"] == check_date:
            events.append(f"Union Budget - {b['note']}")

    return events


def is_high_impact_day(check_date: str = None) -> bool:
    """
    Returns True if today has any hardcoded high-impact event.
    Plug this into market_state to flag volatility.
    """
    return len(get_todays_events(check_date)) > 0


if __name__ == "__main__":
    # quick manual test
    today = date.today().isoformat()
    events = get_todays_events(today)
    if events:
        print(f"{today}: HIGH IMPACT DAY")
        for e in events:
            print(f"  - {e}")
    else:
        print(f"{today}: No hardcoded high-impact events")