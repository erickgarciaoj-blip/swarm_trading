"""
AsyncRepository — SQLAlchemy 2.0 async persistence layer for historical
trades and swarm snapshots.

The database is OPTIONAL: every write method here swallows its own
exceptions and logs a warning instead of raising, so a dead/unreachable DB
never takes the swarm down. Read methods return an empty list on failure
for the same reason.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from swarm_trading.core.models import ExecutedTrade
from swarm_trading.data.historic.db_models import AgentORM, RiskStateORM, SwarmSnapshotORM, TradeORM
from swarm_trading.risk.engine.risk_engine import RiskStateSnapshot

# Rows/snapshots serialize to plain JSON-able dicts (str/float/int/None
# values) for the dashboard API — not worth a dataclass per shape here.
JSONDict = dict[str, Any]

# risk_state is a single-row table — the swarm's halt state is global, not
# per-agent (see ADR-0010). This id is fixed rather than autoincrement.
_RISK_STATE_ID = 1


def normalize_async_url(url: str) -> str:
    """DATABASE_URL is often given driver-less (postgresql://, sqlite://) —
    e.g. that's exactly what Supabase hands out. Fill in the async driver
    (asyncpg / aiosqlite) SQLAlchemy's async engine needs, unless one is
    already specified."""
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def _trade_to_dict(row: TradeORM) -> JSONDict:
    return {
        "trade_id": row.trade_id,
        "agent_id": row.agent_id,
        "symbol": row.symbol,
        "side": row.side,
        "entry_price": row.entry_price,
        "quantity": row.quantity,
        "sl_price": row.sl_price,
        "tp_price": row.tp_price,
        "pnl": row.pnl,
        "status": row.status,
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
    }


def _snapshot_to_dict(row: SwarmSnapshotORM) -> JSONDict:
    return {
        "id": row.id,
        "timestamp": row.timestamp.isoformat() if row.timestamp else None,
        "total_equity": row.total_equity,
        "daily_pnl": row.daily_pnl,
        "active_agents": row.active_agents,
        "total_trades": row.total_trades,
    }


class AsyncRepository:
    def __init__(self, database_url: str):
        self._url = normalize_async_url(database_url)
        engine_kwargs: dict[str, Any] = {}
        if ":memory:" in self._url:
            # A SQLite :memory: DB only lives as long as one connection to it
            # stays open — the default pool opens/closes a connection per
            # checkout, which would silently reset it to empty on every call.
            # StaticPool pins a single connection for the engine's lifetime.
            # Only ever hit in tests; real postgres/file URLs are unaffected.
            engine_kwargs["poolclass"] = StaticPool
        self._engine: AsyncEngine = create_async_engine(self._url, **engine_kwargs)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        """Validates connectivity only — does NOT create or modify schema.
        Alembic (`alembic upgrade head`) is the sole schema authority; see
        docs/architecture/adr/0008-postgresql-alembic-schema-authority.md.
        Left to raise on failure — callers (main.py) decide whether a dead DB
        at startup should be treated as fatal or degrade to repository=None."""
        try:
            async with self._engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            # SQLAlchemy only wraps statement-execution failures into
            # SQLAlchemyError/DBAPIError — a pure connection-establishment
            # failure (e.g. asyncpg/asyncio hitting a closed port) propagates
            # the raw OSError/ConnectionRefusedError instead. Both are real
            # "can't reach the database" cases and belong in the same
            # clear-failure path.
            raise ConnectionError(
                f"[Repository] Could not connect to the database ({self._engine.dialect.name}): {exc}"
            ) from exc
        logger.info(f"[Repository] Connectivity OK ({self._engine.dialect.name})")

    async def is_ready(self, timeout: float = 3.0) -> bool:
        """Lightweight readiness probe for GET /health/ready — polled on every
        health-check tick, so unlike init() it never raises or logs at INFO,
        just returns a bool. Reuses self._engine's own connection pool (no
        unmanaged raw connection per call) and bounds the probe with a short
        timeout so a black-holed network doesn't hang the readiness check
        indefinitely. asyncio.timeout() distinguishes "this probe timed out"
        (raised as TimeoutError at the `async with` boundary) from genuine
        outer task cancellation (still raised as CancelledError) — only the
        former is treated as "not ready"; the latter always propagates."""
        try:
            async with asyncio.timeout(timeout):
                async with self._engine.begin() as conn:
                    await conn.execute(text("SELECT 1"))
            return True
        except asyncio.CancelledError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            # Logged internally at debug (not exposed to the HTTP caller —
            # see dashboard/api/routes.py::health_ready) so the specific
            # driver error/credentials context never leaks into a public
            # response, but the cause is still recorded for whoever reads
            # container logs.
            logger.debug(f"[Repository] Not ready ({self._engine.dialect.name}): {exc}")
            return False

    async def save_trade(self, trade: ExecutedTrade) -> None:
        try:
            values = {
                "trade_id": trade.trade_id,
                "agent_id": trade.agent_id,
                "symbol": trade.symbol.value,
                "side": trade.side.value,
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
                "sl_price": trade.sl_price,
                "tp_price": trade.tp_price,
                "pnl": trade.pnl,
                "status": trade.status.value,
                "opened_at": trade.opened_at,
                "closed_at": trade.closed_at,
            }
            # postgresql.insert/sqlite.insert return dialect-specific Insert
            # subclasses (the only ones with on_conflict_do_*) — mypy only
            # sees the generic sqlalchemy.Insert from this Union, not either
            # concrete subclass, so the two calls below need a narrow ignore.
            insert = postgresql.insert if self._engine.dialect.name == "postgresql" else sqlite.insert

            async with self._session_factory() as session:
                # TradeORM.agent_id has a FK to agents.id with no seed path
                # elsewhere in this task — upsert a minimal placeholder row
                # first so a trade for an unseen agent never fails its FK
                # constraint. Never overwrites a real AgentORM row (DO NOTHING).
                agent_stub = (
                    insert(AgentORM)
                    .values(
                        id=trade.agent_id,
                        agent_type="UNKNOWN",
                        symbol=trade.symbol.value,
                        initial_capital=0.0,
                    )
                    .on_conflict_do_nothing(index_elements=["id"])  # type: ignore[attr-defined]
                )
                await session.execute(agent_stub)

                update_cols = {k: v for k, v in values.items() if k != "trade_id"}
                stmt = (
                    insert(TradeORM)
                    .values(**values)
                    .on_conflict_do_update(  # type: ignore[attr-defined]
                        index_elements=["trade_id"],
                        set_=update_cols,
                    )
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Repository] save_trade failed: {exc}")

    async def save_snapshot(self, summary: JSONDict) -> None:
        try:
            snapshot = SwarmSnapshotORM(
                total_equity=summary.get("total_equity", 0.0),
                daily_pnl=summary.get("daily_pnl", 0.0),
                active_agents=summary.get("active_agents", 0),
                total_trades=summary.get("total_trades", 0),
            )
            async with self._session_factory() as session:
                session.add(snapshot)
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Repository] save_snapshot failed: {exc}")

    async def get_agent_trades(self, agent_id: str, limit: int | None = None) -> list[JSONDict]:
        """Most recent trades first (opened_at desc). `limit=None` returns all."""
        try:
            async with self._session_factory() as session:
                stmt = select(TradeORM).where(TradeORM.agent_id == agent_id).order_by(TradeORM.opened_at.desc())
                if limit is not None:
                    stmt = stmt.limit(limit)
                result = await session.execute(stmt)
                return [_trade_to_dict(row) for row in result.scalars().all()]
        except Exception as exc:
            logger.warning(f"[Repository] get_agent_trades failed: {exc}")
            return []

    async def get_recent_snapshots(self, limit: int | None = 60) -> list[JSONDict]:
        """Oldest-first (chronological) — ready to feed straight into a time-series
        chart. `limit=None` returns the full history since the swarm's first tick."""
        try:
            async with self._session_factory() as session:
                stmt = select(SwarmSnapshotORM).order_by(SwarmSnapshotORM.timestamp.desc())
                if limit is not None:
                    stmt = stmt.limit(limit)
                result = await session.execute(stmt)
                rows = list(result.scalars().all())
                rows.reverse()
                return [_snapshot_to_dict(row) for row in rows]
        except Exception as exc:
            logger.warning(f"[Repository] get_recent_snapshots failed: {exc}")
            return []

    async def get_agent_equity_curve(self, agent_id: str, initial_capital: float) -> list[JSONDict]:
        """Reconstructs an agent's equity over time as initial_capital + the running
        sum of its closed trades' pnl, ordered oldest-first — there's no per-agent
        equity snapshot table, so this derives the curve straight from `trades`."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(TradeORM)
                    .where(TradeORM.agent_id == agent_id, TradeORM.closed_at.isnot(None))
                    .order_by(TradeORM.closed_at.asc())
                )
                result = await session.execute(stmt)
                rows = result.scalars().all()
            equity = initial_capital
            curve: list[JSONDict] = []
            for row in rows:
                equity += row.pnl
                # closed_at is guaranteed non-null by the WHERE clause above —
                # mypy can't see that from the SQL filter alone.
                assert row.closed_at is not None
                curve.append({"timestamp": row.closed_at.isoformat(), "equity": round(equity, 4)})
            return curve
        except Exception as exc:
            logger.warning(f"[Repository] get_agent_equity_curve failed: {exc}")
            return []

    async def save_risk_state(self, snapshot: RiskStateSnapshot) -> None:
        try:
            values = {
                "id": _RISK_STATE_ID,
                "daily_reference_equity": snapshot.daily_reference_equity,
                "daily_reference_date": snapshot.daily_reference_date,
                "daily_halted": snapshot.daily_halted,
                "daily_halted_at": snapshot.daily_halted_at,
                "daily_halt_observed_value": snapshot.daily_halt_observed_value,
                "sticky_halted": snapshot.sticky_halted,
                "halt_cause": snapshot.halt_cause,
                "halted_at": snapshot.halted_at,
                "halt_observed_value": snapshot.halt_observed_value,
            }
            insert = postgresql.insert if self._engine.dialect.name == "postgresql" else sqlite.insert
            update_cols = {k: v for k, v in values.items() if k != "id"}
            stmt = (
                insert(RiskStateORM)
                .values(**values)
                .on_conflict_do_update(  # type: ignore[attr-defined]
                    index_elements=["id"],
                    set_=update_cols,
                )
            )
            async with self._session_factory() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Repository] save_risk_state failed: {exc}")

    async def load_risk_state(self) -> RiskStateSnapshot | None:
        """Unlike every other read method in this class, this one is
        intentionally NOT fail-soft: a daily/total-loss halt must never be
        silently dropped just because the DB hiccuped on read at startup — a
        process restart must not silently reactivate the swarm (see
        ADR-0010). Raises on failure; main.py already treats a dead DB at
        startup as fatal for repository.init() for the same reason (see
        ADR-0008), and this call only ever runs after init() has already
        succeeded."""
        async with self._session_factory() as session:
            result = await session.execute(select(RiskStateORM).where(RiskStateORM.id == _RISK_STATE_ID))
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return RiskStateSnapshot(
            daily_reference_equity=row.daily_reference_equity,
            daily_reference_date=row.daily_reference_date,
            daily_halted=row.daily_halted,
            daily_halted_at=row.daily_halted_at,
            daily_halt_observed_value=row.daily_halt_observed_value,
            sticky_halted=row.sticky_halted,
            halt_cause=row.halt_cause,
            halted_at=row.halted_at,
            halt_observed_value=row.halt_observed_value,
        )

    async def close(self) -> None:
        await self._engine.dispose()
