"""Unit tests for HedgerAgent (swarm-imbalance hedging, no price signal)."""

from datetime import datetime

import pytest

from swarm_trading.agents.hedger.hedger_agent import HedgerAgent
from swarm_trading.core.models import Candle, MarketState, Side, Symbol


def _state(long_count=0, short_count=0, atr=2.0, close=1900.0, news_blackout=False):
    candle = Candle(
        symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), open=close, high=close, low=close, close=close, volume=1.0
    )
    return MarketState(
        symbol=Symbol.XAUUSD,
        timestamp=datetime.utcnow(),
        candles=[candle],
        indicators={"atr_14": atr, "swarm_long_count": long_count, "swarm_short_count": short_count},
        is_news_blackout=news_blackout,
    )


@pytest.mark.asyncio
async def test_no_signal_when_swarm_is_balanced():
    agent = HedgerAgent(symbol=Symbol.XAUUSD)
    proposal = await agent.analyze(_state(long_count=5, short_count=5))
    assert proposal is None


@pytest.mark.asyncio
async def test_shorts_when_swarm_is_too_long():
    agent = HedgerAgent(symbol=Symbol.XAUUSD)
    proposal = await agent.analyze(_state(long_count=8, short_count=1, atr=2.0, close=1900.0))
    assert proposal is not None
    assert proposal.side == Side.SHORT
    assert proposal.tp_price < 1900.0 < proposal.sl_price


@pytest.mark.asyncio
async def test_longs_when_swarm_is_too_short():
    agent = HedgerAgent(symbol=Symbol.XAUUSD)
    proposal = await agent.analyze(_state(long_count=0, short_count=9, atr=2.0, close=1900.0))
    assert proposal is not None
    assert proposal.side == Side.LONG
    assert proposal.sl_price < 1900.0 < proposal.tp_price


@pytest.mark.asyncio
async def test_threshold_is_exclusive_at_exactly_7():
    agent = HedgerAgent(symbol=Symbol.XAUUSD)
    proposal = await agent.analyze(_state(long_count=7, short_count=0))
    assert proposal is None  # spec says "> 7", not ">= 7"


@pytest.mark.asyncio
async def test_news_blackout_blocks_signal():
    agent = HedgerAgent(symbol=Symbol.XAUUSD)
    proposal = await agent.analyze(_state(long_count=10, short_count=0, news_blackout=True))
    assert proposal is None


@pytest.mark.asyncio
async def test_quantity_is_clamped_to_min_entry_pct():
    # HedgerAgent requests risk_pct=0.01, below the swarm-wide 3% floor,
    # so BaseAgent.calc_notional clamps it up to risk_min_entry_pct.
    agent = HedgerAgent(symbol=Symbol.XAUUSD, initial_capital=50.0)
    proposal = await agent.analyze(_state(long_count=8, short_count=0))
    assert proposal is not None
    assert proposal.quantity == pytest.approx(1.5)
