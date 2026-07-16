"""
Regression test for the blocking-I/O bug fixed in
docs/architecture/adr/0002-async-io-blocking-calls-must-use-executor.md:
MarketFeed._fetch_yfinance() used to call the synchronous yf.download()
directly inside a coroutine, freezing the whole event loop (all 100 agents,
the dashboard WebSocket, everything) for as long as the network call took.
"""

import asyncio
import time

import pandas as pd
import pytest

from swarm_trading.core.models import Symbol
from swarm_trading.data.feeds.market_feed import MarketFeed


def _fake_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [1.0, 1.0], "High": [1.0, 1.0], "Low": [1.0, 1.0], "Close": [1.0, 1.0], "Volume": [1.0, 1.0]},
        index=pd.date_range("2024-01-01", periods=2, freq="1min"),
    )


@pytest.mark.asyncio
async def test_fetch_yfinance_does_not_block_the_event_loop(monkeypatch):
    def slow_download(*args, **kwargs):
        time.sleep(0.2)  # stands in for a slow/stalled network call
        return _fake_ohlcv()

    monkeypatch.setattr("yfinance.download", slow_download)
    feed = MarketFeed(backend="yfinance")

    ticks: list[float] = []

    async def ticker() -> None:
        for _ in range(4):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    # A coroutine awaited directly (not wrapped in create_task) runs
    # synchronously through its first step until it hits a real suspend
    # point — so if _fetch_yfinance's blocking call comes before any await,
    # the ticker task never even gets to run its *first* iteration until the
    # blocking call returns. Measuring only the gaps between ticks (as an
    # earlier version of this test did) misses that: once the ticker finally
    # starts, its own iterations are evenly spaced regardless, they just all
    # start ~0.2s late. What must be asserted is the delay before tick[0].
    start = time.monotonic()
    await asyncio.gather(feed.get_state(Symbol.XAUUSD), ticker())

    startup_delay = ticks[0] - start
    assert startup_delay < 0.1, (
        f"ticker's first tick was delayed {startup_delay:.3f}s — the event loop was blocked by the fetch"
    )


@pytest.mark.asyncio
async def test_fetch_yfinance_still_returns_correct_state(monkeypatch):
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: _fake_ohlcv())

    feed = MarketFeed(backend="yfinance")
    state = await feed.get_state(Symbol.XAUUSD)

    assert state.symbol == Symbol.XAUUSD
    assert len(state.candles) == 2
    assert state.candles[-1].close == 1.0
