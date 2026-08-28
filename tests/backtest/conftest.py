"""
Pytest fixtures for the replay-engine tests.

Self-contained on purpose: this module imports nothing from the test tree.
A module under tests/ that other test modules import is reachable both as
`tests.backtest.x` and as `swarm_trading.tests.backtest.x`, because this
repo imports itself as `swarm_trading.*` — and mypy rejects the same file
appearing under two module names. Shared builders therefore live either
here (as fixtures, which pytest injects without an import) or in the
`backtest` package itself, which has one canonical path.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from swarm_trading.core.costs import CostBook, InstrumentCosts
from swarm_trading.core.models import (
    Candle,
    ExecutedTrade,
    OrderProposal,
    OrderStatus,
    Side,
    Symbol,
)

# Round and large enough that every cost effect is visible in an assertion
# rather than lost in the fifth decimal. Not a claim about any instrument.
TEST_COSTS = InstrumentCosts(spread_bps=10.0, slippage_bps=5.0, commission_bps=2.0)

BASE_TIME = datetime(2026, 1, 1, 9, 30)


@pytest.fixture
def test_costs() -> InstrumentCosts:
    return TEST_COSTS


@pytest.fixture
def zero_costs() -> CostBook:
    return CostBook.zero()


@pytest.fixture
def flat_costs() -> CostBook:
    """Same non-zero rates for every symbol, so per-symbol differences never
    confound a test that is about something else."""
    return CostBook(costs={}, default=TEST_COSTS)


@pytest.fixture
def make_candle():
    """Builds a bar with an exact high/low, so a test can place a level
    precisely inside or outside the range."""

    def _make(
        high: float,
        low: float,
        close: float | None = None,
        open_: float | None = None,
        symbol: Symbol = Symbol.XAUUSD,
        minute: int = 0,
    ) -> Candle:
        mid = (high + low) / 2
        return Candle(
            symbol=symbol,
            timestamp=BASE_TIME + timedelta(minutes=minute),
            open=open_ if open_ is not None else mid,
            high=high,
            low=low,
            close=close if close is not None else mid,
            volume=100.0,
        )

    return _make


@pytest.fixture
def make_proposal():
    def _make(
        side: Side = Side.LONG,
        price: float = 100.0,
        sl_price: float = 90.0,
        tp_price: float = 110.0,
        quantity: float = 1_000.0,
        agent_id: str = "SCALPER_XAUUSD_0",
        symbol: Symbol = Symbol.XAUUSD,
    ) -> OrderProposal:
        return OrderProposal(
            agent_id=agent_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            sl_price=sl_price,
            tp_price=tp_price,
            confidence=0.9,
            price=price,
        )

    return _make


@pytest.fixture
def make_closed_trade():
    """A synthetic closed trade for statistics tests, where only the PnL
    matters and the price path does not."""

    def _make(
        net_pnl: float,
        agent_id: str = "SCALPER_XAUUSD_0",
        symbol: Symbol = Symbol.XAUUSD,
        gross_pnl: float | None = None,
        minute: int = 0,
    ) -> ExecutedTrade:
        gross = net_pnl if gross_pnl is None else gross_pnl
        costs = gross - net_pnl
        return ExecutedTrade(
            trade_id=f"{agent_id}#{minute}",
            agent_id=agent_id,
            symbol=symbol,
            side=Side.LONG,
            entry_price=100.0,
            quantity=1_000.0,
            sl_price=90.0,
            tp_price=110.0,
            status=OrderStatus.FILLED,
            pnl=net_pnl,
            gross_pnl=gross,
            entry_costs=costs / 2,
            exit_costs=costs / 2,
            closed_at=BASE_TIME + timedelta(minutes=minute),
        )

    return _make
