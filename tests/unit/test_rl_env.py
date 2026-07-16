"""Unit tests for SwarmTradingEnv (requires gymnasium + pandas)."""

import pandas as pd
import pytest

from swarm_trading.agents.rl.env import ACTION_FLAT, ACTION_LONG, ACTION_SHORT, SwarmTradingEnv


def _df(closes):
    return pd.DataFrame(
        {
            "close": closes,
            "rsi_14": [50.0] * len(closes),
            "atr_14": [1.0] * len(closes),
            "ema_20": closes,
            "ema_50": closes,
            "ema_200": closes,
        }
    )


def test_reset_returns_observation_of_correct_shape():
    env = SwarmTradingEnv(_df([100.0, 101.0, 102.0]))
    obs, info = env.reset()
    assert obs.shape == (6,)
    assert info == {}


def test_long_action_on_rising_price_gives_positive_reward():
    env = SwarmTradingEnv(_df([100.0, 110.0]))
    env.reset()
    _obs, reward, terminated, _truncated, _info = env.step(ACTION_LONG)
    assert reward == pytest.approx(0.10)
    assert not terminated


def test_short_action_on_rising_price_gives_negative_reward():
    env = SwarmTradingEnv(_df([100.0, 110.0]))
    env.reset()
    _, reward, _, _, _ = env.step(ACTION_SHORT)
    assert reward == pytest.approx(-0.10)


def test_flat_action_always_gives_zero_reward():
    env = SwarmTradingEnv(_df([100.0, 150.0]))
    env.reset()
    _, reward, _, _, _ = env.step(ACTION_FLAT)
    assert reward == 0.0


def test_truncates_at_end_of_data():
    # A 2-row df has exactly one row-to-row transition (index 0 → 1); the
    # env only knows it's out of data once it tries to step past the last row.
    env = SwarmTradingEnv(_df([100.0, 101.0]))
    env.reset()
    env.step(ACTION_FLAT)
    _, _, _, truncated, _ = env.step(ACTION_FLAT)
    assert truncated


def test_equity_can_be_wiped_out_and_terminates():
    env = SwarmTradingEnv(_df([100.0, 0.0, 1.0]), initial_equity_ratio=1.0)
    env.reset()
    _, _, terminated, _, _ = env.step(ACTION_LONG)  # -100% return
    assert terminated


def test_rejects_dataframe_missing_required_columns():
    with pytest.raises(ValueError):
        SwarmTradingEnv(pd.DataFrame({"close": [1.0, 2.0]}))


def test_rejects_dataframe_with_fewer_than_two_rows():
    with pytest.raises(ValueError):
        SwarmTradingEnv(_df([100.0]))
