"""
ReplayBroker — exit resolution, intrabar policy, costs and lifecycle.

The bar geometry in these tests is explicit rather than generated: each
candle is built with the exact high/low needed to touch (or miss) a level,
so a failure points at the rule that broke rather than at a random walk that
happened to move.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from swarm_trading.backtest.broker import (
    BarTouch,
    IntrabarPolicy,
    ReplayBroker,
    levels_touched,
    resolve_exit_price,
)
from swarm_trading.core.costs import CostBook
from swarm_trading.core.models import Candle, ExecutedTrade, OrderProposal, OrderStatus, Side, Symbol

BASE_TIME = datetime(2026, 1, 1, 9, 30)
# Local builders rather than a shared import: a test module importing
# another test module is reachable under two module names (see this
# package's conftest docstring), which mypy rejects.


def candle(
    high: float,
    low: float,
    close: float | None = None,
    symbol: Symbol = Symbol.XAUUSD,
    minute: int = 0,
) -> Candle:
    mid = (high + low) / 2
    return Candle(
        symbol=symbol,
        timestamp=BASE_TIME + timedelta(minutes=minute),
        open=mid,
        high=high,
        low=low,
        close=close if close is not None else mid,
        volume=100.0,
    )


def proposal(
    side: Side = Side.LONG,
    price: float = 100.0,
    sl_price: float = 90.0,
    tp_price: float = 110.0,
    agent_id: str = "SCALPER_XAUUSD_0",
    symbol: Symbol = Symbol.XAUUSD,
) -> OrderProposal:
    return OrderProposal(
        agent_id=agent_id,
        symbol=symbol,
        side=side,
        quantity=1_000.0,
        sl_price=sl_price,
        tp_price=tp_price,
        confidence=0.9,
        price=price,
    )


# No module-level asyncio mark: pyproject sets asyncio_mode = "auto", so
# async tests are collected automatically and marking sync ones warns.

ENTRY, TP, SL = 100.0, 110.0, 90.0


async def _open(broker: ReplayBroker, side: Side = Side.LONG, bar: int = 0, **kwargs):
    broker.set_bar_index(bar)
    # For a SHORT the target is BELOW and the stop ABOVE — mirroring how
    # ScalperAgent/SwingAgent build their proposals.
    defaults = {"sl_price": SL, "tp_price": TP} if side == Side.LONG else {"sl_price": TP, "tp_price": SL}
    defaults.update(kwargs)
    return await broker.execute(proposal(side=side, price=ENTRY, **defaults))


# ─── Directional exits ────────────────────────────────────────────────────


async def test_long_closes_at_take_profit(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)

    assert len(closed) == 1
    trade = closed[0]
    assert trade.exit_price == TP
    assert trade.pnl > 0
    assert trade.gross_pnl == pytest.approx(1_000.0 * (TP - ENTRY) / ENTRY)
    assert trade.status == OrderStatus.FILLED
    assert trade.closed_at is not None


async def test_long_closes_at_stop_loss(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=101.0, low=89.0, minute=1), 1)

    assert len(closed) == 1
    assert closed[0].exit_price == SL
    assert closed[0].pnl < 0
    assert closed[0].gross_pnl == pytest.approx(1_000.0 * (SL - ENTRY) / ENTRY)


async def test_short_closes_at_take_profit(zero_costs):
    """A SHORT's target is below the entry — it profits on the low."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.SHORT)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=101.0, low=89.0, minute=1), 1)

    assert len(closed) == 1
    assert closed[0].exit_price == SL  # tp_price for a SHORT
    assert closed[0].pnl > 0


async def test_short_closes_at_stop_loss(zero_costs):
    """A SHORT's stop is above the entry — it loses on the high."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.SHORT)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)

    assert len(closed) == 1
    assert closed[0].exit_price == TP  # sl_price for a SHORT
    assert closed[0].pnl < 0


async def test_bar_that_touches_neither_level_leaves_the_position_open(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=105.0, low=95.0, minute=1), 1)

    assert closed == []
    assert broker.open_position_count == 1


async def test_high_low_are_used_not_the_close(zero_costs):
    """A level touched intrabar and then retraced still fills. Resolving on
    the close alone — all the live paper broker can do — would miss it."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    # High pierces TP, but the bar closes back at the entry.
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=112.0, low=99.0, close=100.0, minute=1), 1)

    assert len(closed) == 1
    assert closed[0].exit_price == TP


