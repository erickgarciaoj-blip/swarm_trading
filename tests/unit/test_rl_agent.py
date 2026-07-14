"""
Unit tests for RLAgent's live-inference and retrain-triggering logic, with
the Stable-Baselines3 model stubbed out — these never import
stable_baselines3/gymnasium/torch, matching how the agent itself only
imports them lazily when a real model must be built.
"""
from datetime import datetime
import pytest

from swarm_trading.agents.rl.rl_agent import ACTION_FLAT, ACTION_LONG, ACTION_SHORT, RLAgent
from swarm_trading.core.models import AgentStatus, Candle, ExecutedTrade, MarketState, OrderStatus, Side, Symbol


class _FakeModel:
    def __init__(self, action: int):
        self._action = action
        self.saved_to: list[str] = []

    def predict(self, obs, deterministic=True):
        return self._action, None

    def save(self, path):
        self.saved_to.append(path)


def _agent(tmp_path, action=ACTION_LONG, retrain_every=3) -> RLAgent:
    agent = RLAgent(
        symbol=Symbol.XAUUSD, initial_capital=1.0,
        retrain_every_n_trades=retrain_every, model_path=str(tmp_path),
    )
    agent._model = _FakeModel(action)  # bypass _ensure_model()/stable_baselines3
    return agent


def _state(atr=2.0, close=1900.0, news_blackout=False):
    candle = Candle(symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(),
                     open=close, high=close, low=close, close=close, volume=1.0)
    return MarketState(
        symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), candles=[candle],
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
    assert proposal.confidence == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_on_trade_closed_triggers_retrain_after_n_trades(tmp_path, monkeypatch):
    agent = _agent(tmp_path, action=ACTION_LONG, retrain_every=2)
    calls = []
    monkeypatch.setattr(agent, "_train_and_save", lambda: calls.append(1))

    trade = ExecutedTrade(
        trade_id="t1", agent_id=agent.agent_id, symbol=Symbol.XAUUSD, side=Side.LONG,
        entry_price=1900.0, quantity=0.01, sl_price=1850.0, tp_price=1950.0,
        status=OrderStatus.FILLED, pnl=0.5,
    )

    await agent.on_trade_closed(trade)
    assert len(calls) == 0  # only 1 trade so far, threshold is 2
    assert agent.status == AgentStatus.ACTIVE

    await agent.on_trade_closed(trade)
    assert len(calls) == 1  # threshold reached, retrain fired
    assert agent.status == AgentStatus.ACTIVE  # restored after retrain
    assert agent._trades_since_retrain == 0
