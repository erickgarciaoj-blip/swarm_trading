"""
RiskEngine — the single gate every OrderProposal must pass through.
No order reaches the broker without this validation.
FTMO-style rules + swarm-level correlation limits.
"""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import datetime
from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentMetrics, ExecutedTrade, OrderProposal, Symbol,
)

RECENT_TRADES_MAXLEN = 10


class RiskEngine:
    def __init__(self):
        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._open_positions_by_symbol: dict[Symbol, int] = defaultdict(int)
        self._last_reset: datetime = datetime.utcnow()
        self._is_halted: bool = False
        # Newest first — the single global choke point every closed trade
        # passes through, so it's the natural place for a swarm-wide feed.
        self._recent_trades: deque[ExecutedTrade] = deque(maxlen=RECENT_TRADES_MAXLEN)

    # ─── Main validation gate ─────────────────────────────────────────────────

    def validate(
        self,
        proposal: OrderProposal,
        agent_metrics: AgentMetrics,
        is_news_blackout: bool = False,
    ) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        Every check must pass for the order to reach the broker.
        """
        if self._is_halted:
            return False, "SWARM_HALTED: global risk limit breached"

        if is_news_blackout:
            return False, "NEWS_BLACKOUT: high-impact event window"

        if agent_metrics.current_status.value != "ACTIVE":
            return False, f"AGENT_INACTIVE: status={agent_metrics.current_status.value}"

        # Per-agent drawdown
        dd = (agent_metrics.initial_capital - agent_metrics.equity) / agent_metrics.initial_capital
        if dd >= settings.risk_max_total_loss_pct:
            return False, f"AGENT_MAX_DD: drawdown={dd:.1%}"

        # NOTE: no daily-loss halt by design — only the total swarm loss limit
        # below stops the swarm, so agents get more room to run. `daily_pnl`
        # is still tracked (see on_trade_closed) purely for dashboard display.

        # Total swarm PnL
        if self._total_pnl <= -(settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct):
            self._is_halted = True
            return False, f"TOTAL_LOSS_LIMIT: pnl={self._total_pnl:.2f}"

        # Concentration per symbol
        active_on_symbol = self._open_positions_by_symbol[proposal.symbol]
        if active_on_symbol >= settings.risk_max_agents_per_symbol:
            return False, f"SYMBOL_CONCENTRATION: {proposal.symbol.value} has {active_on_symbol} agents"

        return True, "OK"

    # ─── State updates ────────────────────────────────────────────────────────

    def on_order_opened(self, proposal: OrderProposal) -> None:
        self._open_positions_by_symbol[proposal.symbol] += 1
        logger.debug(f"[RiskEngine] +1 open on {proposal.symbol.value} by {proposal.agent_id}")

    def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self._daily_pnl  += trade.pnl
        self._total_pnl  += trade.pnl
        symbol = trade.symbol
        if self._open_positions_by_symbol[symbol] > 0:
            self._open_positions_by_symbol[symbol] -= 1
        self._recent_trades.appendleft(trade)

    def reset_daily(self) -> None:
        self._daily_pnl = 0.0
        self._last_reset = datetime.utcnow()
        if self._is_halted:
            self._is_halted = False
            logger.info("[RiskEngine] Daily reset — swarm UNHALTED")

    def halt(self, reason: str = "") -> None:
        self._is_halted = True
        logger.critical(f"[RiskEngine] MANUAL HALT | reason={reason}")

    def resume(self) -> None:
        self._is_halted = False
        logger.warning("[RiskEngine] RESUMED by operator")

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def total_pnl(self) -> float:
        return self._total_pnl

    @property
    def recent_trades(self) -> list[ExecutedTrade]:
        """Newest-first, capped at RECENT_TRADES_MAXLEN."""
        return list(self._recent_trades)
