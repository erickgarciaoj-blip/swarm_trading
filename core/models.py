"""Core domain models shared across all layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Symbol(StrEnum):
    XAUUSD = "XAUUSD"  # Gold
    PLTR = "PLTR"  # Palantir
    NAS100 = "NAS100"  # NASDAQ 100
    US100 = "US100"  # US100 Index
    OIL = "OIL"  # Crude Oil (WTI)


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class AgentType(StrEnum):
    SCALPER = "SCALPER"
    SWING = "SWING"
    NEWS_REACTIVE = "NEWS_REACTIVE"
    HEDGER = "HEDGER"
    RL = "RL"


class AgentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"  # burned its capital
    TRAINING = "TRAINING"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class NewsImpact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


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
    candles: list[Candle]  # last N candles
    indicators: dict[str, float]  # RSI, ATR, ADX, EMA, etc.
    spread: float = 0.0
    is_news_blackout: bool = False
    upcoming_news: list[NewsEvent] = field(default_factory=list)


@dataclass
class NewsEvent:
    timestamp: datetime
    title: str
    impact: NewsImpact
    currency: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None


@dataclass
class OrderProposal:
    """An agent's intention — validated by RiskEngine before execution."""

    agent_id: str
    symbol: Symbol
    side: Side
    quantity: float  # USD-notional to risk on this trade
    sl_price: float
    tp_price: float
    confidence: float  # 0.0 – 1.0
    price: float = 1.0  # reference price used to size quantity into units
    reason: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutedTrade:
    """A broker-confirmed order and, once closed, its costed result.

    PnL SEMANTICS — read before touching any consumer of these fields.

    `pnl` is the NET result: gross minus every transaction cost. It is the
    one number the rest of the system spends, displays and aggregates
    (BaseAgent.record_trade, RiskEngine.on_trade_closed, the dashboard, the
    equity curve). Nothing may subtract costs from it again.

    `commission` is a BREAKDOWN field, already contained in
    entry_costs/exit_costs and therefore already reflected in `pnl`. It used
    to be subtracted separately at two call sites; with `pnl` now net, doing
    so double-charged the trade. Kept for auditing what portion of the drag
    was fees rather than spread/slippage.

    Invariant, exact by construction (see core/costs.compute_costed_pnl).
    The grouping is canonical — costs summed first, subtracted once:

        total_costs == entry_costs + exit_costs
        pnl         == gross_pnl - total_costs

    `entry_price`/`exit_price` are the REFERENCE prices the strategy acted
    on (candle close, TP/SL level). `entry_fill_price`/`exit_fill_price` are
    what was actually transacted after crossing the spread and slipping.
    Both are kept so a closed trade can be re-derived and audited later.
    """

    trade_id: str
    agent_id: str
    symbol: Symbol
    side: Side
    entry_price: float
    quantity: float
    sl_price: float
    tp_price: float
    status: OrderStatus
    # NET of all costs — see the class docstring.
    pnl: float = 0.0
    # Before costs. Equals `pnl` when the instrument is configured with zero
    # costs, which is what makes a costed run comparable with pre-cost history.
    gross_pnl: float = 0.0
    entry_costs: float = 0.0
    exit_costs: float = 0.0
    # Commission component of entry_costs + exit_costs. Informational only:
    # ALREADY included in `pnl`. Never subtract it again.
    commission: float = 0.0
    # Effective transacted prices. Default 0.0 means "not costed" (live
    # adapters don't populate these yet); the reference prices above always
    # carry the strategy's own view regardless.
    entry_fill_price: float = 0.0
    exit_price: float | None = None
    exit_fill_price: float | None = None
    opened_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: datetime | None = None

    @property
    def total_costs(self) -> float:
        return self.entry_costs + self.exit_costs


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
