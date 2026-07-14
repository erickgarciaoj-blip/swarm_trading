"""
RL Agent — PPO (Stable-Baselines3) policy that starts from a per-symbol
checkpoint under settings.rl_model_dir (or a fresh, untrained policy if none
exists yet) and periodically retrains itself on the freshest historical
window, persisting the updated weights so it keeps improving across runs.

stable_baselines3/gymnasium/pandas are imported lazily, inside methods, so
importing this module — and therefore agents.templates.swarm_factory and
main.py — doesn't hard-require those heavy optional dependencies unless an
RL agent actually runs, mirroring how MT5Broker lazily imports MetaTrader5.
"""
from __future__ import annotations
import asyncio
from pathlib import Path
import numpy as np
from loguru import logger

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.agents.rl.features import build_observation
from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentStatus, AgentType, ExecutedTrade, MarketState, OrderProposal, Side, Symbol,
)

ACTION_FLAT, ACTION_LONG, ACTION_SHORT = 0, 1, 2
COLD_START_CONFIDENCE = 0.5
TRAINED_CONFIDENCE = 0.75


class RLAgent(BaseAgent):
    def __init__(
        self,
        symbol: Symbol,
        initial_capital: float = 1.0,
        atr_sl_multiplier: float = 2.0,
        atr_tp_multiplier: float = 4.0,
        retrain_every_n_trades: int | None = None,
        model_path: str | None = None,
        **kwargs,
    ):
        super().__init__(symbol=symbol, agent_type=AgentType.RL, initial_capital=initial_capital, **kwargs)
        self.atr_sl_multiplier = atr_sl_multiplier
        self.atr_tp_multiplier = atr_tp_multiplier
        self.retrain_every_n_trades = retrain_every_n_trades or settings.rl_retrain_every_n_trades

        self._model_path = Path(model_path or settings.rl_model_dir) / f"{symbol.value}_ppo.zip"
        self._model = None
        self._is_pretrained = self._model_path.exists()
        self._trades_since_retrain = 0

    # ─── Model lifecycle ─────────────────────────────────────────────────────

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        from stable_baselines3 import PPO
        import pandas as pd
        from swarm_trading.agents.rl.env import SwarmTradingEnv

        if self._model_path.exists():
            self._model = PPO.load(str(self._model_path))
            logger.info(f"[{self.agent_id}] Loaded checkpoint {self._model_path}")
        else:
            # A fresh policy still needs *an* env to infer obs/action spaces
            # from — two placeholder rows are enough; no learning happens here.
            dummy_df = pd.DataFrame([{
                "close": 1.0, "rsi_14": 50.0, "atr_14": 0.01,
                "ema_20": 1.0, "ema_50": 1.0, "ema_200": 1.0,
            }] * 2)
            self._model = PPO("MlpPolicy", SwarmTradingEnv(dummy_df), verbose=0)
            logger.info(f"[{self.agent_id}] No checkpoint found — starting from a fresh policy")
        return self._model

    # ─── Live inference ──────────────────────────────────────────────────────

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        if not market_state.candles or market_state.is_news_blackout:
            return None

        atr = market_state.indicators.get("atr_14")
        close = market_state.candles[-1].close
        if atr is None:
            return None

        model = self._ensure_model()
        equity_ratio = self.equity / self.target_equity if self.target_equity else 1.0
        obs = build_observation(market_state, equity_ratio)

        action, _ = model.predict(obs, deterministic=True)
        action = int(action)

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

    # ─── Learning from experience ────────────────────────────────────────────

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)
        self._trades_since_retrain += 1
        if self._trades_since_retrain >= self.retrain_every_n_trades:
            await self._retrain()

    async def _retrain(self) -> None:
        self._trades_since_retrain = 0
        prior_status = self.status
        self.status = AgentStatus.TRAINING  # excluded from active_agents meanwhile
        logger.info(f"[{self.agent_id}] Retraining on fresh {self.symbol.value} data...")
        try:
            await asyncio.to_thread(self._train_and_save)
        except Exception as exc:
            logger.warning(f"[{self.agent_id}] Retrain failed: {exc}")
        finally:
            if self.status == AgentStatus.TRAINING:
                self.status = prior_status

    def _train_and_save(self) -> None:
        """Runs on a worker thread (via asyncio.to_thread) so retraining
        never blocks the orchestrator's event loop."""
        from swarm_trading.agents.rl.env import SwarmTradingEnv
        from swarm_trading.agents.rl.data import fetch_feature_frame

        model = self._ensure_model()
        df = fetch_feature_frame(self.symbol)
        model.set_env(SwarmTradingEnv(df))
        model.learn(total_timesteps=settings.rl_incremental_timesteps)

        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        model.save(str(self._model_path))
        self._is_pretrained = True
        logger.info(f"[{self.agent_id}] Saved updated checkpoint → {self._model_path}")
