"""
Offline/incremental training environment for RL trading agents.

Feeds a single symbol's historical (close, indicator) rows to a PPO agent
one bar at a time. Action controls the position held *during the next bar*;
reward is that bar's percentage return in the chosen direction. This is a
simplification (no slippage, no partial fills, no ATR-based SL/TP hit
simulation) — good enough to bootstrap/refresh a policy that RLAgent then
trades live through the real RiskEngine/broker.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import gymnasium as gym
from gymnasium import spaces

from swarm_trading.agents.rl.features import N_FEATURES, build_observation_from_row

ACTION_FLAT, ACTION_LONG, ACTION_SHORT = 0, 1, 2
_DIRECTION = {ACTION_FLAT: 0.0, ACTION_LONG: 1.0, ACTION_SHORT: -1.0}

REQUIRED_COLUMNS = {"close", "rsi_14", "atr_14", "ema_20", "ema_50", "ema_200"}


class SwarmTradingEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, feature_df: pd.DataFrame, initial_equity_ratio: float = 1.0):
        super().__init__()
        missing = REQUIRED_COLUMNS - set(feature_df.columns)
        if missing:
            raise ValueError(f"feature_df missing columns: {missing}")
        if len(feature_df) < 2:
            raise ValueError("feature_df needs at least 2 rows")

        self._df = feature_df.reset_index(drop=True)
        self._initial_equity_ratio = initial_equity_ratio
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(N_FEATURES,), dtype=np.float32)
        self.action_space = spaces.Discrete(3)  # 0=FLAT, 1=LONG, 2=SHORT

        self._i = 0
        self._equity_ratio = initial_equity_ratio

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self._i = 0
        self._equity_ratio = self._initial_equity_ratio
        return self._obs(), {}

    def step(self, action: int):
        row = self._df.iloc[self._i]
        has_next = self._i + 1 < len(self._df)
        next_close = self._df.iloc[self._i + 1]["close"] if has_next else row["close"]

        direction = _DIRECTION[int(action)]
        pct_return = (next_close - row["close"]) / row["close"] if row["close"] else 0.0
        reward = direction * pct_return

        self._equity_ratio = max(0.0, self._equity_ratio * (1.0 + reward))
        self._i += 1

        terminated = self._equity_ratio <= 0.0
        truncated = not has_next
        return self._obs(), float(reward), terminated, truncated, {}

    def _obs(self) -> np.ndarray:
        row = self._df.iloc[min(self._i, len(self._df) - 1)]
        return build_observation_from_row(row, self._equity_ratio)
