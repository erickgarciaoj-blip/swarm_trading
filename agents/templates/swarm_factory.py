"""
SwarmFactory — builds all 100 agents and registers them in the Orchestrator.
Distribution:
  - 30 Scalpers    (6 per symbol × 5 symbols)
  - 30 Swing       (6 per symbol × 5 symbols)
  - 20 NewsReactive (4 per symbol × 5 symbols)
  - 10 Hedgers     (2 per symbol × 5 symbols)
  - 10 RL          (2 per symbol × 5 symbols, agents/rl)
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from swarm_trading.agents.hedger.hedger_agent import HedgerAgent
from swarm_trading.agents.news_reactive.news_agent import NewsReactiveAgent
from swarm_trading.agents.rl.rl_agent import RLAgent
from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
from swarm_trading.agents.swing.swing_agent import SwingAgent
from swarm_trading.core.config import settings
from swarm_trading.core.models import ExecutedTrade, OrderStatus, Side, Symbol
from swarm_trading.core.orchestrator.orchestrator import SwarmOrchestrator

if TYPE_CHECKING:
    from swarm_trading.agents.base.base_agent import BaseAgent
    from swarm_trading.data.historic.repository import AsyncRepository


SCALPERS_PER_SYMBOL = 6
SWING_PER_SYMBOL = 6
NEWS_PER_SYMBOL = 4
HEDGER_PER_SYMBOL = 2
RL_PER_SYMBOL = 2

SYMBOLS: list[Symbol] = list(Symbol)
CAPITAL = settings.swarm_capital_per_agent


async def _restore_agent_state(agent: BaseAgent, repository: AsyncRepository) -> None:
    """Replays an agent's closed trades from the DB so equity/win-rate/sharpe
    survive a restart instead of resetting to initial_capital every time.
    Only works because agent_id is now stable across restarts (see below) —
    trades saved under the old random-UUID ids are orphaned and won't match."""
    trades = await repository.get_agent_trades(agent.agent_id, limit=None)
    if not trades:
        return
    for row in reversed(trades):  # DB returns newest-first; replay oldest-first
        if not row["closed_at"]:
            continue
        trade = ExecutedTrade(
            trade_id=row["trade_id"],
            agent_id=row["agent_id"],
            symbol=Symbol(row["symbol"]),
            side=Side(row["side"]),
            entry_price=row["entry_price"],
            quantity=row["quantity"],
            sl_price=row["sl_price"],
            tp_price=row["tp_price"],
            status=OrderStatus(row["status"]),
            pnl=row["pnl"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=datetime.fromisoformat(row["closed_at"]),
        )
        agent.record_trade(trade)
    m = agent.get_metrics()
    logger.info(f"[{agent.agent_id}] Restored {m.total_trades} historical trades | equity=${m.equity:.2f}")


async def build_swarm(orchestrator: SwarmOrchestrator, repository: AsyncRepository | None = None) -> int:
    """Register all agents in the orchestrator. Returns total count."""
    count = 0

    for symbol in SYMBOLS:
        # ── Scalpers ──────────────────────────────────────────────────────
        for i in range(SCALPERS_PER_SYMBOL):
            agent: BaseAgent = ScalperAgent(
                symbol=symbol,
                agent_id=f"SCALPER_{symbol.value}_{i}",
                initial_capital=CAPITAL,
                atr_sl_multiplier=1.5 + i * 0.1,  # each slightly different
                atr_tp_multiplier=3.0 + i * 0.2,
            )
            if repository:
                await _restore_agent_state(agent, repository)
            orchestrator.register_agent(agent)
            count += 1

        # ── Swing (real SwingAgent — EMA cross + ADX trend filter) ─────────
        for i in range(SWING_PER_SYMBOL):
            agent = SwingAgent(
                symbol=symbol,
                agent_id=f"SWING_{symbol.value}_{i}",
                initial_capital=CAPITAL,
                adx_threshold=22.0 + i * 1.5,  # each slightly different
                atr_sl_mult=2.0 + i * 0.1,
                atr_tp_mult=4.0 + i * 0.5,
            )
            if repository:
                await _restore_agent_state(agent, repository)
            orchestrator.register_agent(agent)
            count += 1

        # ── News Reactive ─────────────────────────────────────────────────
        for i in range(NEWS_PER_SYMBOL):
            agent = NewsReactiveAgent(
                symbol=symbol,
                agent_id=f"NEWS_REACTIVE_{symbol.value}_{i}",
                initial_capital=CAPITAL,
                entry_window_seconds=20 + i * 10,
            )
            if repository:
                await _restore_agent_state(agent, repository)
            orchestrator.register_agent(agent)
            count += 1

        # ── Hedgers ───────────────────────────────────────────────────────
        for i in range(HEDGER_PER_SYMBOL):
            agent = HedgerAgent(
                symbol=symbol,
                agent_id=f"HEDGER_{symbol.value}_{i}",
                initial_capital=CAPITAL,
                atr_sl_multiplier=2.0 + i * 0.2,
                atr_tp_multiplier=3.0 + i * 0.3,
            )
            if repository:
                await _restore_agent_state(agent, repository)
            orchestrator.register_agent(agent)
            count += 1

        # ── RL (agents/rl) ───────────────────────────────────────────────
        for i in range(RL_PER_SYMBOL):
            agent = RLAgent(
                symbol=symbol,
                agent_id=f"RL_{symbol.value}_{i}",
                initial_capital=CAPITAL,
                atr_sl_multiplier=2.0 + i * 0.2,
                atr_tp_multiplier=4.0 + i * 0.5,
            )
            if repository:
                await _restore_agent_state(agent, repository)
            orchestrator.register_agent(agent)
            count += 1

    return count
