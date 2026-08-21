"""
Fase 0 — open-position bookkeeping in SwarmOrchestrator.

Two gaps this covers, both of which made a restart lose live paper state:

1. A trade was only persisted when it CLOSED (on_trade_closed_callback), so
   an open position existed solely in the broker's in-memory registry.
2. RiskEngine.on_order_opened() fired for every broker response, FILLED or
   not, so a rejected order permanently consumed a SYMBOL_CONCENTRATION slot
   that nothing would ever release.

Plus the floating-PnL path, which depended on the offline broker reporting
its open positions at all (see test_ibkr_broker.py for the adapter side).
"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.brokers.ibkr.ibkr_broker import IBKRBroker
from swarm_trading.core.models import (
    AgentType,
    Candle,
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

    def __init__(self, quantity: float = 30.0, price: float = 1950.0):
        super().__init__(symbol=Symbol.XAUUSD, agent_type=AgentType.SCALPER, initial_capital=1000.0)
        self._quantity = quantity
        self._price = price

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        return OrderProposal(
            agent_id=self.agent_id,
            symbol=Symbol.XAUUSD,
            side=Side.LONG,
            quantity=self._quantity,
            sl_price=1900.0,
            tp_price=2000.0,
            confidence=0.9,
            price=self._price,
        )

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)


class _StubBroker:
    """Returns a trade with a caller-chosen status, so the orchestrator's
    FILLED-vs-REJECTED branching can be exercised without a real adapter."""

    def __init__(self, status: OrderStatus = OrderStatus.FILLED):
        self.status = status
        self.execute_calls = 0

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def execute(self, proposal: OrderProposal) -> ExecutedTrade:
        self.execute_calls += 1
        return ExecutedTrade(
            trade_id="trade-1",
            agent_id=proposal.agent_id,
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=1950.0 if self.status == OrderStatus.FILLED else 0.0,
            quantity=proposal.quantity,
            sl_price=proposal.sl_price,
            tp_price=proposal.tp_price,
            status=self.status,
        )

    async def get_open_positions(self):
        return []

    async def close_position(self, trade_id):
        raise NotImplementedError

    async def check_tp_sl(self, symbol, current_price):
        return []


class _RecordingRepository:
    """Captures save_trade() payloads. Deliberately records the ExecutedTrade
    values at call time (not the object), since the orchestrator may hand the
    same trade_id twice — open then close — and we assert on both states."""

    def __init__(self):
        self.saved: list[dict[str, Any]] = []

    async def save_trade(self, trade: ExecutedTrade) -> None:
        self.saved.append(
            {
                "trade_id": trade.trade_id,
                "status": trade.status,
                "pnl": trade.pnl,
                "closed_at": trade.closed_at,
                "quantity": trade.quantity,
            }
        )

    async def save_risk_state(self, snapshot) -> None:
        pass


def _state() -> MarketState:
    return MarketState(symbol=Symbol.XAUUSD, timestamp=datetime.utcnow(), candles=[], indicators={})


class _SlowRepository(_RecordingRepository):
    """save_trade() that only completes after several event-loop turns.

    A fire-and-forget write would still be sitting in the task queue when
    _process_agent() returns, so `saved` would be empty at that point. An
    awaited one cannot be — which is precisely the distinction under test."""

    TURNS = 5

    async def save_trade(self, trade: ExecutedTrade) -> None:
        for _ in range(self.TURNS):
            await asyncio.sleep(0)
        await super().save_trade(trade)


class _FailingRepository(_RecordingRepository):
    """Stands in for a repository whose write blows up despite
    AsyncRepository's own fail-soft try/except — e.g. a driver error raised
    outside it. The swarm must survive it."""

    async def save_trade(self, trade: ExecutedTrade) -> None:
        raise ConnectionError("postgres is down")


async def _drain_background_tasks(orch: SwarmOrchestrator) -> None:
    """The close-side write in on_trade_closed_callback() is still dispatched
    via _fire_and_forget, so it has not run yet when the callback returns.
    The OPEN-side write is awaited and needs no draining — see
    test_open_trade_persistence_is_awaited_before_process_agent_returns."""
    if orch._background_tasks:
        await asyncio.gather(*list(orch._background_tasks))


# ─── Persistence at open ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filled_trade_is_persisted_when_opened():
    repo = _RecordingRepository()
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None, repository=repo)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert len(repo.saved) == 1
    row = repo.saved[0]
    assert row["trade_id"] == "trade-1"
    assert row["status"] == OrderStatus.FILLED
    # An open row: no close timestamp, no realized result yet.
    assert row["closed_at"] is None
    assert row["pnl"] == 0.0


@pytest.mark.asyncio
async def test_open_trade_persistence_is_awaited_before_process_agent_returns():
    """The write must complete inside _process_agent(), not be deferred.

    The broker has already filled by this point, so a real position exists
    that nothing else records. A deferred write leaves a window — fill,
    position live, write still queued, process dies — in which that position
    is unrecoverable on restart.

    _SlowRepository needs several loop turns to finish, so a fire-and-forget
    dispatch would leave `saved` empty here.
    """
    repo = _SlowRepository()
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None, repository=repo)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    # No draining, no extra loop turns: the write is already durable.
    assert len(repo.saved) == 1
    # And nothing was queued for later.
    assert orch._background_tasks == set()


@pytest.mark.asyncio
async def test_open_trade_is_persisted_before_it_is_broadcast():
    """Ordering, not just completion: the row exists before any consumer is
    told the position opened."""
    repo = _RecordingRepository()
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None, repository=repo)
    saved_count_at_broadcast: list[int] = []

    async def _record(msg):
        saved_count_at_broadcast.append(len(repo.saved))

    orch.set_broadcaster(_record)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert saved_count_at_broadcast == [1]


@pytest.mark.asyncio
async def test_failing_open_persistence_does_not_take_down_the_swarm():
    """Fail-soft is preserved: a dead database must not propagate out of
    _process_agent(). AsyncRepository.save_trade() already swallows its own
    errors; this covers the orchestrator's behaviour if one escapes anyway."""
    orch = SwarmOrchestrator(
        broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None, repository=_FailingRepository()
    )
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())  # must not raise

    # The position is still counted — the broker really did fill it, and
    # losing the DB row must not also lose the risk-side bookkeeping.
    assert orch._risk._open_positions_by_symbol[Symbol.XAUUSD] == 1


