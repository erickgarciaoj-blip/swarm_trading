"""
News-Reactive Agent — only activates around high-impact news events.
Trades the initial momentum spike after major releases (NFP, CPI, FOMC, etc.).
"""

from __future__ import annotations

from datetime import datetime

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.core.models import (
    AgentType,
    ExecutedTrade,
    MarketState,
    NewsImpact,
    OrderProposal,
    Side,
    Symbol,
)


class NewsReactiveAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        entry_window_seconds: int = 30,  # seconds after news to enter
        hold_candles: int = 5,  # how many candles to hold max
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.NEWS_REACTIVE, initial_capital=initial_capital, **kwargs)
        self.entry_window_seconds = entry_window_seconds
        self.hold_candles = hold_candles
        self._last_entry_time: datetime | None = None

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.upcoming_news:
            return None

        high_impact = [n for n in market_state.upcoming_news if n.impact == NewsImpact.HIGH]
        if not high_impact:
            return None

        now = market_state.timestamp
        for event in high_impact:
            delta = (now - event.timestamp).total_seconds()
            # Only act in the window just after the event
            if 0 <= delta <= self.entry_window_seconds:
                ind = market_state.indicators
                close = market_state.candles[-1].close
                atr = ind.get("atr_14", close * 0.001)

                # Simple momentum: direction from close vs EMA20
                ema20 = ind.get("ema_20", close)
                side = Side.LONG if close > ema20 else Side.SHORT

                sl_price = close - atr * 2 if side == Side.LONG else close + atr * 2
                tp_price = close + atr * 4 if side == Side.LONG else close - atr * 4

                quantity = self.calc_notional(close, risk_pct=0.03)

                return OrderProposal(
                    agent_id=self.agent_id,
                    symbol=self.symbol,
                    side=side,
                    quantity=quantity,
                    sl_price=round(sl_price, 5),
                    tp_price=round(tp_price, 5),
                    confidence=0.75,
                    price=close,
                    reason=f"NEWS:{event.title} delta={delta:.0f}s",
                )
        return None

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)
