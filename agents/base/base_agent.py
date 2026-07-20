"""Abstract base class for all 100 trading agents."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentMetrics,
    AgentStatus,
    AgentType,
    ExecutedTrade,
    MarketState,
    OrderProposal,
    Symbol,
)


class BaseAgent(ABC):
    """
    Every agent in the swarm inherits from this class.
    An agent is completely independent: it has its own capital,
    its own policy, and does NOT communicate with other agents.
    All coordination goes through the Orchestrator (MCP layer).
    """

    def __init__(
        self,
        symbol: Symbol,
        agent_type: AgentType,
        initial_capital: float = 1.0,
        target_multiplier: float = 10.0,
        agent_id: str | None = None,
    ):
        self.agent_id: str = agent_id or f"{agent_type.value}_{symbol.value}_{uuid.uuid4().hex[:8]}"
        self.symbol = symbol
        self.agent_type = agent_type
        self.equity: float = initial_capital
        self.initial_capital: float = initial_capital
        self.target_equity: float = initial_capital * target_multiplier
        self.status: AgentStatus = AgentStatus.ACTIVE

        self._total_trades: int = 0
        self._wins: int = 0
        self._max_equity: float = initial_capital
        self._trade_history: list[ExecutedTrade] = []

        logger.info(
            f"[{self.agent_id}] Initialized | capital=${initial_capital:.2f} | target=${self.target_equity:.2f}"
        )

    # ─── Abstract interface ───────────────────────────────────────────────────

    @abstractmethod
    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        """
        Core decision function. Receives market context and returns
        an OrderProposal (or None to stay flat). Must be implemented
        by every concrete agent subclass.
        """
        ...

    @abstractmethod
    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        """Called when broker confirms a trade is closed. Update internal state."""
        ...

    # ─── Common lifecycle methods ─────────────────────────────────────────────

    def calc_notional(self, price: float, risk_pct: float = 0.02) -> float:
        """Retorna USD-notional a arriesgar en este trade, acotado a [risk_min_entry_pct, risk_max_entry_pct]."""
        clamped_pct = max(settings.risk_min_entry_pct, min(settings.risk_max_entry_pct, risk_pct))
        return round(self.equity * clamped_pct, 4)

    def update_equity(self, new_equity: float) -> None:
        self.equity = new_equity
        if new_equity > self._max_equity:
            self._max_equity = new_equity
        if new_equity <= 0:
            self.status = AgentStatus.RETIRED
            logger.warning(f"[{self.agent_id}] RETIRED — capital exhausted")
        if new_equity >= self.target_equity:
            logger.success(f"[{self.agent_id}] TARGET HIT — ${new_equity:.2f} >= ${self.target_equity:.2f}")

    def record_trade(self, trade: ExecutedTrade) -> None:
        self._trade_history.append(trade)
        self._total_trades += 1
        if trade.pnl > 0:
            self._wins += 1
        self.update_equity(self.equity + trade.pnl - trade.commission)

    def get_metrics(self) -> AgentMetrics:
        win_rate = self._wins / self._total_trades if self._total_trades else 0.0
        max_dd = (self._max_equity - self.equity) / self._max_equity if self._max_equity else 0.0
        return AgentMetrics(
            agent_id=self.agent_id,
            equity=self.equity,
            initial_capital=self.initial_capital,
            total_trades=self._total_trades,
            win_rate=win_rate,
            sharpe=self._compute_sharpe(),
            max_drawdown=max_dd,
            current_status=self.status,
        )

    def _compute_sharpe(self) -> float:
        if len(self._trade_history) < 2:
            return 0.0
        pnls = [t.pnl for t in self._trade_history]
        import statistics

        avg = statistics.mean(pnls)
        std = statistics.stdev(pnls)
        return round(avg / std if std else 0.0, 3)

    @property
    def is_alive(self) -> bool:
        return self.status == AgentStatus.ACTIVE

    def __repr__(self) -> str:
        return f"<{self.agent_type.value} id={self.agent_id} eq=${self.equity:.2f} status={self.status.value}>"
