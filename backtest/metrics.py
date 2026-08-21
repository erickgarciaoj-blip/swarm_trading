"""
Backtest statistics — pure functions over a list of closed trades.

Everything here is computed on NET PnL (`ExecutedTrade.pnl`, already net of
spread, slippage and commission — see core/models.ExecutedTrade). Gross and
cost totals are reported alongside so the drag is visible, never so it can
be double-counted.

DELIBERATELY NOT A SHARPE RATIO
-------------------------------
The audit found BaseAgent._compute_sharpe() computes mean(pnl)/stdev(pnl)
over absolute dollar PnL, unannualised and per-trade — a figure that changes
with position size even when the strategy does not, and that is comparable
with nothing. Rather than reproduce that here, this module reports
expectancy, profit factor and payoff ratio, which are well-defined per-trade
statistics that need no period or annualisation to mean something. Fixing
the Sharpe is a separate change to a separate module.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from swarm_trading.core.models import ExecutedTrade

# Returned instead of a ratio when the denominator is zero. `None`
# serialises to JSON `null`, which reads as "undefined" — unlike 0.0
# (indistinguishable from a genuinely terrible result) or `inf` (which is
# not valid JSON at all).
UNDEFINED: float | None = None


@dataclass
class EquityPoint:
    timestamp: str
    equity: float
    trade_id: str
    net_pnl: float


@dataclass
class Stats:
    """Result statistics for one bucket — a strategy, a symbol, or the whole run."""

    trades: int = 0
    gross_pnl: float = 0.0
    total_costs: float = 0.0
    net_pnl: float = 0.0
    avg_net_pnl_per_trade: float | None = None

    wins: int = 0
    losses: int = 0
    scratches: int = 0  # exactly zero net — neither a win nor a loss
    win_rate: float | None = None

    gross_profit: float = 0.0  # sum of winning net PnL
    gross_loss: float = 0.0  # sum of losing net PnL, as a POSITIVE number
    profit_factor: float | None = None
    expectancy: float | None = None

    average_win: float | None = None
    average_loss: float | None = None  # positive number
    payoff_ratio: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None

    max_drawdown: float = 0.0  # absolute, positive
    max_drawdown_pct: float | None = None
    longest_losing_streak: int = 0
    longest_winning_streak: int = 0

    # Fraction of the replay's bars during which at least one position in
    # this bucket was open. None when the engine did not supply bar counts.
    exposure_pct: float | None = None
    bars_in_market: int | None = None
    total_bars: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    """Everything one replay produced, serialisable end to end."""

    total: Stats = field(default_factory=Stats)
    by_strategy: dict[str, Stats] = field(default_factory=dict)
    by_symbol: dict[str, Stats] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    trades: list[ExecutedTrade] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "total": self.total.to_dict(),
            "by_strategy": {k: v.to_dict() for k, v in sorted(self.by_strategy.items())},
            "by_symbol": {k: v.to_dict() for k, v in sorted(self.by_symbol.items())},
            "equity_curve": [asdict(p) for p in self.equity_curve],
        }


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else UNDEFINED


def max_drawdown_from_equity(equity: Sequence[float]) -> tuple[float, float | None]:
    """(absolute drawdown, drawdown as a fraction of the peak).

    Peak-to-trough on the realised equity curve, which is the only drawdown a
    trade-level backtest can measure honestly: it says nothing about how far
    an open position went against itself between entry and exit. That
    intra-trade excursion needs bar-level marking, noted as a limitation
    rather than approximated here.
    """
    peak = float("-inf")
    max_dd = 0.0
    max_dd_pct: float | None = None

    for value in equity:
        peak = max(peak, value)
        drawdown = peak - value
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = drawdown / peak if peak else UNDEFINED
    return max_dd, max_dd_pct


def _streaks(net_pnls: Iterable[float]) -> tuple[int, int]:
    """(longest losing streak, longest winning streak).

    A scratch (exactly zero) breaks both streaks rather than extending
    either — it is neither a loss nor a win.
    """
    longest_loss = longest_win = current_loss = current_win = 0
    for pnl in net_pnls:
        if pnl < 0:
            current_loss += 1
            current_win = 0
        elif pnl > 0:
            current_win += 1
            current_loss = 0
        else:
            current_loss = current_win = 0
        longest_loss = max(longest_loss, current_loss)
        longest_win = max(longest_win, current_win)
    return longest_loss, longest_win


def compute_stats(
    trades: Sequence[ExecutedTrade],
    starting_equity: float = 0.0,
    bars_in_market: int | None = None,
    total_bars: int | None = None,
) -> Stats:
    """Full statistics for one bucket of closed trades.

    Trades are consumed in the order given; the caller is responsible for
    that order being chronological, which the engine guarantees.
    """
    stats = Stats(trades=len(trades), bars_in_market=bars_in_market, total_bars=total_bars)
    if total_bars:
        stats.exposure_pct = (bars_in_market or 0) / total_bars

    if not trades:
        return stats

    net_pnls = [t.pnl for t in trades]
    stats.gross_pnl = sum(t.gross_pnl for t in trades)
    stats.total_costs = sum(t.total_costs for t in trades)
    stats.net_pnl = sum(net_pnls)
    stats.avg_net_pnl_per_trade = stats.net_pnl / len(trades)

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    stats.wins = len(wins)
    stats.losses = len(losses)
    stats.scratches = len(net_pnls) - len(wins) - len(losses)
    stats.win_rate = len(wins) / len(net_pnls)

    stats.gross_profit = sum(wins)
    stats.gross_loss = -sum(losses)  # positive
    stats.profit_factor = _safe_ratio(stats.gross_profit, stats.gross_loss)

    stats.average_win = sum(wins) / len(wins) if wins else UNDEFINED
    stats.average_loss = -sum(losses) / len(losses) if losses else UNDEFINED
    stats.payoff_ratio = (
        _safe_ratio(stats.average_win, stats.average_loss)
        if stats.average_win is not None and stats.average_loss is not None
        else UNDEFINED
    )
    stats.largest_win = max(wins) if wins else UNDEFINED
    stats.largest_loss = -min(losses) if losses else UNDEFINED

    # Expectancy: average net result per trade, stated as the probability-
    # weighted identity so it is readable as "what one more trade is worth".
    # Equals avg_net_pnl_per_trade by construction; both are reported because
    # they are read for different reasons.
    win_rate = stats.win_rate
    stats.expectancy = win_rate * (stats.average_win or 0.0) - (1 - win_rate) * (stats.average_loss or 0.0)

    equity = [starting_equity]
    for pnl in net_pnls:
        equity.append(equity[-1] + pnl)
    stats.max_drawdown, stats.max_drawdown_pct = max_drawdown_from_equity(equity)

    stats.longest_losing_streak, stats.longest_winning_streak = _streaks(net_pnls)
    return stats


def strategy_of(agent_id: str) -> str:
    """Strategy family from an agent id ("SCALPER_XAUUSD_0" -> "SCALPER").

    Longest-first so NEWS_REACTIVE is not shadowed by a shorter prefix.
    """
    for prefix in ("NEWS_REACTIVE", "SCALPER", "SWING", "HEDGER", "RL"):
        if agent_id.startswith(prefix + "_"):
            return prefix
    return "UNKNOWN"


def build_result(
    trades: Sequence[ExecutedTrade],
    starting_equity: float,
    config: dict[str, Any],
    bars_in_market_by_bucket: dict[str, int] | None = None,
    total_bars: int | None = None,
) -> BacktestResult:
    """Aggregate closed trades into total / per-strategy / per-symbol stats."""
    by_strategy: dict[str, list[ExecutedTrade]] = defaultdict(list)
    by_symbol: dict[str, list[ExecutedTrade]] = defaultdict(list)
    for trade in trades:
        by_strategy[strategy_of(trade.agent_id)].append(trade)
        by_symbol[trade.symbol.value].append(trade)

    exposure = bars_in_market_by_bucket or {}

    result = BacktestResult(
        total=compute_stats(trades, starting_equity, bars_in_market=exposure.get("__total__"), total_bars=total_bars),
        by_strategy={
            name: compute_stats(group, starting_equity, exposure.get(f"strategy:{name}"), total_bars)
            for name, group in by_strategy.items()
        },
        by_symbol={
            name: compute_stats(group, starting_equity, exposure.get(f"symbol:{name}"), total_bars)
            for name, group in by_symbol.items()
        },
        trades=list(trades),
        config=config,
    )

    equity = starting_equity
    for trade in trades:
        equity += trade.pnl
        result.equity_curve.append(
            EquityPoint(
                timestamp=trade.closed_at.isoformat() if trade.closed_at else "",
                equity=equity,
                trade_id=trade.trade_id,
                net_pnl=trade.pnl,
            )
        )
    return result