@pytest.mark.asyncio
async def test_rejected_trade_is_not_persisted_as_open_position():
    repo = _RecordingRepository()
    orch = SwarmOrchestrator(
        broker=_StubBroker(OrderStatus.REJECTED), market_feed=None, news_feed=None, repository=repo
    )
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert repo.saved == []


@pytest.mark.asyncio
async def test_rejected_order_does_not_consume_a_concentration_slot():
    """A rejected order is not a position. Counting one leaks the slot
    forever: nothing closes a rejected trade, so on_trade_closed() never
    runs to decrement it and SYMBOL_CONCENTRATION drifts toward blocking the
    symbol outright."""
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.REJECTED), market_feed=None, news_feed=None)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert orch._risk._open_positions_by_symbol[Symbol.XAUUSD] == 0


@pytest.mark.asyncio
async def test_filled_order_does_consume_a_concentration_slot():
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert orch._risk._open_positions_by_symbol[Symbol.XAUUSD] == 1


@pytest.mark.asyncio
async def test_rejected_trade_is_not_broadcast_as_opened():
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.REJECTED), market_feed=None, news_feed=None)
    received: list[dict[str, Any]] = []

    async def _record(msg):
        received.append(msg)

    orch.set_broadcaster(_record)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())

    assert received == []


@pytest.mark.asyncio
async def test_closing_trade_updates_existing_trade_row():
    """Open and close must write the SAME trade_id — save_trade() upserts on
    it, so the open row is updated in place rather than duplicated."""
    repo = _RecordingRepository()
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None, repository=repo)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())
    await _drain_background_tasks(orch)

    closed = ExecutedTrade(
        trade_id="trade-1",  # same id the open write used
        agent_id=agent.agent_id,
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=1950.0,
        quantity=30.0,
        sl_price=1900.0,
        tp_price=2000.0,
        status=OrderStatus.FILLED,
        pnl=0.77,
        closed_at=datetime.utcnow(),
    )
    await orch.on_trade_closed_callback(closed)
    await _drain_background_tasks(orch)

    assert len(repo.saved) == 2
    assert [r["trade_id"] for r in repo.saved] == ["trade-1", "trade-1"]
    # First write is the open state, second carries the realized result.
    assert repo.saved[0]["closed_at"] is None and repo.saved[0]["pnl"] == 0.0
    assert repo.saved[1]["closed_at"] is not None and repo.saved[1]["pnl"] == 0.77


