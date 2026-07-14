"""
AsyncRepository — SQLAlchemy 2.0 async persistence layer for historical
trades and swarm snapshots.

The database is OPTIONAL: every write method here swallows its own
exceptions and logs a warning instead of raising, so a dead/unreachable DB
never takes the swarm down. Read methods return an empty list on failure
for the same reason.
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from swarm_trading.core.models import ExecutedTrade
from swarm_trading.data.historic.db_models import AgentORM, Base, SwarmSnapshotORM, TradeORM


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


def _trade_to_dict(row: TradeORM) -> dict:
    return {
        "trade_id":   row.trade_id,
        "agent_id":   row.agent_id,
        "symbol":     row.symbol,
        "side":       row.side,
        "entry_price": row.entry_price,
        "quantity":   row.quantity,
        "sl_price":   row.sl_price,
        "tp_price":   row.tp_price,
        "pnl":        row.pnl,
        "status":     row.status,
        "opened_at":  row.opened_at.isoformat() if row.opened_at else None,
        "closed_at":  row.closed_at.isoformat() if row.closed_at else None,
    }


def _snapshot_to_dict(row: SwarmSnapshotORM) -> dict:
    return {
        "id":             row.id,
        "timestamp":      row.timestamp.isoformat() if row.timestamp else None,
        "total_equity":   row.total_equity,
        "daily_pnl":      row.daily_pnl,
        "active_agents":  row.active_agents,
        "total_trades":   row.total_trades,
    }


class AsyncRepository:
    def __init__(self, database_url: str):
        self._url = normalize_async_url(database_url)
        engine_kwargs: dict = {}
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
        """Creates tables if they don't exist yet. Left to raise on failure —
        callers (main.py) decide whether a dead DB at startup should be
        treated as fatal or degrade to repository=None."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info(f"[Repository] Initialized ({self._engine.dialect.name})")

    async def save_trade(self, trade: ExecutedTrade) -> None:
        try:
            values = dict(
                trade_id=trade.trade_id,
                agent_id=trade.agent_id,
                symbol=trade.symbol.value,
                side=trade.side.value,
                entry_price=trade.entry_price,
                quantity=trade.quantity,
                sl_price=trade.sl_price,
                tp_price=trade.tp_price,
                pnl=trade.pnl,
                status=trade.status.value,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
            )
            insert = postgresql.insert if self._engine.dialect.name == "postgresql" else sqlite.insert

            async with self._session_factory() as session:
                # TradeORM.agent_id has a FK to agents.id with no seed path
                # elsewhere in this task — upsert a minimal placeholder row
                # first so a trade for an unseen agent never fails its FK
                # constraint. Never overwrites a real AgentORM row (DO NOTHING).
                agent_stub = insert(AgentORM).values(
                    id=trade.agent_id, agent_type="UNKNOWN",
                    symbol=trade.symbol.value, initial_capital=0.0,
                ).on_conflict_do_nothing(index_elements=["id"])
                await session.execute(agent_stub)

                update_cols = {k: v for k, v in values.items() if k != "trade_id"}
                stmt = insert(TradeORM).values(**values).on_conflict_do_update(
                    index_elements=["trade_id"], set_=update_cols,
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            logger.warning(f"[Repository] save_trade failed: {exc}")

    async def save_snapshot(self, summary: dict) -> None:
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

    async def get_agent_trades(self, agent_id: str, limit: int | None = None) -> list[dict]:
        """Most recent trades first (opened_at desc). `limit=None` returns all."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(TradeORM)
                    .where(TradeORM.agent_id == agent_id)
                    .order_by(TradeORM.opened_at.desc())
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                result = await session.execute(stmt)
                return [_trade_to_dict(row) for row in result.scalars().all()]
        except Exception as exc:
            logger.warning(f"[Repository] get_agent_trades failed: {exc}")
            return []

    async def get_recent_snapshots(self, limit: int = 60) -> list[dict]:
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(SwarmSnapshotORM).order_by(SwarmSnapshotORM.timestamp.desc()).limit(limit)
                )
                return [_snapshot_to_dict(row) for row in result.scalars().all()]
        except Exception as exc:
            logger.warning(f"[Repository] get_recent_snapshots failed: {exc}")
            return []

    async def close(self) -> None:
        await self._engine.dispose()
