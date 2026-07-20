"""Unit tests for AsyncRepository (SQLAlchemy 2.0 async persistence layer)."""

import asyncio
from datetime import date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from swarm_trading.core.models import ExecutedTrade, OrderStatus, Side, Symbol
from swarm_trading.data.historic.db_models import Base
from swarm_trading.data.historic.repository import AsyncRepository
from swarm_trading.risk.engine.risk_engine import RiskStateSnapshot

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


async def _create_schema(repo: AsyncRepository) -> None:
    """Test-only schema setup, standing in for `alembic upgrade head` in a
    real deployment. AsyncRepository.init() deliberately no longer creates
    tables (see repository.py / ADR-0008) — these unit tests exercise CRUD
    logic against an already-migrated database, so they build the schema
    directly rather than through the repository."""
    async with repo._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
async def repo():
    r = AsyncRepository(MEMORY_DB_URL)
    await r.init()
    await _create_schema(r)
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
async def test_save_and_load_risk_state_round_trip(repo):
    snapshot = RiskStateSnapshot(
        daily_reference_equity=100_000.0,
        daily_reference_date=date(2026, 7, 20),
        daily_halted=True,
        daily_halted_at=datetime(2026, 7, 20, 12, 0, 0),
        daily_halt_observed_value=84_000.0,
        sticky_halted=False,
        halt_cause="daily_loss",
        halted_at=None,
        halt_observed_value=None,
    )

    await repo.save_risk_state(snapshot)
    loaded = await repo.load_risk_state()

    assert loaded == snapshot


@pytest.mark.asyncio
async def test_save_risk_state_upserts_the_single_row(repo):
    first = RiskStateSnapshot(
        daily_reference_equity=100_000.0,
        daily_reference_date=date(2026, 7, 20),
        daily_halted=False,
        daily_halted_at=None,
        daily_halt_observed_value=None,
        sticky_halted=False,
        halt_cause=None,
        halted_at=None,
        halt_observed_value=None,
    )
    await repo.save_risk_state(first)

    second = RiskStateSnapshot(
        daily_reference_equity=90_000.0,
        daily_reference_date=date(2026, 7, 21),
        daily_halted=False,
        daily_halted_at=None,
        daily_halt_observed_value=None,
        sticky_halted=True,
        halt_cause="total_loss",
        halted_at=datetime(2026, 7, 21, 9, 0, 0),
        halt_observed_value=-30_000.0,
    )
    await repo.save_risk_state(second)

    loaded = await repo.load_risk_state()
    assert loaded == second


@pytest.mark.asyncio
async def test_load_risk_state_returns_none_when_nothing_persisted_yet(repo):
    assert await repo.load_risk_state() is None


@pytest.mark.asyncio
async def test_save_risk_state_does_not_raise_on_db_error():
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")
    snapshot = RiskStateSnapshot(
        daily_reference_equity=None,
        daily_reference_date=None,
        daily_halted=False,
        daily_halted_at=None,
        daily_halt_observed_value=None,
        sticky_halted=False,
        halt_cause=None,
        halted_at=None,
        halt_observed_value=None,
    )
    await bad_repo.save_risk_state(snapshot)  # must not raise
    await bad_repo.close()


@pytest.mark.asyncio
async def test_load_risk_state_raises_on_db_error():
    """Unlike every other read method (which fails soft), load_risk_state
    must raise — a restart must never silently treat a DB read failure as
    'no halt persisted' (see ADR-0010 and repository.py's docstring)."""
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")
    with pytest.raises((SQLAlchemyError, OSError)):
        await bad_repo.load_risk_state()
    await bad_repo.close()


@pytest.mark.asyncio
async def test_save_trade_does_not_raise_on_db_error():
    # Points at a directory that can't exist — sqlite fails to open it
    # immediately, no network timeout involved.
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")
    await bad_repo.save_trade(_trade())  # must not raise
    await bad_repo.close()


@pytest.mark.asyncio
async def test_init_does_not_create_tables():
    """AsyncRepository.init() must only validate connectivity — schema
    creation is Alembic's exclusive responsibility (see ADR-0008). A DB with
    no migrations applied must stay empty after init(), not get silently
    self-corrected."""
    r = AsyncRepository(MEMORY_DB_URL)
    await r.init()

    async with r._engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    assert table_names == []
    await r.close()


@pytest.mark.asyncio
async def test_init_raises_clear_error_on_unreachable_db():
    """A genuinely unreachable DB must fail loudly and clearly at init() —
    callers (main.py) decide whether that's fatal or degrades to no
    persistence; either way it must not be silently swallowed."""
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")

    with pytest.raises(ConnectionError, match="Could not connect"):
        await bad_repo.init()

    await bad_repo.close()


@pytest.mark.asyncio
async def test_static_pool_only_applied_to_sqlite_memory_url():
    """StaticPool is a SQLite :memory:-only workaround for keeping the one
    connection that owns the in-memory DB alive (see repository.py) — it
    must never be silently applied to a real PostgreSQL URL, which relies on
    the driver's own normal connection pooling instead."""
    pg_repo = AsyncRepository("postgresql://user:pass@localhost:5432/swarm_trading")

    assert not isinstance(pg_repo._engine.pool, StaticPool)

    await pg_repo.close()


class _HangingConnection:
    async def execute(self, _stmt):
        await asyncio.sleep(1000)


class _HangingEngineCtx:
    async def __aenter__(self):
        return _HangingConnection()

    async def __aexit__(self, *_exc_info):
        return False


class _HangingEngine:
    """Stands in for a connection attempt that never resolves — the only way
    to exercise is_ready()'s timeout/cancellation paths without a real
    network-level hang."""

    dialect = type("_Dialect", (), {"name": "fake"})()

    def begin(self):
        return _HangingEngineCtx()

    async def dispose(self):
        pass


@pytest.mark.asyncio
async def test_is_ready_true_when_reachable(repo):
    assert await repo.is_ready() is True


@pytest.mark.asyncio
async def test_is_ready_false_when_unreachable():
    bad_repo = AsyncRepository("sqlite+aiosqlite:////this/path/does/not/exist/at/all.db")

    assert await bad_repo.is_ready() is False

    await bad_repo.close()


@pytest.mark.asyncio
async def test_is_ready_returns_false_on_timeout_not_raise():
    """A probe that exceeds its own timeout is "not ready", not an error —
    callers (GET /health/ready) poll this every tick and must never see an
    exception from a merely-slow database."""
    r = AsyncRepository(MEMORY_DB_URL)
    r._engine = _HangingEngine()  # type: ignore[assignment]

    assert await r.is_ready(timeout=0.05) is False


@pytest.mark.asyncio
async def test_is_ready_propagates_real_cancellation_not_swallowed_as_not_ready():
    """Task cancellation (e.g. the ASGI server cancelling an in-flight
    request) must never be caught and turned into `False` — only the probe's
    own internal timeout is treated as "not ready"."""
    r = AsyncRepository(MEMORY_DB_URL)
    r._engine = _HangingEngine()  # type: ignore[assignment]

    task = asyncio.ensure_future(r.is_ready(timeout=10))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
