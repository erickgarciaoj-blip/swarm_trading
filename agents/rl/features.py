"""
Shared observation-vector construction for RL agents — used identically by
live inference (RLAgent.analyze) and training (SwarmTradingEnv), so a policy
trained on one feature space acts on the exact same one live.
"""

from __future__ import annotations

import numpy as np

from swarm_trading.core.models import MarketState

FEATURE_NAMES = ["rsi_norm", "atr_pct", "ema20_dev", "ema50_dev", "ema200_dev", "equity_ratio"]
N_FEATURES = len(FEATURE_NAMES)


def _vector(
    rsi: float | None, atr: float, ema20: float, ema50: float, ema200: float, close: float, equity_ratio: float
) -> np.ndarray:
    return np.array(
        [
            (rsi if rsi is not None else 50.0) / 100.0,
            (atr / close) if close else 0.0,
            (ema20 / close - 1.0) if close else 0.0,
            (ema50 / close - 1.0) if close else 0.0,
            (ema200 / close - 1.0) if close else 0.0,
            float(np.clip(equity_ratio, 0.0, 2.0)),
        ],
        dtype=np.float32,
    )


def build_observation(market_state: MarketState, equity_ratio: float) -> np.ndarray:
    ind = market_state.indicators
    close = market_state.candles[-1].close if market_state.candles else 0.0
    return _vector(
        ind.get("rsi_14"),
        ind.get("atr_14", 0.0),
        ind.get("ema_20", close),
        ind.get("ema_50", close),
        ind.get("ema_200", close),
        close,
        equity_ratio,
    )


def build_observation_from_row(row, equity_ratio: float) -> np.ndarray:
    close = row["close"]
    return _vector(row["rsi_14"], row["atr_14"], row["ema_20"], row["ema_50"], row["ema_200"], close, equity_ratio)


def validate_observation(obs: np.ndarray) -> str | None:
    """Gate before model.predict(): returns None if `obs` is safe to feed to
    the model, otherwise a short human-readable reason naming the offending
    feature(s). Deliberately doesn't try to *fix* a bad vector (e.g. by
    imputing zeros) — see docs/architecture/adr/0007-rl-inference-input-validation.md
    for why "no signal" is the safe default when the input can't be trusted."""
    if obs.shape != (N_FEATURES,):
        return f"unexpected observation shape {obs.shape}, expected ({N_FEATURES},)"

    finite_mask = np.isfinite(obs)
    if not finite_mask.all():
        bad_names = [FEATURE_NAMES[i] for i in np.where(~finite_mask)[0].tolist()]
        nan_present = bool(np.isnan(obs[~finite_mask]).any())
        inf_present = bool(np.isinf(obs[~finite_mask]).any())
        if nan_present and not inf_present:
            kind = "NaN"
        elif inf_present and not nan_present:
            kind = "infinite"
        else:
            kind = "non-finite"
        return f"{kind} value(s) in feature(s): {', '.join(bad_names)}"

    return None
