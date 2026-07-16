"""
RL Agent — PPO (Stable-Baselines3) policy, INFERENCE ONLY.

Loads a per-symbol checkpoint from settings.rl_model_dir (or a fresh,
untrained policy if none exists yet) and predicts. It never trains itself —
training is a separate, offline process (see agents/rl/train.py), run on a
different machine or on-demand, never inside the live trading process
(see docs/architecture/adr/0001-rl-inference-only-in-production.md).

To update an agent's policy, drop a new `{symbol}_ppo.zip` at its model path
(e.g. `scp` it in) — _ensure_model() compares the file's mtime on every tick
and hot-reloads on change, so there's no need to restart the swarm.

stable_baselines3/gymnasium/pandas are imported lazily, inside methods, so
importing this module — and therefore agents.templates.swarm_factory and
main.py — doesn't hard-require those heavy optional dependencies unless an
RL agent actually runs, mirroring how MT5Broker lazily imports MetaTrader5.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

if TYPE_CHECKING:
    # stable_baselines3 stays a lazy, optional runtime import (see module
    # docstring) — this is only evaluated by mypy, never at runtime.
    from stable_baselines3 import PPO

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.agents.rl.features import build_observation, validate_observation
from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentType,
    ExecutedTrade,
    MarketState,
    OrderProposal,
    Side,
    Symbol,
)

ACTION_FLAT, ACTION_LONG, ACTION_SHORT = 0, 1, 2
COLD_START_CONFIDENCE = 0.5
TRAINED_CONFIDENCE = 0.75

# Matches the RSI-14/ATR-14 rolling window in MarketFeed._compute_indicators
# — fewer candles than this and those two indicators are NaN by construction
# (a pandas .rolling(14) needs 14 rows before it produces its first value).
MIN_CANDLES_REQUIRED = 14


class RLAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        atr_sl_multiplier: float = 2.0,
        atr_tp_multiplier: float = 4.0,
        model_path: str | None = None,
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.RL, initial_capital=initial_capital, **kwargs)
        self.atr_sl_multiplier = atr_sl_multiplier
        self.atr_tp_multiplier = atr_tp_multiplier

        self._model_path = Path(model_path or settings.rl_model_dir) / f"{symbol.value}_ppo.zip"
        self._model: PPO | None = None
        self._model_mtime: float | None = None
        self._is_pretrained = self._model_path.exists()

        # Counters for the two ways a tick can be rejected before reaching
        # model.predict() — see docs/architecture/adr/0007. Public/no
        # leading underscore so they're inspectable without an accessor;
        # become real Prometheus counters once Fase 9 wires up /metrics.
        self.insufficient_history_count = 0
        self.invalid_observation_count = 0

    # ─── Model lifecycle ─────────────────────────────────────────────────────

    def _load_model(self, path: str) -> PPO:
        from stable_baselines3 import PPO

        return PPO.load(path)

    def _build_fresh_model(self) -> PPO:
        import pandas as pd
        from stable_baselines3 import PPO

        from swarm_trading.agents.rl.env import SwarmTradingEnv

        # A fresh policy still needs *an* env to infer obs/action spaces
        # from — two placeholder rows are enough; no learning happens here.
        dummy_df = pd.DataFrame(
            [
                {
                    "close": 1.0,
                    "rsi_14": 50.0,
                    "atr_14": 0.01,
                    "ema_20": 1.0,
                    "ema_50": 1.0,
                    "ema_200": 1.0,
                }
            ]
            * 2
        )
        return PPO("MlpPolicy", SwarmTradingEnv(dummy_df), verbose=0)

    async def _ensure_model(self) -> PPO:
        """stat() itself is a cheap local syscall, left un-threaded (see
        ADR-0002) — but PPO.load()/construction deserializes weights from
        disk and is not free, so *that* part runs off the event loop.

        Deploying a new checkpoint (e.g. via `scp`) is not atomic from this
        process's point of view: a write can be caught mid-transfer, which
        would make PPO.load() raise on a truncated/corrupt zip. If that
        happens, this keeps serving the last good in-memory model instead of
        crashing the agent — a bad deploy should degrade to "stale model",
        never to "agent stops trading" (see ADR-0003). The mtime is only
        updated on a *successful* load, so a corrupt drop gets retried on
        every subsequent tick until a valid file lands.
        """
        if self._model_path.exists():
            mtime = self._model_path.stat().st_mtime
            if self._model is None or mtime != self._model_mtime:
                try:
                    new_model = await asyncio.to_thread(self._load_model, str(self._model_path))
                except Exception as exc:
                    if self._model is not None:
                        logger.warning(
                            f"[{self.agent_id}] Failed to hot-swap checkpoint {self._model_path} "
                            f"({exc}) — keeping previous model in place"
                        )
                        return self._model
                    raise  # nothing to fall back to yet — this is the first load ever
                self._model = new_model
                self._model_mtime = mtime
                self._is_pretrained = True
                logger.info(f"[{self.agent_id}] Loaded checkpoint {self._model_path} (mtime={mtime})")
            return self._model

        if self._model is not None:
            return self._model

        self._model = await asyncio.to_thread(self._build_fresh_model)
        logger.info(f"[{self.agent_id}] No checkpoint found — starting from a fresh policy")
        return self._model

    # ─── Live inference ──────────────────────────────────────────────────────

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.candles or market_state.is_news_blackout:
            return None

        # Gate 1: enough history for the rolling indicators to be real
        # numbers at all (see MIN_CANDLES_REQUIRED). Cheaper and more
        # specific than waiting to discover NaN later, so it gets its own
        # counter/log reason instead of falling through to gate 3's generic
        # "non-finite" message.
        n_candles = len(market_state.candles)
        if n_candles < MIN_CANDLES_REQUIRED:
            self.insufficient_history_count += 1
            logger.bind(
                agent_id=self.agent_id, symbol=self.symbol.value, candles=n_candles, required=MIN_CANDLES_REQUIRED
            ).warning(
                f"[{self.agent_id}] Skipping inference for {self.symbol.value}: "
                f"insufficient history ({n_candles} < {MIN_CANDLES_REQUIRED} candles)"
            )
            return None

        atr = market_state.indicators.get("atr_14")
        close = market_state.candles[-1].close

        # Gate 2: atr/close feed sl_price/tp_price math further down, outside
        # the observation vector gate 3 checks — a NaN here would otherwise
        # reach the broker as a NaN order price, not just a bad model input.
        if atr is None or not math.isfinite(atr) or not math.isfinite(close):
            self.invalid_observation_count += 1
            logger.bind(
                agent_id=self.agent_id, symbol=self.symbol.value, feature="atr_14/close", atr=atr, close=close
            ).warning(f"[{self.agent_id}] Skipping inference for {self.symbol.value}: atr_14/close missing/non-finite")
            return None

        model = await self._ensure_model()
        equity_ratio = self.equity / self.target_equity if self.target_equity else 1.0
        obs = build_observation(market_state, equity_ratio)

        # Gate 3: the full observation vector, feature by feature. No
        # imputation on failure (e.g. zero-filling) — a signal built on data
        # we don't trust is worse than no signal at all (see ADR-0007).
        validation_error = validate_observation(obs)
        if validation_error is not None:
            self.invalid_observation_count += 1
            logger.bind(agent_id=self.agent_id, symbol=self.symbol.value, reason=validation_error).warning(
                f"[{self.agent_id}] Skipping inference for {self.symbol.value}: {validation_error}"
            )
            return None

        # model.predict() is a single forward pass over a tiny MLP on a
        # handful of scalar features — sub-millisecond, deliberately left
        # un-threaded (same reasoning as the stat() call above).
        raw_action, _ = model.predict(obs, deterministic=True)
        action = int(raw_action)

        if action == ACTION_FLAT:
            return None

        side = Side.LONG if action == ACTION_LONG else Side.SHORT
        sl_dist = atr * self.atr_sl_multiplier
        tp_dist = atr * self.atr_tp_multiplier
        sl_price = close - sl_dist if side == Side.LONG else close + sl_dist
        tp_price = close + tp_dist if side == Side.LONG else close - tp_dist

        return OrderProposal(
            agent_id=self.agent_id,
            symbol=self.symbol,
            side=side,
            quantity=self.calc_notional(close, risk_pct=0.02),
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            confidence=TRAINED_CONFIDENCE if self._is_pretrained else COLD_START_CONFIDENCE,
            price=close,
            reason=f"RL action={action} obs={np.round(obs, 4).tolist()}",
        )

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)
