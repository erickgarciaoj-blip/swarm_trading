"""
Hedger Agent — counter-trades swarm-wide directional imbalance.

Doesn't look at price action at all: it only reacts to how lopsided the
rest of the swarm currently is (swarm_long_count / swarm_short_count on
MarketState.indicators). If too many agents are long, it opens a SHORT to
offset exposure, and vice versa — a portfolio-level hedge, not a signal on
the instrument itself.

NOTE: swarm_long_count/swarm_short_count aren't populated by MarketFeed or
the orchestrator anywhere yet — nothing in this codebase currently tracks
each agent's open-position side. Until something injects those keys into
MarketState.indicators, this agent will just see the .get(..., 0) defaults
and stay dormant (never propose a trade). See BaseAgent/SwarmOrchestrator
if you want to wire that up.
"""
from __future__ import annotations
from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.core.models import (
    AgentType, ExecutedTrade, MarketState, OrderProposal, Side, Symbol,
)

SWARM_IMBALANCE_THRESHOLD = 7


class HedgerAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        atr_sl_multiplier: float = 2.0,
        atr_tp_multiplier: float = 3.0,
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.HEDGER, initial_capital=initial_capital, **kwargs)
        self.atr_sl_multiplier = atr_sl_multiplier
        self.atr_tp_multiplier = atr_tp_multiplier

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.candles or market_state.is_news_blackout:
            return None

        ind = market_state.indicators
        atr = ind.get("atr_14")
        close = market_state.candles[-1].close
        if atr is None:
            return None

        long_count = ind.get("swarm_long_count", 0)
        short_count = ind.get("swarm_short_count", 0)

        side: Side | None = None
        if long_count > SWARM_IMBALANCE_THRESHOLD:
            side = Side.SHORT  # too many longs — hedge by shorting
        elif short_count > SWARM_IMBALANCE_THRESHOLD:
            side = Side.LONG   # too many shorts — hedge by going long

        if side is None:
            return None

        sl_dist = atr * self.atr_sl_multiplier
        tp_dist = atr * self.atr_tp_multiplier

        if side == Side.LONG:
            sl_price = close - sl_dist
            tp_price = close + tp_dist
        else:
            sl_price = close + sl_dist
            tp_price = close - tp_dist

        return OrderProposal(
            agent_id=self.agent_id,
            symbol=self.symbol,
            side=side,
            quantity=self.calc_notional(close, risk_pct=0.01),
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            confidence=0.5,
            price=close,
            reason=f"HEDGE long={long_count} short={short_count} ATR={atr:.5f}",
        )

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)
