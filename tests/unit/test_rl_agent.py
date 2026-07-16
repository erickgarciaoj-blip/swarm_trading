"""
Unit tests for RLAgent's live-inference-only logic, with the Stable-Baselines3
model stubbed out — these never import stable_baselines3/gymnasium/torch,
matching how the agent itself only imports them lazily when a real model
must be built. RLAgent never trains itself in-process (see
docs/architecture/adr/0001-rl-inference-only-in-production.md); training is
a separate offline script (agents/rl/train.py), so there's nothing here
about retrain triggers — only load/hot-swap and inference.
"""

import os
from datetime import datetime

import pytest

from swarm_trading.agents.rl.rl_agent import ACTION_FLAT, ACTION_LONG, ACTION_SHORT, MIN_CANDLES_REQUIRED, RLAgent
from swarm_trading.core.models import (
    AgentStatus,
    Candle,
    ExecutedTrade,
    MarketState,
    OrderStatus,
    Side,
    Symbol,
)


class _FakeModel:
    def __init__(self, action: int):
        self._action = action
        self.saved_to: list[str] = []

    def predict(self, obs, deterministic=True):
        return self._action, None

    def save(self, path):
        self.saved_to.append(path)


def _agent(tmp_path, action=ACTION_LONG) -> RLAgent:
    agent = RLAgent(symbol=Symbol.XAUUSD, initial_capital=1.0, model_path=str(tmp_path))
    agent._model = _FakeModel(action)  # bypass _ensure_model()/stable_baselines3
    return agent


def _state(atr=2.0, close=1900.0, news_blackout=False, n_candles=MIN_CANDLES_REQUIRED):
    candle = Candle(
        symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), open=close, high=close, low=close, close=close, volume=1.0
    )
    return MarketState(
        symbol=Symbol.XAUUSD,
        timestamp=datetime.utcnow(),
        candles=[candle] * n_candles,
        indicators={"rsi_14": 50.0, "atr_14": atr, "ema_20": close, "ema_50": close, "ema_200": close},
        is_news_blackout=news_blackout,
    )


@pytest.mark.asyncio
async def test_flat_action_returns_no_proposal(tmp_path):
    agent = _agent(tmp_path, action=ACTION_FLAT)
    proposal = await agent.analyze(_state())
    assert proposal is None


