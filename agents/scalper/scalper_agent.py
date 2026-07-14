"""
Scalper Agent — operates on 1m/5m candles.
Logic: RSI extremes + ATR-based SL/TP.
Each instance has different RSI thresholds to ensure policy diversity.
"""
from __future__ import annotations
import random
from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.core.models import (
    AgentType, ExecutedTrade, MarketState, OrderProposal, Side, Symbol,
)


class ScalperAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        rsi_oversold: float | None = None,
        rsi_overbought: float | None = None,
        atr_sl_multiplier: float = 1.5,
        atr_tp_multiplier: float = 3.0,
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.SCALPER, initial_capital=initial_capital, **kwargs)
        # Randomize thresholds slightly so agents are not identical → no cascade failure
        self.rsi_oversold   = rsi_oversold   or round(random.uniform(25, 35), 1)
        self.rsi_overbought = rsi_overbought or round(random.uniform(65, 75), 1)
        self.atr_sl_multiplier = atr_sl_multiplier
        self.atr_tp_multiplier = atr_tp_multiplier

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.candles or market_state.is_news_blackout:
            return None

        ind = market_state.indicators
        rsi = ind.get("rsi_14")
        atr = ind.get("atr_14")
        close = market_state.candles[-1].close

        if rsi is None or atr is None:
            return None

        side: Side | None = None
        if rsi < self.rsi_oversold:
            side = Side.LONG
        elif rsi > self.rsi_overbought:
            side = Side.SHORT

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

        quantity = self.calc_notional(close)

        return OrderProposal(
            agent_id=self.agent_id,
            symbol=self.symbol,
            side=side,
            quantity=quantity,
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            confidence=round(min(1.0, abs(50 - rsi) / 50), 3),
            price=close,
            reason=f"RSI={rsi:.1f} ATR={atr:.5f}",
        )

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)
