"""
Backtester — the replay loop.

Runs the REAL agent classes (ScalperAgent, SwingAgent) against historical
bars, through the same RiskEngine and the same cost model the paper runtime
uses. Nothing here reimplements a strategy, a TP/SL rule or a PnL formula;
where the runtime has one, this calls it.

THE LOOP, AND WHERE LOOK-AHEAD WOULD HIDE
------------------------------------------
For each bar index i, in order:

  1. advance the replay clock to i;
  2. resolve exits against bar i's HIGH/LOW for positions opened at < i;
  3. build a MarketState from candles[..i] and indicators as of i;
  4. ask each agent to analyze() that state;
  5. validate through RiskEngine;
  6. open approved positions at bar i's CLOSE.

Step 2 precedes step 4 deliberately, mirroring
SwarmOrchestrator._process_symbol(), so a closing trade frees its
concentration slot before new signals are considered in the same bar.

Step 6 uses the close, and step 2 skips positions opened on the current bar
(enforced in ReplayBroker), so a position opened from bar i's close can
first be resolved on bar i+1. Nothing in the loop reads index i+1 while
processing i: OHLCVSeries.window() is bounded above by the requested index,
and the indicator frame is causal (see backtest/data.py).

WHAT A RESULT IS AND IS NOT
---------------------------
A replay measures the strategies AS RUN ON THE BARS IT WAS GIVEN. The bar
interval is not a detail: ScalperAgent sizes its stop and target from
ATR-14, so a 1h replay gives it stops roughly an order of magnitude wider
than the runtime's, and RSI-14 on 1h bars turns over far more slowly than on
1m. A backtest on 1h bars therefore validates the ENGINE — determinism,
costs, exit resolution, no look-ahead — but it does not measure the
strategies the live swarm is running, which consume 1m bars from MarketFeed.

Treat a cross-interval result as sensitivity analysis, never as a
profitability estimate for production. Comparing against the runtime
requires replaying the runtime's own interval.

DETERMINISM
-----------
ScalperAgent and SwingAgent randomise their thresholds in __init__ using the
`random` module. The engine seeds it before constructing agents and restores
the previous state afterwards, so a run is reproducible without leaking a
seeded global into whatever called it.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
from swarm_trading.agents.swing.swing_agent import SwingAgent
from swarm_trading.backtest.broker import IntrabarPolicy, ReplayBroker
from swarm_trading.backtest.data import OHLCVSeries
from swarm_trading.backtest.metrics import BacktestResult, build_result, strategy_of
from swarm_trading.core.config import settings
from swarm_trading.core.costs import CostBook
from swarm_trading.core.models import ExecutedTrade, MarketState, Symbol
from swarm_trading.risk.engine.risk_engine import RiskEngine

# Matches the live feed: MarketState carries the last 100 candles
# (MarketFeed._fetch_yfinance slices candles[-100:]).
DEFAULT_LOOKBACK = 100

# Only these two run in this phase. News, Hedger and RL are excluded on
# purpose — the audit established they are dormant or produce noise, and
# backtesting a known-broken agent measures the breakage, not the strategy.
SUPPORTED_STRATEGIES = ("SCALPER", "SWING")


@dataclass
class BacktestConfig:
    """One replay's inputs. Everything that can change a result lives here,
    so a run is fully described by this object plus the bar data."""

    seed: int = 42
    lookback: int = DEFAULT_LOOKBACK
    intrabar_policy: IntrabarPolicy = IntrabarPolicy.CONSERVATIVE
    cost_book: CostBook | None = None  # None -> settings.instrument_costs
    capital_per_agent: float = field(default_factory=lambda: settings.swarm_capital_per_agent)
    scalpers_per_symbol: int = 6
    swing_per_symbol: int = 6
    # Skip bars before the slowest indicator has warmed up. EMA-200 needs
    # ~200 bars to be meaningful; below this the strategies would trade on
    # values still converging from their seed.
    warmup_bars: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "lookback": self.lookback,
            "intrabar_policy": self.intrabar_policy.value,
            "capital_per_agent": self.capital_per_agent,
            "scalpers_per_symbol": self.scalpers_per_symbol,
            "swing_per_symbol": self.swing_per_symbol,
            "warmup_bars": self.warmup_bars,
            "cost_book": repr(self.cost_book) if self.cost_book else "settings.instrument_costs",
        }


def build_agents(config: BacktestConfig, symbols: list[Symbol]) -> list[BaseAgent]:
    """Construct the real agent classes with the real factory parameters.

    Parameters are copied from SwarmFactory (atr multipliers, adx
    thresholds, id scheme) so a replay exercises the deployed configuration
    rather than an idealised one. They are duplicated rather than imported
    because build_swarm() registers into an orchestrator and builds all five
    agent types including the three excluded here.

    TECHNICAL DEBT — this duplication can silently go stale. If someone
    retunes SwarmFactory's ladder (atr_sl_multiplier, atr_tp_mult,
    adx_threshold) and does not mirror it here, backtests keep reporting on
    the OLD configuration while claiming to replay the deployed one, and
    nothing fails. The fix is to extract the composition into a shared,
    data-driven spec both callers read from (ARCHITECTURE_REVIEW's Fase 8
    already proposes exactly that); it is deliberately out of scope for this
    phase. Until then, test_engine.py pins the current values literally, so
    a divergence at least breaks a test rather than passing unnoticed.

    The `random` seed is set around construction only — the agents' analyze()
    paths are themselves deterministic.
    """
    previous_state = random.getstate()
    random.seed(config.seed)
    try:
        agents: list[BaseAgent] = []
        for symbol in symbols:
            for i in range(config.scalpers_per_symbol):
                agents.append(
                    ScalperAgent(
                        symbol=symbol,
                        agent_id=f"SCALPER_{symbol.value}_{i}",
                        initial_capital=config.capital_per_agent,
                        atr_sl_multiplier=1.5 + i * 0.1,
                        atr_tp_multiplier=3.0 + i * 0.2,
                    )
                )
            for i in range(config.swing_per_symbol):
                agents.append(
                    SwingAgent(
                        symbol=symbol,
                        agent_id=f"SWING_{symbol.value}_{i}",
                        initial_capital=config.capital_per_agent,
                        adx_threshold=22.0 + i * 1.5,
                        atr_sl_mult=2.0 + i * 0.1,
                        atr_tp_mult=4.0 + i * 0.5,
                    )
                )
        return agents
    finally:
        random.setstate(previous_state)


class Backtester:
    """Replays one or more symbols' bar series through the real strategies."""

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.broker = ReplayBroker(cost_book=self.config.cost_book, intrabar_policy=self.config.intrabar_policy)
        self.risk = RiskEngine()
        self._closed: list[ExecutedTrade] = []
        # bucket -> number of bars during which that bucket held a position
        self._bars_in_market: dict[str, int] = defaultdict(int)
        self._total_bars = 0

    async def run(self, series_by_symbol: dict[Symbol, OHLCVSeries]) -> BacktestResult:
        """Replay every symbol's series bar by bar.

        Symbols advance in lockstep by bar INDEX, not by wall-clock
        timestamp. That is exact when the inputs share a calendar and
        interval, and approximate when they do not — see the phase report's
        limitations. Series of unequal length simply stop contributing once
        exhausted.
        """
        if not series_by_symbol:
            raise ValueError("no series to replay")

        symbols = sorted(series_by_symbol, key=lambda s: s.value)
        agents = build_agents(self.config, symbols)
        agents_by_symbol: dict[Symbol, list[BaseAgent]] = defaultdict(list)
        for agent in agents:
            agents_by_symbol[agent.symbol].append(agent)

        max_bars = max(len(s) for s in series_by_symbol.values())
        self._total_bars = max(0, max_bars - self.config.warmup_bars)

        logger.info(
            f"[Backtest] {len(agents)} agents | {len(symbols)} symbols | {max_bars} bars "
            f"| policy={self.config.intrabar_policy.value} | seed={self.config.seed}"
        )

        for bar_index in range(self.config.warmup_bars, max_bars):
            # Timestamp comes from the first symbol that has this bar, so
            # fills carry simulated time rather than wall clock.
            bar_time = next(
                (s.candle(bar_index).timestamp for s in series_by_symbol.values() if bar_index < len(s)),
                None,
            )
            self.broker.set_bar_index(bar_index, bar_time)
            for symbol in symbols:
                series = series_by_symbol[symbol]
                if bar_index >= len(series):
                    continue
                await self._process_bar(symbol, series, bar_index, agents_by_symbol[symbol])
            self._record_exposure()

        return build_result(
            trades=self._closed,
            starting_equity=self.config.capital_per_agent * len(agents),
            config={
                **self.config.to_dict(),
                "symbols": [s.value for s in symbols],
                "bars": max_bars,
                # How many exits the bar data could not settle on its own.
                "ambiguous_exits": self.broker.ambiguous_exits,
            },
            bars_in_market_by_bucket=dict(self._bars_in_market),
            total_bars=self._total_bars,
        )

    async def _process_bar(self, symbol: Symbol, series: OHLCVSeries, bar_index: int, agents: list[BaseAgent]) -> None:
        candle = series.candle(bar_index)

        # 1. Exits first — frees concentration slots before new signals are
        #    considered, exactly as SwarmOrchestrator._process_symbol does.
        for closed_trade in self.broker.resolve_bar(symbol, candle, bar_index):
            self.risk.on_trade_closed(closed_trade)
            agent = next((a for a in agents if a.agent_id == closed_trade.agent_id), None)
            if agent is not None:
                await agent.on_trade_closed(closed_trade)
            self._closed.append(closed_trade)

        # 2. The state this bar's decision may use — nothing past bar_index.
        state = MarketState(
            symbol=symbol,
            timestamp=candle.timestamp,
            candles=series.window(bar_index, self.config.lookback),
            indicators=series.indicators_at(bar_index),
        )

        # 3. Real strategies, real risk gate, entry at this bar's close.
        for agent in agents:
            if not agent.is_alive:
                continue
            proposal = await agent.analyze(state)
            if proposal is None:
                continue

            approved, reason = self.risk.validate(
                proposal, agent.get_metrics(), is_news_blackout=state.is_news_blackout
            )
            if not approved:
                logger.debug(f"[Backtest] {agent.agent_id} rejected: {reason}")
                continue

            trade = await self.broker.execute(proposal)
            self.risk.on_order_opened(proposal)
            logger.debug(f"[Backtest] {agent.agent_id} opened {trade.side.value} @ {trade.entry_price}")

    def _record_exposure(self) -> None:
        """Count this bar toward every bucket currently holding a position."""
        open_positions = list(self.broker._open_trades.values())
        if not open_positions:
            return
        self._bars_in_market["__total__"] += 1
        for name in {strategy_of(t.agent_id) for t in open_positions}:
            self._bars_in_market[f"strategy:{name}"] += 1
        for name in {t.symbol.value for t in open_positions}:
            self._bars_in_market[f"symbol:{name}"] += 1