@pytest.mark.asyncio
async def test_open_persistence_is_skipped_without_a_repository():
    """ "DB is optional" stays true — no repository must not crash the tick."""
    orch = SwarmOrchestrator(broker=_StubBroker(OrderStatus.FILLED), market_feed=None, news_feed=None)
    agent = _FixedAgent()
    orch.register_agent(agent)

    await orch._process_agent(agent, _state())  # must not raise

    assert orch._background_tasks == set()


# ─── Floating PnL over offline open positions ─────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_floating_pnl_uses_offline_open_positions():
    """With the offline broker reporting its open trades, an unrealized move
    must produce non-zero floating PnL. Before Fase 0 this was pinned at 0.0
    because get_open_positions() returned [] in offline mode."""
    broker = IBKRBroker(offline=True)
    orch = SwarmOrchestrator(broker=broker, market_feed=None, news_feed=None)
    agent = _FixedAgent()
    orch.register_agent(agent)

    state = MarketState(
        symbol=Symbol.XAUUSD,
        timestamp=datetime.utcnow(),
        candles=[Candle(Symbol.XAUUSD, datetime.utcnow(), 1950.0, 1951.0, 1949.0, 1950.0, 1.0)],
        indicators={},
    )
    await orch._process_agent(agent, state)

    # Price moves +1% against the $30 notional entered at 1950.
    orch._last_price[Symbol.XAUUSD] = 1969.5
    floating = await orch._compute_floating_pnl()

    assert agent.agent_id in floating
    assert floating[agent.agent_id] == pytest.approx(30.0 * 0.01)
    assert floating[agent.agent_id] != 0.0


@pytest.mark.asyncio
async def test_floating_pnl_is_signed_by_side():
    """A LONG losing and a SHORT gaining on the same downward move."""
    broker = IBKRBroker(offline=True)
    orch = SwarmOrchestrator(broker=broker, market_feed=None, news_feed=None)

    for side, agent_id in ((Side.LONG, "long-agent"), (Side.SHORT, "short-agent")):
        await broker.execute(
            OrderProposal(
                agent_id=agent_id,
                symbol=Symbol.XAUUSD,
                side=side,
                quantity=30.0,
                sl_price=1.0 if side == Side.LONG else 9999.0,
                tp_price=9999.0 if side == Side.LONG else 1.0,
                confidence=0.9,
                price=1950.0,
            )
        )

    orch._last_price[Symbol.XAUUSD] = 1930.5  # -1%
    floating = await orch._compute_floating_pnl()

    assert floating["long-agent"] == pytest.approx(-0.3)
    assert floating["short-agent"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_total_equity_includes_floating_pnl_from_offline_positions():
    """The halt machinery reads _compute_total_equity(); it must now see
    unrealized moves, which is the whole point of surfacing open positions."""
    broker = IBKRBroker(offline=True)
    orch = SwarmOrchestrator(broker=broker, market_feed=None, news_feed=None)
    agent = _FixedAgent()
    orch.register_agent(agent)

    state = MarketState(
        symbol=Symbol.XAUUSD,
        timestamp=datetime.utcnow(),
        candles=[Candle(Symbol.XAUUSD, datetime.utcnow(), 1950.0, 1951.0, 1949.0, 1950.0, 1.0)],
        indicators={},
    )
    await orch._process_agent(agent, state)

    equity_flat = orch._compute_total_equity()
    orch._last_price[Symbol.XAUUSD] = 1930.5  # -1%
    orch._floating_pnl = await orch._compute_floating_pnl()
    equity_after_drawdown = orch._compute_total_equity()

    assert equity_flat == pytest.approx(1000.0)
    assert equity_after_drawdown == pytest.approx(1000.0 - 0.3)
