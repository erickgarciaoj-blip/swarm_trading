"""Unit tests for the RL observation-vector builder (no ML deps needed)."""
from datetime import datetime
import numpy as np
import pytest

from swarm_trading.agents.rl.features import build_observation, build_observation_from_row, N_FEATURES
from swarm_trading.core.models import Candle, MarketState, Symbol


def _state(rsi=60.0, atr=2.0, ema20=105.0, ema50=100.0, ema200=95.0, close=110.0):
    candle = Candle(symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(),
                     open=close, high=close, low=close, close=close, volume=1.0)
    return MarketState(
        symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), candles=[candle],
        indicators={"rsi_14": rsi, "atr_14": atr, "ema_20": ema20, "ema_50": ema50, "ema_200": ema200},
    )


def test_observation_has_expected_shape_and_dtype():
    obs = build_observation(_state(), equity_ratio=0.5)
    assert obs.shape == (N_FEATURES,)
    assert obs.dtype == np.float32


def test_rsi_is_normalized_to_0_1():
    obs = build_observation(_state(rsi=80.0), equity_ratio=1.0)
    assert obs[0] == pytest.approx(0.8)


def test_ema_deviation_signs_reflect_trend_direction():
    obs = build_observation(_state(ema20=105.0, ema50=100.0, ema200=95.0, close=110.0), equity_ratio=1.0)
    assert obs[2] < 0  # ema20 below close
    assert obs[4] < 0  # ema200 below close


def test_equity_ratio_is_clipped_to_0_2_range():
    obs = build_observation(_state(), equity_ratio=5.0)
    assert obs[5] == pytest.approx(2.0)
    obs_neg = build_observation(_state(), equity_ratio=-1.0)
    assert obs_neg[5] == pytest.approx(0.0)


def test_missing_close_does_not_divide_by_zero():
    obs = build_observation(_state(close=0.0), equity_ratio=1.0)
    assert np.isfinite(obs).all()


def test_build_observation_from_row_matches_build_observation():
    row = {"close": 110.0, "rsi_14": 60.0, "atr_14": 2.0, "ema_20": 105.0, "ema_50": 100.0, "ema_200": 95.0}
    from_row = build_observation_from_row(row, equity_ratio=0.5)
    from_state = build_observation(_state(), equity_ratio=0.5)
    np.testing.assert_array_almost_equal(from_row, from_state)
