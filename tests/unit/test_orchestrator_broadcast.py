"""Unit tests for SwarmOrchestrator's WebSocket broadcaster wiring."""

import asyncio
import contextlib
from datetime import datetime

import pytest

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.core.models import (
    AgentType,
    ExecutedTrade,
    MarketState,
    OrderProposal,
    OrderStatus,
    Side,
    Symbol,
)
from swarm_trading.core.orchestrator.orchestrator import SwarmOrchestrator


class _FixedAgent(BaseAgent):
    """Always proposes the same LONG order — just enough to drive _process_agent."""

    def __init__(self):
        super().__init__(symbol=Symbol.XAUUSD, agent_type=AgentType.SCALPER, initial_capital=1.0)

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        return OrderProposal(
            agent_id=self.agent_id,
            symbol=Symbol.XAUUSD,
            side=Side.LONG,
            quantity=0.01,
            sl_price=1800.0,
            tp_price=1900.0,
            confidence=0.9,
        )

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)


class _FakeBroker:
    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def execute(self, proposal: OrderProposal) -> ExecutedTrade:
        return ExecutedTrade(
            trade_id="t1",
            agent_id=proposal.agent_id,
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=1850.0,
            quantity=proposal.quantity,
            sl_price=proposal.sl_price,
            tp_price=proposal.tp_price,
            status=OrderStatus.FILLED,
        )

    async def get_open_positions(self):
        return []

    async def close_position(self, trade_id):
        raise NotImplementedError


def _orchestrator():
    return SwarmOrchestrator(broker=_FakeBroker(), market_feed=None, news_feed=None)


def _state():
    return MarketState(symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), candles=[], indicators={})


@pytest.mark.asyncio
async def test_process_agent_broadcasts_trade_opened():
    orch = _orchestrator()
    received = []
    orch.set_broadcaster(lambda msg: received.append(msg) or _async_noop())

    agent = _FixedAgent()
    orch.register_agent(agent)
    await orch._process_agent(agent, _state())

    assert len(received) == 1
    assert received[0]["type"] == "trade_opened"
    assert received[0]["data"]["agent_id"] == agent.agent_id
    assert received[0]["data"]["side"] == "LONG"
    assert received[0]["data"]["entry_price"] == 1850.0


@pytest.mark.asyncio
async def test_on_trade_closed_callback_broadcasts_trade_closed():
    orch = _orchestrator()
    received = []
    orch.set_broadcaster(lambda msg: received.append(msg) or _async_noop())

    agent = _FixedAgent()
    orch.register_agent(agent)

    trade = ExecutedTrade(
        trade_id="t1",
        agent_id=agent.agent_id,
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=1850.0,
        quantity=0.01,
        sl_price=1800.0,
        tp_price=1900.0,
        status=OrderStatus.FILLED,
        pnl=1.25,
        closed_at=datetime.utcnow(),
    )
    await orch.on_trade_closed_callback(trade)

    assert len(received) == 1
    assert received[0]["type"] == "trade_closed"
    assert received[0]["data"]["pnl"] == 1.25
    assert received[0]["data"]["closed_at"] is not None


@pytest.mark.asyncio
async def test_default_broadcaster_is_a_noop_when_unset():
    orch = _orchestrator()  # set_broadcaster() never called
    agent = _FixedAgent()
    orch.register_agent(agent)
    await orch._process_agent(agent, _state())  # must not raise


async def _async_noop():
    pass


class _FlakyMarketFeed:
    """Raises for a chosen set of symbols, like yfinance genuinely does when
    a futures ticker (GC=F, CL=F) has no intraday bars available right now —
    real, observed, unrelated-to-any-code-change behavior, not a mock of a
    hypothetical failure."""

    def __init__(self, broken_symbols):
        self._broken = set(broken_symbols)
        self.calls: list[Symbol] = []

    async def get_state(self, symbol, period="1d", interval="1m"):
        self.calls.append(symbol)
        if symbol in self._broken:
            raise ValueError(f"No data for {symbol.value}")
        return MarketState(symbol=symbol, timestamp=datetime.utcnow(), candles=[], indicators={})


class _NoopNewsFeed:
    async def get_upcoming(self, symbol, horizon_hours=2):
        return []


@pytest.mark.asyncio
async def test_one_broken_symbol_does_not_block_the_others_in_the_same_tick():
    """Regression test: before this fix, one symbol's get_state() exception
    propagated out of the `for symbol in Symbol` loop into run()'s outer
    except, which logged, slept, and restarted the tick from Symbol's first
    member every time — so the swarm never got past the broken symbol to
    process any of the other four, ever. Observed live against a real
    yfinance outage on GC=F during Fase 2 verification."""
    feed = _FlakyMarketFeed(broken_symbols={Symbol.XAUUSD})
    orch = SwarmOrchestrator(broker=_FakeBroker(), market_feed=feed, news_feed=_NoopNewsFeed())

    task = asyncio.ensure_future(orch.run())
    await asyncio.sleep(0.05)  # let one tick's worth of symbol iteration happen
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert set(feed.calls) == set(Symbol)
