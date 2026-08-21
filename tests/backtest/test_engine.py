"""
Backtester — the replay loop end to end.

These run the REAL ScalperAgent and SwingAgent against synthetic bars. They
assert structural properties (determinism, ordering, cost accounting), never
a specific PnL figure: the numbers depend on strategy parameters this phase
is explicitly not allowed to change, and pinning them would turn any future
strategy work into a test-editing exercise.
"""

from __future__ import annotations

import json

import pytest

from swarm_trading.backtest.broker import IntrabarPolicy
from swarm_trading.backtest.data import load_dataframe, synthetic_ohlcv
from swarm_trading.backtest.engine import BacktestConfig, Backtester, build_agents
from swarm_trading.backtest.metrics import strategy_of
from swarm_trading.backtest.report import to_json, trades_to_csv
from swarm_trading.core.costs import CostBook
from swarm_trading.core.models import Symbol

# Enough bars past the 200-bar warm-up to generate trades, small enough to
# keep the suite fast.
BARS = 700


def _series(seed: int = 7, bars: int = BARS, symbol: Symbol = Symbol.XAUUSD, start: float = 100.0):
    return {symbol: load_dataframe(symbol, synthetic_ohlcv(bars, seed=seed, start=start))}


def _config(**overrides) -> BacktestConfig:
    defaults: dict[str, object] = {
        "seed": 42,
        "cost_book": CostBook.zero(),
        "scalpers_per_symbol": 2,
        "swing_per_symbol": 2,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)


# ─── Determinism ──────────────────────────────────────────────────────────


async def test_same_inputs_and_seed_produce_an_identical_result():
    """The reproducibility invariant, asserted on the serialised output —
    so it covers trade ids and ordering, not just the aggregate numbers."""
    first = await Backtester(_config()).run(_series())
    second = await Backtester(_config()).run(_series())

    assert to_json(first) == to_json(second)
    assert trades_to_csv(first) == trades_to_csv(second)


async def test_a_different_seed_changes_the_agents():
    """The seed feeds the strategies' randomised thresholds, so it must
    actually reach them — otherwise 'seeded' would be a no-op."""
    agents_a = build_agents(_config(seed=1), [Symbol.XAUUSD])
    agents_b = build_agents(_config(seed=2), [Symbol.XAUUSD])

    thresholds_a = [a.rsi_oversold for a in agents_a if hasattr(a, "rsi_oversold")]
    thresholds_b = [a.rsi_oversold for a in agents_b if hasattr(a, "rsi_oversold")]

    assert thresholds_a != thresholds_b


async def test_the_same_seed_rebuilds_identical_agents():
    a = build_agents(_config(seed=99), [Symbol.XAUUSD])
    b = build_agents(_config(seed=99), [Symbol.XAUUSD])

    assert [x.agent_id for x in a] == [x.agent_id for x in b]
    assert [getattr(x, "rsi_oversold", None) for x in a] == [getattr(x, "rsi_oversold", None) for x in b]


def test_seeding_does_not_leak_into_the_callers_random_state():
    """build_agents() seeds the global `random` module, so it must restore
    what it found — otherwise running a backtest would silently make the
    rest of the process deterministic."""
    import random

    random.seed(12345)
    expected = [random.random() for _ in range(3)]

    random.seed(12345)
    build_agents(_config(seed=777), [Symbol.XAUUSD])
    after = [random.random() for _ in range(3)]

    assert after == expected


# ─── Agents built are the real ones ───────────────────────────────────────


def test_engine_builds_the_real_agent_classes_with_factory_parameters():
    from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
    from swarm_trading.agents.swing.swing_agent import SwingAgent

    agents = build_agents(BacktestConfig(seed=42), [Symbol.XAUUSD])

    scalpers = [a for a in agents if isinstance(a, ScalperAgent)]
    swings = [a for a in agents if isinstance(a, SwingAgent)]

    assert len(scalpers) == 6 and len(swings) == 6
    # SwarmFactory's exact parameter ladder.
    assert [round(s.atr_sl_multiplier, 4) for s in scalpers] == [1.5, 1.6, 1.7, 1.8, 1.9, 2.0]
    assert [round(s.atr_tp_multiplier, 4) for s in scalpers] == [3.0, 3.2, 3.4, 3.6, 3.8, 4.0]
    assert [round(s.atr_sl_mult, 4) for s in swings] == [2.0, 2.1, 2.2, 2.3, 2.4, 2.5]


def test_only_scalper_and_swing_are_replayed():
    """News, Hedger and RL are excluded on purpose in this phase."""
    agents = build_agents(BacktestConfig(), list(Symbol))

    assert {strategy_of(a.agent_id) for a in agents} == {"SCALPER", "SWING"}


# ─── Replay mechanics ─────────────────────────────────────────────────────


async def test_a_replay_produces_trades_and_a_coherent_result():
    result = await Backtester(_config()).run(_series())

    assert result.total.trades > 0
    assert len(result.trades) == result.total.trades
    assert len(result.equity_curve) == result.total.trades
    assert result.config["intrabar_policy"] == "conservative"
    assert result.config["seed"] == 42


async def test_every_trade_closes_after_it_opens():
    result = await Backtester(_config()).run(_series())

    for trade in result.trades:
        assert trade.closed_at is not None
        assert trade.closed_at > trade.opened_at, trade.trade_id


