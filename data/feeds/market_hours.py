"""
Per-symbol market-hours check.

Equities/futures use pandas_market_calendars' real exchange calendars
(exact holidays, half-days) — far more accurate than a day-of-week/hour
heuristic. XAUUSD is modeled as spot/CFD gold, which doesn't trade on a
single exchange calendar, so it keeps the standard global FX week
(Sun 22:00 UTC -> Fri 22:00 UTC) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache

import pandas_market_calendars as mcal

from swarm_trading.core.models import Symbol

# Exchange calendar per symbol, matching the contracts in
# brokers/ibkr/ibkr_broker.py's get_contract() (PLTR->NYSE stock,
# NAS100/US100->NQ on CME, OIL->CL on NYMEX/CME Globex).
# None means "not exchange-listed" — handled by the FX heuristic below.
SYMBOL_CALENDAR: dict[Symbol, str | None] = {
    Symbol.XAUUSD: None,
    Symbol.PLTR: "NYSE",
    Symbol.NAS100: "CME_Equity",
    Symbol.US100: "CME_Equity",
    Symbol.OIL: "CMEGlobex_CL",
}


@cache
def _get_calendar(name: str):
    return mcal.get_calendar(name)


def _is_fx_open(now_utc: datetime) -> bool:
    """Standard global FX week: Sun 22:00 UTC -> Fri 22:00 UTC."""
    weekday, hour = now_utc.weekday(), now_utc.hour  # Mon=0 ... Sun=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    return not (weekday == 6 and hour < 22)  # Sunday before 22:00 UTC


def is_market_open(symbol: Symbol, now: datetime | None = None) -> bool:
    """True if `symbol`'s market is open at `now` (defaults to current UTC time)."""
    now_utc = (now or datetime.utcnow()).replace(tzinfo=UTC)
    calendar_name = SYMBOL_CALENDAR.get(symbol)

    if calendar_name is None:
        return _is_fx_open(now_utc)

    calendar = _get_calendar(calendar_name)
    # A +/-1 day window, not just now_utc.date(): overnight-session exchanges
    # (CME Globex) index a session by its regular-hours end date, so e.g.
    # Sunday 23:00 UTC — already trading, as Monday's session opened at
    # Sunday 22:00 UTC — would otherwise look empty/closed for "Sunday".
    window_start = (now_utc - timedelta(days=1)).date()
    window_end = (now_utc + timedelta(days=1)).date()
    schedule = calendar.schedule(start_date=window_start, end_date=window_end)
    if schedule.empty:
        return False

    return any(row["market_open"] <= now_utc <= row["market_close"] for _, row in schedule.iterrows())


def market_status_by_symbol(now: datetime | None = None) -> dict[str, bool]:
    """{"XAUUSD": True, "PLTR": False, ...} for every tracked symbol."""
    return {symbol.value: is_market_open(symbol, now) for symbol in Symbol}
