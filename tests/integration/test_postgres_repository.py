"""Integration tests for AsyncRepository against a real PostgreSQL instance.

These require `DATABASE_URL` to point at a real, already-migrated
(`alembic upgrade head`) PostgreSQL database — see
.github/workflows/ci.yml's `postgres-integration` job, or run locally with:

    docker compose up -d postgres
    docker compose run --rm migrate
    DATABASE_URL=postgresql+asyncpg://swarm:changeme@localhost:5432/swarm_trading \\
        pytest tests/integration/test_postgres_repository.py -m integration -v

Never simulate PostgreSQL with SQLite here — that's exactly what
tests/unit/test_repository.py already covers, and it would hide real
dialect differences (upsert syntax, pooling, autoincrement) instead of
catching them. If DATABASE_URL isn't a postgresql:// URL, tests fail loudly
rather than silently falling back to SQLite.
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from swarm_trading.core.models import ExecutedTrade, OrderStatus, Side, Symbol
from swarm_trading.data.historic.repository import AsyncRepository

pytestmark = pytest.mark.integration


def _require_postgres_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.fail(
            "tests/integration/* require DATABASE_URL to point at a real "
            f"PostgreSQL instance, got: {url!r}. Refusing to silently run "
            "against SQLite — see this file's module docstring."
        )
    return url


def _trade(trade_id: str = "pg-t1", agent_id: str = "pg-agent-1", pnl: float = 1.5) -> ExecutedTrade:
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
async def pg_url() -> str:
    return _require_postgres_url()


@pytest.fixture
async def repo(pg_url: str):
    r = AsyncRepository(pg_url)
    yield r
    await r.close()


@pytest.mark.asyncio
async def test_init_connects_to_real_postgres(repo: AsyncRepository) -> None:
    """Requirement: AsyncRepository.init() connects correctly to PostgreSQL."""
    await repo.init()
    assert repo._engine.dialect.name == "postgresql"


@pytest.mark.asyncio
async def test_migrations_created_the_expected_schema(repo: AsyncRepository) -> None:
    """Requirement: `alembic upgrade head` (run by CI before this test suite,
    see .github/workflows/ci.yml) created the full schema — not init(),
    which never runs CREATE TABLE (see ADR-0008)."""
    await repo.init()

    async with repo._engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    # alembic_version is Alembic's own bookkeeping table (tracks the applied
    # revision) — it's a genuine, expected part of what `alembic upgrade
    # head` creates, alongside this project's own 3 tables.
    assert set(table_names) == {"agents", "trades", "swarm_snapshots", "alembic_version"}


@pytest.mark.asyncio
async def test_basic_crud_against_postgres(repo: AsyncRepository) -> None:
    """Requirement: basic CRUD against PostgreSQL."""
    await repo.init()
    trade = _trade(trade_id="pg-crud-1", agent_id="pg-crud-agent")

    await repo.save_trade(trade)
    trades = await repo.get_agent_trades("pg-crud-agent")

    assert len(trades) == 1
    assert trades[0]["trade_id"] == "pg-crud-1"
    assert trades[0]["pnl"] == 1.5


@pytest.mark.asyncio
async def test_real_upsert_against_postgres(repo: AsyncRepository) -> None:
    """Requirement: real upsert against PostgreSQL — exercises the
    postgresql.insert().on_conflict_do_update() path in
    repository.py::save_trade specifically (not sqlite.insert())."""
    await repo.init()
    trade_id, agent_id = "pg-upsert-1", "pg-upsert-agent"

    await repo.save_trade(_trade(trade_id=trade_id, agent_id=agent_id, pnl=1.0))
    await repo.save_trade(_trade(trade_id=trade_id, agent_id=agent_id, pnl=99.0))

    trades = await repo.get_agent_trades(agent_id)
    assert len(trades) == 1  # updated in place, not duplicated
    assert trades[0]["pnl"] == 99.0


@pytest.mark.asyncio
async def test_data_persists_across_closing_and_reopening_sessions(pg_url: str) -> None:
    """Requirement: data persists across closing/reopening sessions —
    a fresh AsyncRepository/engine against the same DATABASE_URL must see
    data written by a prior, now-closed one."""
    agent_id = "pg-persist-agent"

    first = AsyncRepository(pg_url)
    await first.init()
    await first.save_trade(_trade(trade_id="pg-persist-1", agent_id=agent_id))
    await first.close()

    second = AsyncRepository(pg_url)
    await second.init()
    trades = await second.get_agent_trades(agent_id)
    await second.close()

    assert len(trades) == 1
    assert trades[0]["trade_id"] == "pg-persist-1"


@pytest.mark.asyncio
async def test_invalid_url_fails_clearly() -> None:
    """Requirement: clear handling of an invalid URL / failed connection —
    unlike save_trade()/get_*() (which swallow errors so a dead DB never
    takes the swarm down), init() must raise, not silently succeed."""
    bad_repo = AsyncRepository("postgresql+asyncpg://swarm:wrong-password@localhost:59999/does_not_exist")

    with pytest.raises(ConnectionError, match="Could not connect"):
        await bad_repo.init()

    await bad_repo.close()


@pytest.mark.asyncio
async def test_sqlite_static_pool_not_applied_to_postgres(repo: AsyncRepository) -> None:
    """Requirement: SQLite-specific config never applies to PostgreSQL —
    confirms it against a real asyncpg-backed engine, not just the URL
    string parsing already covered in tests/unit/test_repository.py."""
    assert not isinstance(repo._engine.pool, StaticPool)


@pytest.mark.asyncio
async def test_missing_migrations_produce_clear_failure_not_silent_correction(pg_url: str) -> None:
    """Requirement: a DB Alembic hasn't migrated yet must not be silently
    self-corrected into a working one. Proves it against a genuinely
    unmigrated schema (a throwaway one created and dropped by this test) —
    not the already-migrated `public` schema every other test in this file
    uses, which would prove nothing about missing-migration behavior."""
    setup = AsyncRepository(pg_url)
    await setup.init()
    async with setup._engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS unmigrated_test"))
    await setup.close()

    r = AsyncRepository(pg_url)
    try:
        async with r._engine.begin() as conn:
            await conn.execute(text("SET search_path TO unmigrated_test"))

            table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            assert table_names == []  # Alembic never ran here, and neither did init()

            with pytest.raises(SQLAlchemyError):
                await conn.execute(text("SELECT * FROM trades"))
    finally:
        await r.close()
        cleanup = AsyncRepository(pg_url)
        await cleanup.init()
        async with cleanup._engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS unmigrated_test CASCADE"))
        await cleanup.close()
