"""Unit tests for AsyncRepository (SQLAlchemy 2.0 async persistence layer)."""

from datetime import datetime

import pytest

from swarm_trading.core.models import ExecutedTrade, OrderStatus, Side, Symbol
from swarm_trading.data.historic.repository import AsyncRepository

# NOTE: "sqlite+aiosqlite:///file::memory:?cache=shared" (the URL initially
# requested) isn't parsed as SQLite's URI-mode memory syntax by SQLAlchemy —
# it's treated as a literal relative file path named "file::memory:", which
# actually writes to disk and persists across test runs. Plain ":memory:"
# combined with StaticPool in AsyncRepository (see repository.py) is the
# correct, well-known way to get an isolated in-memory DB per engine.
MEMORY_DB_URL = "sqlite+aiosqlite:///:memory:"


def _trade(trade_id="t1", agent_id="agent_1", pnl=1.5) -> ExecutedTrade:
    return ExecutedTrade(
        trade_id=trade_id,
        agent_id=agent_id,
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=1900.0,
        quantity=0.01,
        sl_price=1850.0,
        tp_price=1950.0,
        status=OrderStatus.FILLED,
        pnl=pnl,
        closed_at=datetime.utcnow(),
    )


@pytest.fixture
async def repo():
    r = AsyncRepository(MEMORY_DB_URL)
    await r.init()
    yield r
    await r.close()


@pytest.mark.asyncio
async def test_save_and_retrieve_trade(repo):
    await repo.save_trade(_trade())

    trades = await repo.get_agent_trades("agent_1")

    assert len(trades) == 1
    assert trades[0]["trade_id"] == "t1"
    assert trades[0]["symbol"] == "XAUUSD"
    assert trades[0]["side"] == "LONG"
    assert trades[0]["pnl"] == 1.5


@pytest.mark.asyncio
async def test_save_snapshot(repo):
    summary = {"total_equity": 105.5, "daily_pnl": 5.5, "active_agents": 42, "total_trades": 7}
    await repo.save_snapshot(summary)

    snapshots = await repo.get_recent_snapshots(limit=10)

    assert len(snapshots) == 1
    assert snapshots[0]["total_equity"] == 105.5
    assert snapshots[0]["daily_pnl"] == 5.5
    assert snapshots[0]["active_agents"] == 42
    assert snapshots[0]["total_trades"] == 7


@pytest.mark.asyncio
async def test_save_trade_does_not_raise_on_db_error():
    # Points at a directory that can't exist — sqlite fails to open it
    # immediately, no network timeout involved.
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")
    await bad_repo.save_trade(_trade())  # must not raise
    await bad_repo.close()