async def test_trades_are_returned_in_chronological_close_order():
    """The equity curve and drawdown depend on this ordering."""
    result = await Backtester(_config()).run(_series())

    # closed_at is Optional on the model but never None on a closed trade —
    # asserted first so the sort below has a concrete type.
    assert all(t.closed_at is not None for t in result.trades)
    closes = [t.closed_at for t in result.trades if t.closed_at is not None]
    assert closes == sorted(closes)


async def test_no_trade_is_closed_twice():
    result = await Backtester(_config()).run(_series())

    ids = [t.trade_id for t in result.trades]
    assert len(ids) == len(set(ids))


async def test_warmup_bars_are_not_traded():
    """Before EMA-200 has converged the indicators are still moving toward
    their true values; trading there would measure the warm-up."""
    config = _config(warmup_bars=400)
    series = _series()
    result = await Backtester(config).run(series)

    first_tradeable = series[Symbol.XAUUSD].candle(400).timestamp
    for trade in result.trades:
        assert trade.opened_at >= first_tradeable


# ─── Cost integration ─────────────────────────────────────────────────────


async def test_zero_costs_leave_gross_equal_to_net():
    result = await Backtester(_config(cost_book=CostBook.zero())).run(_series())

    assert result.total.total_costs == 0.0
    assert result.total.net_pnl == pytest.approx(result.total.gross_pnl)
    for trade in result.trades:
        assert trade.pnl == trade.gross_pnl


async def test_costs_reduce_net_pnl():
    """Same bars, same seed, same signals — only the cost book differs.

    Note what is NOT asserted: that gross PnL is identical between the two
    runs. It is not, and that is correct rather than a leak. calc_notional()
    sizes from the agent's CURRENT equity, and equity moves by NET PnL, so a
    costed run carries slightly smaller positions into later trades and
    therefore books a slightly different gross. Costs and position sizing
    are coupled through equity; the runs share signals, not sizes.
    """
    from swarm_trading.core.costs import InstrumentCosts

    free = await Backtester(_config(cost_book=CostBook.zero())).run(_series())
    costed = await Backtester(
        _config(cost_book=CostBook(costs={}, default=InstrumentCosts(spread_bps=10.0, commission_bps=2.0)))
    ).run(_series())

    # Signals are cost-independent, so the same trades are taken.
    assert costed.total.trades == free.total.trades
    assert [t.side for t in costed.trades] == [t.side for t in free.trades]
    assert [t.entry_price for t in costed.trades] == [t.entry_price for t in free.trades]

    assert free.total.total_costs == 0.0
    assert costed.total.total_costs > 0
    assert costed.total.net_pnl < free.total.net_pnl


async def test_the_first_trade_has_identical_gross_regardless_of_costs():
    """Isolates the equity coupling: before any trade has closed, both runs
    size from the same starting equity, so the first trade's gross must
    match exactly. Divergence only appears afterwards."""
    from swarm_trading.core.costs import InstrumentCosts

    free = await Backtester(_config(cost_book=CostBook.zero())).run(_series())
    costed = await Backtester(
        _config(cost_book=CostBook(costs={}, default=InstrumentCosts(spread_bps=10.0, commission_bps=2.0)))
    ).run(_series())

    assert costed.trades[0].gross_pnl == pytest.approx(free.trades[0].gross_pnl)
    assert costed.trades[0].pnl < free.trades[0].pnl


async def test_the_gross_minus_costs_identity_holds_across_the_whole_run():
    from swarm_trading.core.costs import InstrumentCosts

    result = await Backtester(
        _config(cost_book=CostBook(costs={}, default=InstrumentCosts(spread_bps=8.0, commission_bps=1.0)))
    ).run(_series())

    assert result.total.net_pnl == pytest.approx(result.total.gross_pnl - result.total.total_costs)
    for trade in result.trades:
        assert trade.pnl == pytest.approx(trade.gross_pnl - trade.total_costs)


# ─── Intrabar policy at engine level ──────────────────────────────────────


async def test_conservative_never_beats_optimistic_over_a_whole_run():
    """The two policies bound the true result; conservative is the lower
    bound by construction."""
    conservative = await Backtester(_config(intrabar_policy=IntrabarPolicy.CONSERVATIVE)).run(_series())
    optimistic = await Backtester(_config(intrabar_policy=IntrabarPolicy.OPTIMISTIC)).run(_series())

    assert conservative.total.net_pnl <= optimistic.total.net_pnl


async def test_the_policy_used_is_recorded_in_the_result():
    result = await Backtester(_config(intrabar_policy=IntrabarPolicy.OPTIMISTIC)).run(_series())

    assert result.config["intrabar_policy"] == "optimistic"
    assert json.loads(to_json(result))["config"]["intrabar_policy"] == "optimistic"


# ─── Multi-symbol ─────────────────────────────────────────────────────────


async def test_multiple_symbols_are_grouped_independently():
    series = {
        Symbol.XAUUSD: load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(BARS, seed=7, start=100.0)),
        Symbol.OIL: load_dataframe(Symbol.OIL, synthetic_ohlcv(BARS, seed=11, start=86.0)),
    }

    result = await Backtester(_config()).run(series)

    assert set(result.by_symbol) <= {"XAUUSD", "OIL"}
    assert sum(s.trades for s in result.by_symbol.values()) == result.total.trades
    for trade in result.trades:
        assert trade.symbol.value in trade.agent_id


async def test_an_empty_replay_is_rejected():
    with pytest.raises(ValueError, match="no series"):
        await Backtester(_config()).run({})


async def test_a_series_shorter_than_the_warmup_produces_no_trades():
    result = await Backtester(_config(warmup_bars=BARS + 100)).run(_series())

    assert result.total.trades == 0
    assert result.total.net_pnl == 0.0
