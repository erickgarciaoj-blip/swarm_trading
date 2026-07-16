"""
Unit tests for NewsFeed, including a regression test for the blocking-I/O
fix in docs/architecture/adr/0002-async-io-blocking-calls-must-use-executor.md
— _fetch_forexfactory() used to call the synchronous feedparser.parse()
directly inside a coroutine. Before this file, NewsFeed had zero test
coverage at all.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from swarm_trading.core.models import NewsImpact, Symbol
from swarm_trading.data.news.news_feed import NewsFeed


@pytest.mark.asyncio
async def test_demo_backend_loads_high_impact_events():
    feed = NewsFeed(backend="demo")
    events = await feed.get_upcoming(Symbol.XAUUSD, horizon_hours=24)
    assert len(events) > 0
    assert any(e.impact == NewsImpact.HIGH for e in events)


@pytest.mark.asyncio
async def test_get_upcoming_filters_by_currency():
    feed = NewsFeed(backend="demo")
    # PLTR only maps to USD (see SYMBOL_CURRENCY_MAP) — every event returned
    # must be USD, never e.g. XAU-only news.
    events = await feed.get_upcoming(Symbol.PLTR, horizon_hours=24)
    assert all(e.currency == "USD" for e in events)


@pytest.mark.asyncio
async def test_get_upcoming_respects_horizon():
    feed = NewsFeed(backend="demo")
    # Demo events are seeded 1h+ into the future — a 0-hour horizon must
    # exclude all of them.
    events = await feed.get_upcoming(Symbol.XAUUSD, horizon_hours=0)
    assert events == []


@pytest.mark.asyncio
async def test_is_blackout_true_within_window_of_high_impact_event():
    feed = NewsFeed(backend="demo")
    await feed.get_upcoming(Symbol.XAUUSD)  # populate _events
    # First demo event (FOMC, HIGH impact) is seeded at now + 1h — well
    # outside a 5-minute blackout window, so blackout must be False here...
    assert await feed.is_blackout(Symbol.XAUUSD, blackout_min=5) is False
    # ...but True with a huge window that necessarily covers it.
    assert await feed.is_blackout(Symbol.XAUUSD, blackout_min=120) is True


@pytest.mark.asyncio
async def test_fetch_forexfactory_does_not_block_the_event_loop(monkeypatch):
    def slow_parse(url):
        time.sleep(0.2)  # stands in for a slow/stalled network call
        return SimpleNamespace(entries=[])

    monkeypatch.setattr("feedparser.parse", slow_parse)
    feed = NewsFeed(backend="forexfactory")

    ticks: list[float] = []

    async def ticker() -> None:
        for _ in range(4):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    # Same reasoning as test_market_feed.py's equivalent test: a coroutine
    # awaited directly runs synchronously through its first step until a real
    # suspend point, so what must be measured is the delay before ticker's
    # very first tick, not the spacing between its own ticks.
    start = time.monotonic()
    await asyncio.gather(feed.get_upcoming(Symbol.XAUUSD), ticker())

    startup_delay = ticks[0] - start
    assert startup_delay < 0.1, (
        f"ticker's first tick was delayed {startup_delay:.3f}s — the event loop was blocked by the fetch"
    )


@pytest.mark.asyncio
async def test_fetch_forexfactory_falls_back_to_demo_on_error(monkeypatch):
    def broken_parse(url):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr("feedparser.parse", broken_parse)
    feed = NewsFeed(backend="forexfactory")

    events = await feed.get_upcoming(Symbol.XAUUSD, horizon_hours=24)
    # Falls back to the demo event set rather than propagating the error.
    assert len(events) > 0