async def test_exit_fills_at_the_level_not_the_bar_close(zero_costs):
    """A resting order fills at its price, not wherever the bar ended."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=130.0, low=99.0, close=125.0, minute=1), 1)

    assert closed[0].exit_price == TP  # 110, not 125 and not 130


# ─── Intrabar ambiguity ───────────────────────────────────────────────────


AMBIGUOUS = {"high": 115.0, "low": 85.0}  # covers both TP (110) and SL (90)


async def test_conservative_policy_takes_the_worst_outcome_for_a_long(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs, intrabar_policy=IntrabarPolicy.CONSERVATIVE)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(**AMBIGUOUS, minute=1), 1)

    assert closed[0].exit_price == SL
    assert closed[0].pnl < 0


async def test_conservative_policy_takes_the_worst_outcome_for_a_short(zero_costs):
    """Worst for a SHORT is its stop, which sits ABOVE the entry — so the
    conservative branch must not be hard-coded to "the lower price"."""
    broker = ReplayBroker(cost_book=zero_costs, intrabar_policy=IntrabarPolicy.CONSERVATIVE)
    await _open(broker, Side.SHORT)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(**AMBIGUOUS, minute=1), 1)

    assert closed[0].exit_price == TP  # 110 == the SHORT's sl_price
    assert closed[0].pnl < 0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_optimistic_policy_takes_the_target(zero_costs, side):
    broker = ReplayBroker(cost_book=zero_costs, intrabar_policy=IntrabarPolicy.OPTIMISTIC)
    await _open(broker, side)

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(**AMBIGUOUS, minute=1), 1)

    assert closed[0].pnl > 0


async def test_the_two_policies_bracket_the_result(zero_costs):
    """The gap between them measures how much the answer depends on an
    assumption the data cannot settle."""
    results = {}
    for policy in (IntrabarPolicy.CONSERVATIVE, IntrabarPolicy.OPTIMISTIC):
        broker = ReplayBroker(cost_book=zero_costs, intrabar_policy=policy)
        await _open(broker, Side.LONG)
        broker.set_bar_index(1)
        results[policy] = broker.resolve_bar(Symbol.XAUUSD, candle(**AMBIGUOUS, minute=1), 1)[0].pnl

    assert results[IntrabarPolicy.CONSERVATIVE] < results[IntrabarPolicy.OPTIMISTIC]


async def test_default_policy_is_conservative():
    assert ReplayBroker().intrabar_policy == IntrabarPolicy.CONSERVATIVE


def test_levels_touched_reports_both_when_the_bar_spans_them():
    trade = ExecutedTrade(
        trade_id="t",
        agent_id="a",
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=ENTRY,
        quantity=1_000.0,
        sl_price=SL,
        tp_price=TP,
        status=OrderStatus.FILLED,
    )
    assert levels_touched(trade, candle(**AMBIGUOUS)) == BarTouch(hit_tp=True, hit_sl=True)
    assert levels_touched(trade, candle(high=105.0, low=95.0)).ambiguous is False
    assert resolve_exit_price(trade, candle(high=105.0, low=95.0), IntrabarPolicy.CONSERVATIVE) is None


# ─── Costs ────────────────────────────────────────────────────────────────


async def test_zero_costs_reproduce_the_legacy_pnl_exactly(zero_costs):
    """Bit-for-bit against the pre-cost formula, so a costed replay stays
    comparable with history recorded before the cost model existed."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    trade = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)[0]

    legacy = 1_000.0 * ((TP - ENTRY) / ENTRY) * 1
    assert trade.pnl == legacy
    assert trade.gross_pnl == trade.pnl
    assert trade.total_costs == 0.0


async def test_non_zero_costs_reduce_net_below_gross(flat_costs):
    broker = ReplayBroker(cost_book=flat_costs)
    await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    trade = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)[0]

    assert trade.total_costs > 0
    assert trade.pnl < trade.gross_pnl
    assert trade.pnl == trade.gross_pnl - trade.total_costs
    assert trade.entry_costs > 0 and trade.exit_costs > 0
    assert trade.commission > 0


async def test_entry_fill_is_adverse_on_open(flat_costs):
    broker = ReplayBroker(cost_book=flat_costs)
    long_trade = await _open(broker, Side.LONG, bar=0)
    short_trade = await _open(broker, Side.SHORT, bar=0)

    assert long_trade.entry_fill_price > ENTRY  # bought the ask
    assert short_trade.entry_fill_price < ENTRY  # sold the bid
    assert long_trade.entry_costs > 0


