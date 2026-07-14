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
from swarm_trading.core.config import settings
from swarm_trading.core.models import Symbol
from swarm_trading.core.orchestrator.orchestrator import SwarmOrchestrator
from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
from swarm_trading.agents.swing.swing_agent import SwingAgent
from swarm_trading.agents.news_reactive.news_agent import NewsReactiveAgent
from swarm_trading.agents.hedger.hedger_agent import HedgerAgent
from swarm_trading.agents.rl.rl_agent import RLAgent


SCALPERS_PER_SYMBOL   = 6
SWING_PER_SYMBOL      = 6
NEWS_PER_SYMBOL       = 4
HEDGER_PER_SYMBOL     = 2
RL_PER_SYMBOL         = 2

SYMBOLS = list(Symbol)
CAPITAL = settings.swarm_capital_per_agent


def build_swarm(orchestrator: SwarmOrchestrator) -> int:
    """Register all agents in the orchestrator. Returns total count."""
    count = 0

    for symbol in SYMBOLS:
        # ── Scalpers ──────────────────────────────────────────────────────
        for i in range(SCALPERS_PER_SYMBOL):
            agent = ScalperAgent(
                symbol=symbol,
                initial_capital=CAPITAL,
                atr_sl_multiplier=1.5 + i * 0.1,   # each slightly different
                atr_tp_multiplier=3.0 + i * 0.2,
            )
            orchestrator.register_agent(agent)
            count += 1

        # ── Swing (real SwingAgent — EMA cross + ADX trend filter) ─────────
        for i in range(SWING_PER_SYMBOL):
            agent = SwingAgent(
                symbol=symbol,
                initial_capital=CAPITAL,
                adx_threshold=22.0 + i * 1.5,   # each slightly different
                atr_sl_mult=2.0 + i * 0.1,
                atr_tp_mult=4.0 + i * 0.5,
            )
            orchestrator.register_agent(agent)
            count += 1

        # ── News Reactive ─────────────────────────────────────────────────
        for i in range(NEWS_PER_SYMBOL):
            agent = NewsReactiveAgent(
                symbol=symbol,
                initial_capital=CAPITAL,
                entry_window_seconds=20 + i * 10,
            )
            orchestrator.register_agent(agent)
            count += 1

        # ── Hedgers ───────────────────────────────────────────────────────
        for i in range(HEDGER_PER_SYMBOL):
            agent = HedgerAgent(
                symbol=symbol,
                initial_capital=CAPITAL,
                atr_sl_multiplier=2.0 + i * 0.2,
                atr_tp_multiplier=3.0 + i * 0.3,
            )
            orchestrator.register_agent(agent)
            count += 1

        # ── RL (agents/rl) ───────────────────────────────────────────────
        for i in range(RL_PER_SYMBOL):
            agent = RLAgent(
                symbol=symbol,
                initial_capital=CAPITAL,
                atr_sl_multiplier=2.0 + i * 0.2,
                atr_tp_multiplier=4.0 + i * 0.5,
            )
            orchestrator.register_agent(agent)
            count += 1

    return count
