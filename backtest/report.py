"""
Serialisation and terminal rendering for a BacktestResult.

Three outputs, all from the same result object: JSON (full fidelity,
machine-readable), CSV (one row per closed trade, for a spreadsheet or
pandas), and a terminal summary meant to be read.

`None` in a statistic means UNDEFINED — a ratio whose denominator was zero,
e.g. profit factor with no losing trades. It is rendered as "n/a" and
serialised as JSON `null`, never coerced to 0.0, which would be
indistinguishable from a genuinely terrible result.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from swarm_trading.backtest.metrics import BacktestResult, Stats, strategy_of

# One row per closed trade. Includes the full cost decomposition and both
# the reference and effective fill prices, so a result can be audited
# without re-running the replay.
TRADE_CSV_COLUMNS = (
    "trade_id",
    "agent_id",
    "strategy",
    "symbol",
    "side",
    "quantity",
    "entry_price",
    "entry_fill_price",
    "exit_price",
    "exit_fill_price",
    "sl_price",
    "tp_price",
    "gross_pnl",
    "entry_costs",
    "exit_costs",
    "total_costs",
    "commission",
    "net_pnl",
    "opened_at",
    "closed_at",
)


def to_json(result: BacktestResult, path: str | Path | None = None, indent: int = 2) -> str:
    payload = json.dumps(result.to_dict(), indent=indent, default=str)
    if path is not None:
        Path(path).write_text(payload, encoding="utf-8")
    return payload


def trades_to_csv(result: BacktestResult, path: str | Path | None = None) -> str:
    rows: list[dict[str, Any]] = []
    for t in result.trades:
        rows.append(
            {
                "trade_id": t.trade_id,
                "agent_id": t.agent_id,
                "strategy": strategy_of(t.agent_id),
                "symbol": t.symbol.value,
                "side": t.side.value,
                "quantity": t.quantity,
                "entry_price": t.entry_price,
                "entry_fill_price": t.entry_fill_price,
                "exit_price": t.exit_price,
                "exit_fill_price": t.exit_fill_price,
                "sl_price": t.sl_price,
                "tp_price": t.tp_price,
                "gross_pnl": t.gross_pnl,
                "entry_costs": t.entry_costs,
                "exit_costs": t.exit_costs,
                "total_costs": t.total_costs,
                "commission": t.commission,
                "net_pnl": t.pnl,
                "opened_at": t.opened_at.isoformat() if t.opened_at else "",
                "closed_at": t.closed_at.isoformat() if t.closed_at else "",
            }
        )

    if path is not None:
        with Path(path).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(TRADE_CSV_COLUMNS))
            writer.writeheader()
            writer.writerows(rows)

    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(TRADE_CSV_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _num(value: float | None, places: int = 4, width: int = 12) -> str:
    return "n/a".rjust(width) if value is None else f"{value:>{width},.{places}f}"


def _pct(value: float | None, width: int = 9) -> str:
    return "n/a".rjust(width) if value is None else f"{value:>{width - 1}.1%} "


def _stats_row(label: str, s: Stats) -> str:
    return (
        f"  {label:<16}{s.trades:>7}{_num(s.gross_pnl)}{_num(s.total_costs)}{_num(s.net_pnl)}"
        f"{_num(s.avg_net_pnl_per_trade, 5)}{_pct(s.win_rate)}{_num(s.profit_factor, 2, 9)}"
    )


def render(result: BacktestResult) -> str:
    lines: list[str] = []
    add = lines.append
    total = result.total

    add("=" * 96)
    add("BACKTEST RESULT")
    add("=" * 96)

    cfg = result.config
    add(f"  symbols            {', '.join(cfg.get('symbols', []))}")
    add(f"  bars               {cfg.get('bars')} (warmup {cfg.get('warmup_bars')})")
    add(f"  intrabar policy    {cfg.get('intrabar_policy')}")
    add(f"  seed               {cfg.get('seed')}")
    add(f"  costs              {cfg.get('cost_book')}")

    add("")
    add("--- Totals ---")
    add(f"  trades             {total.trades:>12,}")
    add(f"  gross pnl          {_num(total.gross_pnl)}")
    add(f"  total costs        {_num(total.total_costs)}")
    add(f"  NET pnl            {_num(total.net_pnl)}")
    add(f"  avg net / trade    {_num(total.avg_net_pnl_per_trade, 5)}")
    add(f"  expectancy         {_num(total.expectancy, 5)}")
    add(f"  win rate           {_pct(total.win_rate, 12)}")
    add(f"  profit factor      {_num(total.profit_factor, 3)}")
    add(f"  payoff ratio       {_num(total.payoff_ratio, 3)}")
    add(f"  average win        {_num(total.average_win, 5)}")
    add(f"  average loss       {_num(total.average_loss, 5)}")
    add(f"  largest win        {_num(total.largest_win, 5)}")
    add(f"  largest loss       {_num(total.largest_loss, 5)}")
    add(f"  max drawdown       {_num(total.max_drawdown)}")
    add(f"  max drawdown %     {_pct(total.max_drawdown_pct, 12)}")
    add(f"  losing streak      {total.longest_losing_streak:>12,}")
    add(f"  winning streak     {total.longest_winning_streak:>12,}")
    add(f"  exposure           {_pct(total.exposure_pct, 12)}")

    header = f"  {'':<16}{'trades':>7}{'gross':>12}{'costs':>12}{'net':>12}{'avg/trade':>12}{'win rate':>9}{'PF':>9}"

    add("")
    add("--- By strategy ---")
    add(header)
    for name, stats in sorted(result.by_strategy.items()):
        add(_stats_row(name, stats))

    add("")
    add("--- By symbol ---")
    add(header)
    for name, stats in sorted(result.by_symbol.items()):
        add(_stats_row(name, stats))

    if total.trades and total.gross_pnl:
        drag = total.total_costs / abs(total.gross_pnl)
        add("")
        add(f"  Costs consumed {drag:.1%} of gross PnL.")

    add("=" * 96)
    return "\n".join(lines)
