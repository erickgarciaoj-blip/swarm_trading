"""
Deterministic replay / backtest engine.

Runs the real agent classes against historical bars using the same cost
model (core/costs.py) and the same RiskEngine as the paper runtime. No
strategy, TP/SL rule or PnL formula is reimplemented here.

A result describes the strategies as run on the bars supplied. The interval
matters: the strategies size stops and targets from ATR, so replaying 1h
bars is not a measurement of a runtime consuming 1m bars. See engine.py's
"WHAT A RESULT IS AND IS NOT".

    from swarm_trading.backtest import Backtester, BacktestConfig, load_dataframe

    series = {Symbol.XAUUSD: load_dataframe(Symbol.XAUUSD, df)}
    result = await Backtester(BacktestConfig(seed=42)).run(series)
    print(render(result))
"""

from swarm_trading.backtest.broker import IntrabarPolicy, ReplayBroker
from swarm_trading.backtest.data import OHLCVSeries, load_csv, load_dataframe
from swarm_trading.backtest.engine import BacktestConfig, Backtester
from swarm_trading.backtest.metrics import BacktestResult, Stats, compute_stats
from swarm_trading.backtest.report import render, to_json, trades_to_csv

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "IntrabarPolicy",
    "OHLCVSeries",
    "ReplayBroker",
    "Stats",
    "compute_stats",
    "load_csv",
    "load_dataframe",
    "render",
    "to_json",
    "trades_to_csv",
]