async def test_costs_are_per_symbol(zero_costs):
    """Two symbols, same geometry, different drag."""
    from swarm_trading.core.costs import InstrumentCosts

    book = CostBook({"XAUUSD": InstrumentCosts(spread_bps=40.0), "OIL": InstrumentCosts(spread_bps=2.0)})
    broker = ReplayBroker(cost_book=book)

    broker.set_bar_index(0)
    await broker.execute(proposal(price=ENTRY, sl_price=SL, tp_price=TP, symbol=Symbol.XAUUSD))
    await broker.execute(proposal(price=ENTRY, sl_price=SL, tp_price=TP, symbol=Symbol.OIL, agent_id="SCALPER_OIL_0"))

    broker.set_bar_index(1)
    gold = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)[0]
    oil = broker.resolve_bar(Symbol.OIL, candle(high=111.0, low=99.0, symbol=Symbol.OIL, minute=1), 1)[0]

    assert gold.gross_pnl == pytest.approx(oil.gross_pnl)
    assert gold.total_costs > oil.total_costs
    assert gold.pnl < oil.pnl


# ─── Lifecycle invariants ─────────────────────────────────────────────────


async def test_a_trade_cannot_close_on_the_bar_it_opened(zero_costs):
    """The signal came from this bar's close, so the position exists only
    from the next bar. Resolving it here would be look-ahead."""
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG, bar=5)

    closed = broker.resolve_bar(Symbol.XAUUSD, candle(**AMBIGUOUS, minute=5), 5)

    assert closed == []
    assert broker.open_position_count == 1


async def test_no_double_close(zero_costs):
    """Resolving the same bar twice must not close a position twice."""
    broker = ReplayBroker(cost_book=zero_costs)
    trade = await _open(broker, Side.LONG)

    broker.set_bar_index(1)
    first = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)
    second = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)

    assert len(first) == 1
    assert second == []
    assert broker.open_position_count == 0
    assert broker.was_closed(trade.trade_id)
    assert len(broker.closed_trades) == 1


async def test_no_phantom_positions(zero_costs):
    """Everything opened is either still open or exactly once closed."""
    broker = ReplayBroker(cost_book=zero_costs)
    opened = [await _open(broker, Side.LONG, bar=0) for _ in range(3)]

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)

    assert len(closed) == 3
    assert broker.open_position_count == 0
    assert {t.trade_id for t in closed} == {t.trade_id for t in opened}
    assert len({t.trade_id for t in closed}) == 3  # no duplicates


async def test_only_the_requested_symbol_is_resolved(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs)
    broker.set_bar_index(0)
    await broker.execute(proposal(price=ENTRY, sl_price=SL, tp_price=TP, symbol=Symbol.XAUUSD))
    await broker.execute(proposal(price=ENTRY, sl_price=SL, tp_price=TP, symbol=Symbol.OIL, agent_id="SCALPER_OIL_0"))

    broker.set_bar_index(1)
    closed = broker.resolve_bar(Symbol.XAUUSD, candle(high=111.0, low=99.0, minute=1), 1)

    assert len(closed) == 1
    assert closed[0].symbol == Symbol.XAUUSD
    assert broker.open_position_count == 1


async def test_replay_clock_cannot_move_backwards():
    broker = ReplayBroker()
    broker.set_bar_index(10)
    with pytest.raises(ValueError, match="backwards"):
        broker.set_bar_index(9)


async def test_trade_ids_are_deterministic(zero_costs):
    """uuid4 would make identical runs produce different ids, breaking
    byte-for-byte reproducibility of the serialised output."""
    ids = []
    for _ in range(2):
        broker = ReplayBroker(cost_book=zero_costs)
        broker.set_bar_index(3)
        ids.append((await broker.execute(proposal())).trade_id)

    assert ids[0] == ids[1]
    assert "SCALPER_XAUUSD_0" in ids[0]


async def test_open_positions_are_defensive_copies(zero_costs):
    broker = ReplayBroker(cost_book=zero_costs)
    await _open(broker, Side.LONG)

    positions = await broker.get_open_positions()
    positions[0].pnl = 999.0

    assert broker.open_position_count == 1
    assert (await broker.get_open_positions())[0].pnl == 0.0


async def test_check_tp_sl_refuses_rather_than_degrading():
    """BrokerInterface's close-only exit path would silently discard the
    high/low a replay depends on, so it raises instead."""
    broker = ReplayBroker()
    with pytest.raises(NotImplementedError, match="resolve_bar"):
        await broker.check_tp_sl(Symbol.XAUUSD, 100.0)


async def test_close_position_by_id_is_not_the_replay_path():
    broker = ReplayBroker()
    with pytest.raises(NotImplementedError, match="resolve_bar"):
        await broker.close_position("whatever")


def test_cost_fixture_is_not_accidentally_zero(test_costs):
    """Guards the fixture itself: zeroed rates would make several assertions
    above pass vacuously."""
    assert test_costs.adverse_price_offset_bps > 0
    assert test_costs.commission_bps > 0