@pytest.mark.asyncio
async def test_long_action_builds_long_proposal_with_atr_sl_tp(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    proposal = await agent.analyze(_state(atr=2.0, close=1900.0))
    assert proposal is not None
    assert proposal.side == Side.LONG
    assert proposal.sl_price < 1900.0 < proposal.tp_price


@pytest.mark.asyncio
async def test_short_action_builds_short_proposal_with_atr_sl_tp(tmp_path):
    agent = _agent(tmp_path, action=ACTION_SHORT)
    proposal = await agent.analyze(_state(atr=2.0, close=1900.0))
    assert proposal is not None
    assert proposal.side == Side.SHORT
    assert proposal.tp_price < 1900.0 < proposal.sl_price


@pytest.mark.asyncio
async def test_news_blackout_blocks_signal(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    proposal = await agent.analyze(_state(news_blackout=True))
    assert proposal is None


@pytest.mark.asyncio
async def test_cold_start_confidence_lower_than_pretrained(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    assert agent._is_pretrained is False
    proposal = await agent.analyze(_state())
    assert proposal is not None
    assert proposal.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_on_trade_closed_only_records_never_trains(tmp_path):
    """No retrain trigger exists anymore — on_trade_closed must do nothing
    but update equity/metrics, regardless of how many trades close."""
    agent = _agent(tmp_path, action=ACTION_LONG)
    assert not hasattr(agent, "_retrain")
    assert not hasattr(agent, "_train_and_save")

    trade = ExecutedTrade(
        trade_id="t1",
        agent_id=agent.agent_id,
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=1900.0,
        quantity=0.01,
        sl_price=1850.0,
        tp_price=1950.0,
        status=OrderStatus.FILLED,
        pnl=0.5,
    )

    for _ in range(5):
        await agent.on_trade_closed(trade)

    assert agent.get_metrics().total_trades == 5
    assert agent.status == AgentStatus.ACTIVE


@pytest.mark.asyncio
async def test_ensure_model_hot_swaps_on_file_change(tmp_path, monkeypatch):
    """Dropping a new checkpoint file (e.g. via scp) must be picked up on the
    next tick without restarting the process — no explicit reload call."""
    agent = RLAgent(symbol=Symbol.XAUUSD, initial_capital=1.0, model_path=str(tmp_path))
    agent._model = None  # undo the _agent() helper's bypass; exercise _ensure_model() for real

    loaded_paths: list[str] = []
    monkeypatch.setattr(agent, "_load_model", lambda path: loaded_paths.append(path) or _FakeModel(ACTION_LONG))

    model_path = tmp_path / "XAUUSD_ppo.zip"
    model_path.write_text("v1")
    v1_mtime = model_path.stat().st_mtime

    m1 = await agent._ensure_model()
    assert len(loaded_paths) == 1
    assert agent._is_pretrained is True

    # Same mtime, same file → no reload.
    m2 = await agent._ensure_model()
    assert len(loaded_paths) == 1
    assert m2 is m1

    # New content, mtime forced strictly forward → must hot-swap regardless
    # of filesystem mtime resolution.
    model_path.write_text("v2")
    os.utime(model_path, (v1_mtime + 1, v1_mtime + 1))

    await agent._ensure_model()
    assert len(loaded_paths) == 2


@pytest.mark.asyncio
async def test_ensure_model_falls_back_to_previous_on_corrupt_checkpoint(tmp_path, monkeypatch):
    """A checkpoint caught mid-write (e.g. a non-atomic scp) must not crash
    the agent or take it offline — it should keep trading on the last good
    model and retry the load on the next tick (see ADR-0003)."""
    agent = RLAgent(symbol=Symbol.XAUUSD, initial_capital=1.0, model_path=str(tmp_path))
    agent._model = None

    good_model = _FakeModel(ACTION_LONG)
    load_calls: list[str] = []

    def flaky_load(path):
        load_calls.append(path)
        if len(load_calls) == 2:
            raise ValueError("truncated zip — simulated mid-scp read")
        return good_model

    monkeypatch.setattr(agent, "_load_model", flaky_load)

    model_path = tmp_path / "XAUUSD_ppo.zip"
    model_path.write_text("v1")
    v1_mtime = model_path.stat().st_mtime

    m1 = await agent._ensure_model()
    assert m1 is good_model
    assert len(load_calls) == 1

    # Simulate a corrupted in-flight overwrite: mtime changes, load fails.
    model_path.write_text("corrupt-mid-write")
    os.utime(model_path, (v1_mtime + 1, v1_mtime + 1))

    m2 = await agent._ensure_model()
    assert m2 is good_model  # fell back, did not raise, did not go stale/None
    assert len(load_calls) == 2
    assert agent._model_mtime == v1_mtime  # mtime NOT advanced — retries next tick

    # Next tick, same broken file, still retried (not permanently given up).
    model_path.write_text("still v2, now valid")
    os.utime(model_path, (v1_mtime + 1, v1_mtime + 1))
    m3 = await agent._ensure_model()
    assert m3 is good_model
    assert len(load_calls) == 3
    assert agent._model_mtime == v1_mtime + 1  # succeeded this time, mtime advances


# ─── Inference input validation (ADR-0007) ──────────────────────────────────
#
# valid / NaN / infinite / insufficient-history / recovery — the exact five
# scenarios called out when this hardening was requested during Fase 2's
# closure review.


@pytest.mark.asyncio
async def test_valid_features_produce_a_signal(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    proposal = await agent.analyze(_state(atr=2.0, close=1900.0))
    assert proposal is not None
    assert agent.invalid_observation_count == 0
    assert agent.insufficient_history_count == 0


@pytest.mark.asyncio
async def test_nan_indicator_is_rejected_without_imputation(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    state = _state(atr=float("nan"), close=1900.0)

    proposal = await agent.analyze(state)

    assert proposal is None
    assert agent.invalid_observation_count == 1
    assert agent.insufficient_history_count == 0


@pytest.mark.asyncio
async def test_nan_in_an_ema_indicator_is_rejected(tmp_path):
    """NaN reaching an EMA (not just atr_14) must also be caught — this one
    only surfaces past gate 2 (atr/close) into gate 3 (the full vector)."""
    agent = _agent(tmp_path, action=ACTION_LONG)
    state = _state(atr=2.0, close=1900.0)
    state.indicators["ema_50"] = float("nan")

    proposal = await agent.analyze(state)

    assert proposal is None
    assert agent.invalid_observation_count == 1


@pytest.mark.asyncio
async def test_infinite_indicator_is_rejected(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    state = _state(atr=float("inf"), close=1900.0)

    proposal = await agent.analyze(state)

    assert proposal is None
    assert agent.invalid_observation_count == 1


@pytest.mark.asyncio
async def test_insufficient_history_is_rejected_before_touching_the_model(tmp_path):
    agent = _agent(tmp_path, action=ACTION_LONG)
    state = _state(n_candles=MIN_CANDLES_REQUIRED - 1)

    proposal = await agent.analyze(state)

    assert proposal is None
    assert agent.insufficient_history_count == 1
    assert agent.invalid_observation_count == 0  # rejected at the earlier, cheaper gate


@pytest.mark.asyncio
async def test_recovers_automatically_once_valid_data_returns(tmp_path):
    """No permanent trip: a bad tick must not disable future ticks — the
    very next call with valid data must produce a signal again."""
    agent = _agent(tmp_path, action=ACTION_LONG)

    bad = await agent.analyze(_state(atr=float("nan"), close=1900.0))
    assert bad is None
    assert agent.invalid_observation_count == 1

    good = await agent.analyze(_state(atr=2.0, close=1900.0))
    assert good is not None
    assert agent.invalid_observation_count == 1  # unchanged — this tick was clean
