"""
SQLAlchemy 2.0 declarative ORM models for historical persistence.
Mirrors the shapes of core.models.ExecutedTrade / SwarmOrchestrator.get_swarm_summary(),
but is intentionally a separate, DB-specific layer — core.models stays free of
any ORM/SQLAlchemy dependency.
"""
from __future__ import annotations
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AgentORM(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_type: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    initial_capital: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TradeORM(Base):
    __tablename__ = "trades"

    trade_id: Mapped[str] = mapped_column(String, primary_key=True)
    # No cascade delete: a trade record must survive even if the owning
    # agent row is ever removed — historical trades are the source of truth.
    agent_id: Mapped[str] = mapped_column(String, ForeignKey("agents.id"))
    symbol: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    sl_price: Mapped[float] = mapped_column(Float)
    tp_price: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String)
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SwarmSnapshotORM(Base):
    __tablename__ = "swarm_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    total_equity: Mapped[float] = mapped_column(Float)
    daily_pnl: Mapped[float] = mapped_column(Float)
    active_agents: Mapped[int] = mapped_column(Integer)
    total_trades: Mapped[int] = mapped_column(Integer)
