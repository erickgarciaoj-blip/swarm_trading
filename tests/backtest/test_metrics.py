"""
Backtest statistics.

Every case uses hand-built trades with round PnL figures, so the expected
value is arithmetic a reader can verify in their head rather than something
only the implementation can produce.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from swarm_trading.backtest.metrics import (
    build_result,
    compute_stats,
    max_drawdown_from_equity,
    strategy_of,
)
from swarm_trading.backtest.report import render, to_json, trades_to_csv
from swarm_trading.core.models import ExecutedTrade, OrderStatus, Side, Symbol

BASE_TIME = datetime(2026, 1, 1, 9, 30)


def closed_trade(
    net_pnl: float,
    agent_id: str = "SCALPER_XAUUSD_0",
    symbol: Symbol = Symbol.XAUUSD,
    gross_pnl: float | None = None,
    minute: int = 0,
) -> ExecutedTrade:
    """Local builder rather than a shared import: a test module that imports
    another test module is reachable under two module names (see this
    package's conftest docstring), which mypy rejects."""
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


def _trades(*net_pnls: float) -> list[ExecutedTrade]:
    return [closed_trade(pnl, minute=i) for i, pnl in enumerate(net_pnls)]


# ─── Core aggregates ──────────────────────────────────────────────────────


def test_empty_input_produces_zeroed_stats_not_a_crash():
    stats = compute_stats([])

    assert stats.trades == 0
    assert stats.net_pnl == 0.0
    assert stats.win_rate is None  # undefined, not 0.0
    assert stats.profit_factor is None


def test_gross_costs_and_net_are_reported_separately():
    trades = [closed_trade(net_pnl=8.0, gross_pnl=10.0), closed_trade(net_pnl=-6.0, gross_pnl=-4.0)]

    stats = compute_stats(trades)

    assert stats.gross_pnl == pytest.approx(6.0)
    assert stats.total_costs == pytest.approx(4.0)
    assert stats.net_pnl == pytest.approx(2.0)
    assert stats.net_pnl == pytest.approx(stats.gross_pnl - stats.total_costs)


def test_win_rate_and_counts():
    stats = compute_stats(_trades(1.0, 2.0, -1.0, -1.0, 0.0))

    assert stats.trades == 5
    assert stats.wins == 2
    assert stats.losses == 2
    assert stats.scratches == 1  # exactly zero is neither
    assert stats.win_rate == pytest.approx(2 / 5)


# ─── Profit factor ────────────────────────────────────────────────────────


def test_profit_factor_is_gross_profit_over_gross_loss():
    stats = compute_stats(_trades(10.0, 5.0, -3.0, -2.0))

    assert stats.gross_profit == pytest.approx(15.0)
    assert stats.gross_loss == pytest.approx(5.0)  # positive
    assert stats.profit_factor == pytest.approx(3.0)


def test_profit_factor_is_undefined_with_no_losses():
    """None, not inf: inf is not valid JSON and 0.0 would read as terrible."""
    stats = compute_stats(_trades(10.0, 5.0))

    assert stats.profit_factor is None
    assert json.dumps(stats.to_dict())  # serialisable


def test_profit_factor_below_one_means_losing():
    stats = compute_stats(_trades(1.0, -3.0))

    assert stats.profit_factor == pytest.approx(1 / 3)
    assert stats.net_pnl < 0


# ─── Expectancy ───────────────────────────────────────────────────────────


def test_expectancy_equals_average_net_pnl_per_trade():
    """The probability-weighted form and the plain average are the same
    number by construction; both are reported because they are read for
    different reasons."""
    stats = compute_stats(_trades(10.0, 10.0, -5.0, -5.0))

    assert stats.expectancy == pytest.approx(2.5)
    assert stats.expectancy == pytest.approx(stats.avg_net_pnl_per_trade)


def test_expectancy_is_negative_for_a_losing_system():
    stats = compute_stats(_trades(2.0, -3.0, -3.0, 2.0))

    assert stats.expectancy is not None  # defined: the run had trades
    assert stats.expectancy < 0


def test_payoff_ratio_is_average_win_over_average_loss():
    stats = compute_stats(_trades(10.0, 20.0, -5.0, -5.0))

    assert stats.average_win == pytest.approx(15.0)
    assert stats.average_loss == pytest.approx(5.0)  # positive
    assert stats.payoff_ratio == pytest.approx(3.0)
    assert stats.largest_win == pytest.approx(20.0)
    assert stats.largest_loss == pytest.approx(5.0)


# ─── Drawdown ─────────────────────────────────────────────────────────────


def test_max_drawdown_is_peak_to_trough():
    # 100 -> 120 (peak) -> 90 (trough) -> 110
    absolute, pct = max_drawdown_from_equity([100.0, 120.0, 90.0, 110.0])

    assert absolute == pytest.approx(30.0)
    assert pct is not None
    assert pct == pytest.approx(30.0 / 120.0)


def test_a_monotonically_rising_curve_has_no_drawdown():
    absolute, pct = max_drawdown_from_equity([100.0, 110.0, 120.0])

    assert absolute == 0.0
    assert pct is None  # undefined: there was never a drawdown to measure


def test_drawdown_measures_the_deepest_trough_not_the_last():
    absolute, _ = max_drawdown_from_equity([100.0, 50.0, 100.0, 90.0])

    assert absolute == pytest.approx(50.0)


def test_drawdown_is_computed_from_the_starting_equity():
    stats = compute_stats(_trades(-10.0, -10.0, 5.0), starting_equity=1_000.0)

    assert stats.max_drawdown == pytest.approx(20.0)
    assert stats.max_drawdown_pct is not None
    assert stats.max_drawdown_pct == pytest.approx(20.0 / 1_000.0)


# ─── Streaks ──────────────────────────────────────────────────────────────


def test_longest_losing_streak():
    stats = compute_stats(_trades(1.0, -1.0, -1.0, -1.0, 1.0, -1.0, -1.0))

    assert stats.longest_losing_streak == 3
    assert stats.longest_winning_streak == 1


def test_a_scratch_breaks_a_streak_rather_than_extending_it():
    stats = compute_stats(_trades(-1.0, -1.0, 0.0, -1.0))

    assert stats.longest_losing_streak == 2


def test_streaks_of_a_single_outcome():
    assert compute_stats(_trades(-1.0, -1.0, -1.0)).longest_losing_streak == 3
    assert compute_stats(_trades(1.0, 1.0)).longest_winning_streak == 2


# ─── Grouping ─────────────────────────────────────────────────────────────


def test_results_are_grouped_by_strategy():
    trades = [
        closed_trade(10.0, agent_id="SCALPER_XAUUSD_0", minute=0),
        closed_trade(-4.0, agent_id="SCALPER_XAUUSD_1", minute=1),
        closed_trade(7.0, agent_id="SWING_OIL_0", minute=2),
    ]

    result = build_result(trades, starting_equity=0.0, config={})

    assert set(result.by_strategy) == {"SCALPER", "SWING"}
    assert result.by_strategy["SCALPER"].trades == 2
    assert result.by_strategy["SCALPER"].net_pnl == pytest.approx(6.0)
    assert result.by_strategy["SWING"].net_pnl == pytest.approx(7.0)
    assert result.total.net_pnl == pytest.approx(13.0)


def test_results_are_grouped_by_symbol():
    trades = [
        closed_trade(10.0, symbol=Symbol.XAUUSD, minute=0),
        closed_trade(-4.0, symbol=Symbol.OIL, agent_id="SCALPER_OIL_0", minute=1),
        closed_trade(2.0, symbol=Symbol.OIL, agent_id="SCALPER_OIL_1", minute=2),
    ]

    result = build_result(trades, starting_equity=0.0, config={})

    assert set(result.by_symbol) == {"XAUUSD", "OIL"}
    assert result.by_symbol["OIL"].trades == 2
    assert result.by_symbol["OIL"].net_pnl == pytest.approx(-2.0)


def test_grouped_net_pnl_sums_to_the_total():
    trades = [
        closed_trade(3.0, agent_id="SCALPER_XAUUSD_0", symbol=Symbol.XAUUSD, minute=0),
        closed_trade(-1.0, agent_id="SWING_OIL_0", symbol=Symbol.OIL, minute=1),
        closed_trade(5.0, agent_id="SWING_XAUUSD_0", symbol=Symbol.XAUUSD, minute=2),
    ]

    result = build_result(trades, starting_equity=0.0, config={})

    assert sum(s.net_pnl for s in result.by_strategy.values()) == pytest.approx(result.total.net_pnl)
    assert sum(s.net_pnl for s in result.by_symbol.values()) == pytest.approx(result.total.net_pnl)


@pytest.mark.parametrize(
    ("agent_id", "expected"),
    [
        ("SCALPER_XAUUSD_0", "SCALPER"),
        ("SWING_OIL_3", "SWING"),
        ("NEWS_REACTIVE_PLTR_1", "NEWS_REACTIVE"),  # must not match a shorter prefix
        ("HEDGER_NAS100_0", "HEDGER"),
        ("RL_US100_1", "RL"),
        ("garbage", "UNKNOWN"),
    ],
)
def test_strategy_is_derived_from_the_agent_id(agent_id, expected):
    assert strategy_of(agent_id) == expected


# ─── Equity curve and exposure ────────────────────────────────────────────


def test_equity_curve_accumulates_net_pnl_from_the_starting_equity():
    result = build_result(_trades(10.0, -4.0, 2.0), starting_equity=1_000.0, config={})

    assert [p.equity for p in result.equity_curve] == pytest.approx([1010.0, 1006.0, 1008.0])
    assert len(result.equity_curve) == 3
    assert result.equity_curve[0].trade_id


def test_exposure_is_reported_when_bar_counts_are_supplied():
    stats = compute_stats(_trades(1.0), bars_in_market=30, total_bars=100)

    assert stats.exposure_pct == pytest.approx(0.30)
    assert stats.bars_in_market == 30


def test_exposure_is_undefined_without_bar_counts():
    assert compute_stats(_trades(1.0)).exposure_pct is None


# ─── Serialisation ────────────────────────────────────────────────────────


def test_json_output_is_valid_and_round_trips():
    result = build_result(_trades(10.0, -4.0), starting_equity=100.0, config={"seed": 42})

    payload = json.loads(to_json(result))

    assert payload["config"]["seed"] == 42
    assert payload["total"]["trades"] == 2
    assert payload["total"]["net_pnl"] == pytest.approx(6.0)
    assert len(payload["equity_curve"]) == 2
    assert "SCALPER" in payload["by_strategy"]


def test_json_renders_undefined_ratios_as_null():
    result = build_result(_trades(10.0), starting_equity=0.0, config={})

    payload = json.loads(to_json(result))

    assert payload["total"]["profit_factor"] is None


def test_json_can_be_written_to_a_file(tmp_path):
    result = build_result(_trades(1.0), starting_equity=0.0, config={})
    path = tmp_path / "result.json"

    to_json(result, path)

    assert json.loads(path.read_text())["total"]["trades"] == 1


def test_csv_has_one_row_per_trade_with_the_cost_breakdown():
    result = build_result([closed_trade(net_pnl=8.0, gross_pnl=10.0, minute=0)], starting_equity=0.0, config={})

    csv_text = trades_to_csv(result)
    header, row = csv_text.strip().splitlines()

    assert "gross_pnl" in header and "total_costs" in header and "net_pnl" in header
    assert "entry_fill_price" in header and "exit_fill_price" in header
    assert "SCALPER" in row  # strategy column derived from the agent id


def test_csv_can_be_written_to_a_file(tmp_path):
    result = build_result(_trades(1.0, 2.0), starting_equity=0.0, config={})
    path = tmp_path / "trades.csv"

    trades_to_csv(result, path)
    lines = path.read_text().strip().splitlines()

    assert len(lines) == 3  # header + 2 trades


def test_terminal_report_renders_the_headline_figures():
    result = build_result(_trades(10.0, -4.0), starting_equity=100.0, config={"seed": 42, "symbols": ["XAUUSD"]})

    text = render(result)

    assert "BACKTEST RESULT" in text
    assert "NET pnl" in text
    assert "By strategy" in text
    assert "By symbol" in text


def test_terminal_report_shows_undefined_as_na():
    result = build_result(_trades(10.0), starting_equity=0.0, config={})

    assert "n/a" in render(result)  # profit factor with no losses
