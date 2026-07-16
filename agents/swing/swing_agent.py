"""
Swing Agent — trend-following, holds positions across many candles.
Target timeframe: the 15m-1h candles delivered in MarketState.
Logic: EMA fast/slow crossover confirmed by ADX (real trend strength filter).
ADX is not produced by the market feed today, so this agent computes it
itself (Wilder's formula) from the raw candles whenever it's missing from
MarketState.indicators — no TA-Lib dependency required.
Wider ATR-based SL/TP than ScalperAgent since it rides bigger moves.
Each instance gets a slightly jittered ADX threshold so the whole swing
cohort doesn't fire (or all sit out) on the exact same bar — no cascade failure.
"""

from __future__ import annotations

import random

from loguru import logger

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.core.models import (
    AgentType,
    Candle,
    ExecutedTrade,
    MarketState,
    OrderProposal,
    Side,
    Symbol,
)


class SwingAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        ema_fast: int = 20,
        ema_slow: int = 50,
        adx_threshold: float = 25.0,
        atr_sl_mult: float = 2.5,
        atr_tp_mult: float = 5.0,
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.SWING, initial_capital=initial_capital, **kwargs)
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        # Small per-instance jitter around the requested threshold so the
        # SwarmFactory's cohort of SwingAgents doesn't all agree/disagree on
        # the exact same bar.
        self.adx_threshold = max(0.0, adx_threshold + round(random.uniform(-1.5, 1.5), 2))
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self._last_side: Side | None = None

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.candles or market_state.is_news_blackout:
            return None

        candles = market_state.candles
        close = candles[-1].close
        ind = market_state.indicators

        ema_fast = ind.get(f"ema_{self.ema_fast}")
        if ema_fast is None:
            ema_fast = self._compute_ema(candles, self.ema_fast)

        ema_slow = ind.get(f"ema_{self.ema_slow}")
        if ema_slow is None:
            ema_slow = self._compute_ema(candles, self.ema_slow)

        atr = ind.get("atr_14")
        if atr is None:
            atr = self._compute_atr(candles, 14)

        adx = ind.get("adx_14")
        if adx is None:
            adx = self._compute_adx(candles, 14)

        if ema_fast is None or ema_slow is None or atr is None or adx is None:
            return None

        if adx <= self.adx_threshold:
            return None  # not enough real trend to trade

        side: Side | None = None
        if ema_fast > ema_slow:
            side = Side.LONG
        elif ema_fast < ema_slow:
            side = Side.SHORT

        if side is None or side == self._last_side:
            return None  # flat cross, or already riding this trend

        sl_dist = atr * self.atr_sl_mult
        tp_dist = atr * self.atr_tp_mult

        if side == Side.LONG:
            sl_price = close - sl_dist
            tp_price = close + tp_dist
        else:
            sl_price = close + sl_dist
            tp_price = close - tp_dist

        self._last_side = side

        proposal = OrderProposal(
            agent_id=self.agent_id,
            symbol=self.symbol,
            side=side,
            quantity=self.calc_notional(close),
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            confidence=round(min(1.0, adx / 100), 3),
            price=close,
            reason=(f"EMA{self.ema_fast}={ema_fast:.5f} EMA{self.ema_slow}={ema_slow:.5f} ADX={adx:.2f} ATR={atr:.5f}"),
        )
        logger.info(
            f"[{self.agent_id}] Signal {side.value} {self.symbol.value} @ {close:.5f} "
            f"| ADX={adx:.2f} (thr={self.adx_threshold:.2f})"
        )
        return proposal

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)

    # ─── Self-contained indicator fallbacks ───────────────────────────────
    # Used only when MarketState.indicators doesn't already carry the value.
    # Keeps the agent independent of TA-Lib being installed/available.

    @staticmethod
    def _compute_ema(candles: list[Candle], period: int) -> float | None:
        if len(candles) < period:
            return None
        closes = [c.close for c in candles[-period * 3 :]]  # a bit of warm-up
        k = 2 / (period + 1)
        ema = closes[0]
        for price in closes[1:]:
            ema = price * k + ema * (1 - k)
        return ema

    @staticmethod
    def _compute_atr(candles: list[Candle], period: int) -> float | None:
        if len(candles) < period + 1:
            return None
        trs: list[float] = []
        for i in range(1, len(candles)):
            high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        window = trs[-period:]
        return sum(window) / len(window)

    @staticmethod
    def _compute_adx(candles: list[Candle], period: int = 14) -> float | None:
        """Wilder's ADX computed directly from OHLC candles — no external TA
        library needed. Needs at least 2*period+1 candles for a properly
        smoothed reading; returns None if there isn't enough history yet."""
        n = len(candles)
        if n < period * 2 + 1:
            return None

        plus_dm: list[float] = []
        minus_dm: list[float] = []
        tr: list[float] = []
        for i in range(1, n):
            up_move = candles[i].high - candles[i - 1].high
            down_move = candles[i - 1].low - candles[i].low
            plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
            minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
            high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
            tr.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

        def wilder_smooth(values: list[float]) -> list[float]:
            smoothed = [sum(values[:period])]
            for v in values[period:]:
                smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
            return smoothed

        tr_s = wilder_smooth(tr)
        plus_dm_s = wilder_smooth(plus_dm)
        minus_dm_s = wilder_smooth(minus_dm)

        dx: list[float] = []
        for tr_v, pdm_v, mdm_v in zip(tr_s, plus_dm_s, minus_dm_s, strict=True):
            if tr_v == 0:
                dx.append(0.0)
                continue
            plus_di = 100 * pdm_v / tr_v
            minus_di = 100 * mdm_v / tr_v
            di_sum = plus_di + minus_di
            dx.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

        if len(dx) < period:
            return None
        return sum(dx[-period:]) / period
