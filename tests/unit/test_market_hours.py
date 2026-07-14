"""
Unit tests for per-symbol market-hours checks (data/feeds/market_hours.py).
Uses pandas_market_calendars' real exchange calendars for equities/futures,
and a FX-week heuristic for XAUUSD.
"""
from datetime import datetime, timezone

from swarm_trading.core.models import Symbol
from swarm_trading.data.feeds.market_hours import is_market_open, market_status_by_symbol


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ─── XAUUSD — FX-week heuristic ────────────────────────────────────────────

def test_xauusd_closed_on_saturday():
    assert is_market_open(Symbol.XAUUSD, _utc(2026, 7, 18, 12, 0)) is False  # Saturday


def test_xauusd_open_on_weekday():
    assert is_market_open(Symbol.XAUUSD, _utc(2026, 7, 14, 20, 48)) is True  # Tuesday


def test_xauusd_open_sunday_after_2200_utc():
    assert is_market_open(Symbol.XAUUSD, _utc(2026, 7, 19, 23, 0)) is True


def test_xauusd_closed_sunday_before_2200_utc():
    assert is_market_open(Symbol.XAUUSD, _utc(2026, 7, 19, 10, 0)) is False


# ─── PLTR — real NYSE calendar ──────────────────────────────────────────────

def test_pltr_open_during_nyse_session():
    # Tuesday 15:00 UTC = 11:00 ET, well within the 9:30-16:00 ET session.
    assert is_market_open(Symbol.PLTR, _utc(2026, 7, 14, 15, 0)) is True


def test_pltr_closed_outside_nyse_session():
    # Tuesday 05:00 UTC = 1:00 ET, hours before the open.
    assert is_market_open(Symbol.PLTR, _utc(2026, 7, 14, 5, 0)) is False


def test_pltr_closed_on_weekend():
    assert is_market_open(Symbol.PLTR, _utc(2026, 7, 18, 15, 0)) is False  # Saturday


def test_pltr_closed_on_us_holiday_even_though_its_a_weekday():
    """Christmas 2026-12-25 is a Friday (a weekday) — a day-of-week
    heuristic would say 'open', but NYSE is actually closed for the holiday.
    This is exactly the gap the old single day-of-week heuristic had."""
    assert is_market_open(Symbol.PLTR, _utc(2026, 12, 25, 15, 0)) is False


# ─── NAS100 / US100 — CME_Equity calendar ──────────────────────────────────

def test_nas100_and_us100_share_the_same_cme_session():
    now = _utc(2026, 7, 14, 15, 0)
    assert is_market_open(Symbol.NAS100, now) == is_market_open(Symbol.US100, now)


def test_nas100_open_during_overnight_globex_session():
    # Sunday 23:00 UTC: Monday's CME Globex session already opened at
    # Sunday 22:00 UTC — the case that needed the +/-1 day query window.
    assert is_market_open(Symbol.NAS100, _utc(2026, 7, 19, 23, 0)) is True


def test_nas100_closed_during_saturday():
    assert is_market_open(Symbol.NAS100, _utc(2026, 7, 18, 12, 0)) is False


# ─── OIL — CME Globex CL calendar ──────────────────────────────────────────

def test_oil_open_during_globex_session():
    assert is_market_open(Symbol.OIL, _utc(2026, 7, 14, 15, 0)) is True


def test_oil_closed_during_saturday():
    assert is_market_open(Symbol.OIL, _utc(2026, 7, 18, 12, 0)) is False


# ─── market_status_by_symbol ────────────────────────────────────────────────

def test_market_status_by_symbol_covers_every_symbol():
    status = market_status_by_symbol(_utc(2026, 7, 14, 15, 0))
    assert set(status.keys()) == {s.value for s in Symbol}
    assert all(isinstance(v, bool) for v in status.values())
