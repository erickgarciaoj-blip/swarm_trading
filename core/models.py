"""Core domain models shared across all layers."""
from __future__ import annotations
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class Symbol(str, Enum):
    XAUUSD = "XAUUSD"   # Gold
    PLTR   = "PLTR"     # Palantir
    NAS100 = "NAS100"   # NASDAQ 100
    US100  = "US100"    # US100 Index
    OIL    = "OIL"      # Crude Oil (WTI)


class Side(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    FLAT  = "FLAT"


class AgentType(str, Enum):
    SCALPER       = "SCALPER"
    SWING         = "SWING"
    NEWS_REACTIVE = "NEWS_REACTIVE"
    HEDGER        = "HEDGER"
    RL            = "RL"


class AgentStatus(str, Enum):
    ACTIVE  = "ACTIVE"
    PAUSED  = "PAUSED"
    RETIRED = "RETIRED"   # burned its capital
    TRAINING = "TRAINING"


class OrderStatus(str, Enum):
    PENDING   = "PENDING"
    FILLED    = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"


class NewsImpact(str, Enum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


@dataclass
class Candle:
    symbol: Symbol
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1m"


@dataclass
class MarketState:
    """Snapshot of market context delivered to every agent each tick."""
    symbol: Symbol
    timestamp: datetime
    candles: list[Candle]       # last N candles
    indicators: dict            # RSI, ATR, ADX, EMA, etc.
    spread: float = 0.0
    is_news_blackout: bool = False
    upcoming_news: list["NewsEvent"] = field(default_factory=list)


@dataclass
class NewsEvent:
    timestamp: datetime
    title: str
    impact: NewsImpact
    currency: str
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None


@dataclass
class OrderProposal:
    """An agent's intention — validated by RiskEngine before execution."""
    agent_id: str
    symbol: Symbol
    side: Side
    quantity: float              # USD-notional to risk on this trade
    sl_price: float
    tp_price: float
    confidence: float           # 0.0 – 1.0
    price: float = 1.0          # reference price used to size quantity into units
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutedTrade:
    trade_id: str
    agent_id: str
    symbol: Symbol
    side: Side
    entry_price: float
    quantity: float
    sl_price: float
    tp_price: float
    status: OrderStatus
    pnl: float = 0.0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None


@dataclass
class AgentMetrics:
    agent_id: str
    equity: float
    initial_capital: float
    total_trades: int
    win_rate: float
    sharpe: float
    max_drawdown: float
    current_status: AgentStatus
    last_updated: datetime = field(default_factory=datetime.utcnow)
