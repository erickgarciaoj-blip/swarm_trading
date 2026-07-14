"""Unit tests for SwingAgent (EMA fast/slow crossover + ADX trend filter)."""
import pytest
from datetime import datetime

from swarm_trading.agents.swing.swing_agent import SwingAgent
from swarm_trading.core.models import Candle, MarketState, Side, Symbol


def _state(ema_fast, ema_slow, adx, close, atr=1.0, news_blackout=False):
    candle = Candle(
        symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(),
        open=close, high=close, low=close, close=close, volume=1.0,
    )
    return MarketState(
        symbol=Symbol.XAUUSD,
        timestamp=datetime.utcnow(),
        candles=[candle],
        # adx_threshold defaults to 25 and gets a ±1.5 per-instance jitter,
        # so tests use ADX values well clear of that band on either side.
        indicators={"ema_20": ema_fast, "ema_50": ema_slow, "atr_14": atr, "adx_14": adx},
        is_news_blackout=news_blackout,
    )


@pytest.mark.asyncio
async def test_no_signal_when_adx_below_threshold():
    """EMA20 > EMA50 (a valid cross) but ADX says there's no real trend."""
    agent = SwingAgent(symbol=Symbol.XAUUSD)
    state = _state(ema_fast=105.0, ema_slow=100.0, adx=15.0, close=110.0)
    proposal = await agent.analyze(state)
    assert proposal is None


@pytest.mark.asyncio
async def test_long_signal_when_ema_cross_up_and_adx_above_threshold():
    agent = SwingAgent(symbol=Symbol.XAUUSD)
    state = _state(ema_fast=105.0, ema_slow=100.0, adx=40.0, close=110.0)
    proposal = await agent.analyze(state)
    assert proposal is not None
    assert proposal.side == Side.LONG
    assert proposal.sl_price < proposal.tp_price


@pytest.mark.asyncio
async def test_short_signal_when_ema_cross_down_and_adx_above_threshold():
    agent = SwingAgent(symbol=Symbol.XAUUSD)
    state = _state(ema_fast=95.0, ema_slow=100.0, adx=40.0, close=90.0)
    proposal = await agent.analyze(state)
    assert proposal is not None
    assert proposal.side == Side.SHORT
    assert proposal.sl_price > proposal.tp_price


@pytest.mark.asyncio
async def test_does_not_reenter_same_trend():
    agent = SwingAgent(symbol=Symbol.XAUUSD)
    state = _state(ema_fast=105.0, ema_slow=100.0, adx=40.0, close=110.0)
    first = await agent.analyze(state)
    second = await agent.analyze(state)
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_news_blackout_blocks_signal():
    agent = SwingAgent(symbol=Symbol.XAUUSD)
    state = _state(ema_fast=105.0, ema_slow=100.0, adx=40.0, close=110.0, news_blackout=True)
    proposal = await agent.analyze(state)
    assert proposal is None


@pytest.mark.asyncio
async def test_custom_params_are_used_instead_of_defaults():
    """SwarmFactory creates instances with slightly different params — make
    sure they're actually applied, not silently ignored."""
    agent = SwingAgent(
        symbol=Symbol.XAUUSD,
        ema_fast=10,
        ema_slow=30,
        adx_threshold=25.0,
        atr_sl_mult=1.0,
        atr_tp_mult=2.0,
    )
    assert agent.ema_fast == 10
    assert agent.ema_slow == 30
    assert agent.atr_sl_mult == 1.0
    assert agent.atr_tp_mult == 2.0
    # jittered by up to ±1.5 around the requested threshold
    assert 23.0 <= agent.adx_threshold <= 27.0
