"""
SwarmOrchestrator — the MCP-layer brain.
Coordinates all 100 agents, data feeds, risk validation and broker execution.
Designed to run as a single async loop; agents are coroutines, not threads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import AgentType, ExecutedTrade, MarketState, Side, Symbol
from swarm_trading.risk.engine.risk_engine import RiskEngine

JSONDict = dict[str, Any]
Broadcaster = Callable[[JSONDict], Awaitable[None]]


async def _noop_broadcaster(_message: JSONDict) -> None:
    pass


def _trade_payload(trade: ExecutedTrade) -> JSONDict:
    """JSON-safe view of an ExecutedTrade for the WebSocket feed."""
    return {
        "trade_id": trade.trade_id,
        "agent_id": trade.agent_id,
        "symbol": trade.symbol.value,
        "side": trade.side.value,
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "sl_price": trade.sl_price,
        "tp_price": trade.tp_price,
        "status": trade.status.value,
        "pnl": trade.pnl,
        "opened_at": trade.opened_at.isoformat(),
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
    }


if TYPE_CHECKING:
    from swarm_trading.agents.base.base_agent import BaseAgent
    from swarm_trading.brokers.adapters.broker_interface import BrokerInterface
    from swarm_trading.data.feeds.market_feed import MarketFeed
    from swarm_trading.data.historic.repository import AsyncRepository
    from swarm_trading.data.news.news_feed import NewsFeed


class SwarmOrchestrator:
    """
    Single entry-point that owns:
    - The agent registry (100 agents keyed by agent_id)
    - The risk engine
    - Broker adapters
    - Market & news data feeds
    """

    def __init__(
        self,
        broker: BrokerInterface,
        market_feed: MarketFeed,
        news_feed: NewsFeed,
        repository: AsyncRepository | None = None,
    ):
        self._agents: dict[str, BaseAgent] = {}
        self._broker = broker
        self._market_feed = market_feed
        self._news_feed = news_feed
        self._repository = repository
        # AsyncRepository.save_risk_state matches RiskStatePersistor's shape
        # structurally (see risk_engine.py) — passed straight through, no
        # adapter needed; repository=None keeps the "DB is optional" stance.
        self._risk = RiskEngine(persistor=repository)
        self._running = False
        self._broadcast: Broadcaster = _noop_broadcaster
        # Fire-and-forget DB writes (save_trade) must be kept referenced
        # until they finish, or asyncio may garbage-collect them mid-flight.
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._started_at: datetime | None = None
        self._last_price: dict[Symbol, float] = {}
        # Unrealized (mark-to-market) PnL per agent, recomputed once per tick from
        # the broker's open positions — agent.equity only moves on trade close,
        # so this is what lets the dashboard's equity curve move between closes.
        self._floating_pnl: dict[str, float] = {}

    def _fire_and_forget(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def set_broadcaster(self, fn: Broadcaster) -> None:
        """Wire a transport (e.g. dashboard/websocket's ConnectionManager.broadcast)
        without the orchestrator importing FastAPI/Starlette types itself."""
        self._broadcast = fn

    # ─── Agent management ────────────────────────────────────────────────────

    def register_agent(self, agent: BaseAgent) -> None:
        self._agents[agent.agent_id] = agent
        logger.info(f"[Swarm] Registered {agent}")

    def remove_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    @property
    def active_agents(self) -> list[BaseAgent]:
        return [a for a in self._agents.values() if a.is_alive]

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main event loop. Fetches market state every tick and dispatches to agents."""
        self._running = True
        self._started_at = datetime.utcnow()
        logger.info(f"[Swarm] Starting with {len(self._agents)} agents")

        while self._running:
            try:
                for symbol in Symbol:
                    # Isolated per symbol: a data-source outage on one symbol
                    # (e.g. yfinance having no intraday bars for a futures
                    # ticker right now) must not block every other symbol's
                    # tick — before this, one bad symbol raised past this
                    # loop into the outer except below, which restarted the
                    # whole tick from Symbol's first member every time,
                    # so the swarm never progressed past the broken symbol.
                    try:
                        await self._process_symbol(symbol)
                    except Exception as exc:
                        logger.warning(f"[Swarm] Skipping {symbol.value} this tick: {exc}")

                self._floating_pnl = await self._compute_floating_pnl()
                # Must run after floating PnL is refreshed above — both the
                # total-loss (30%) and daily-loss (15%) halts are evaluated
                # against realized + unrealized equity. See ADR-0010. Any
                # transition triggered here already persists immediately,
                # inside this call — the next line is only a retry backstop.
                await self._risk.update_daily_tracking(self._compute_total_equity())
                await self._risk.persist_if_dirty()
                await self._broadcast({"type": "swarm_summary", "data": self.get_swarm_summary()})
                await asyncio.sleep(15)  # tick every 15s — adjust per strategy

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Swarm] Orchestrator error: {exc}")
                await asyncio.sleep(5)

        logger.info("[Swarm] Stopped")

    async def _process_symbol(self, symbol: Symbol) -> None:
        state: MarketState = await self._market_feed.get_state(symbol)
        news_events = await self._news_feed.get_upcoming(symbol)
        state.upcoming_news = news_events

        if state.candles:
            self._last_price[symbol] = state.candles[-1].close

            # Resolve TP/SL against this tick's price before dispatching new
            # signals — frees up SYMBOL_CONCENTRATION slots for brokers (e.g.
            # offline mode) that don't manage TP/SL server-side themselves.
            closed_trades = await self._broker.check_tp_sl(symbol, state.candles[-1].close)
            for trade in closed_trades:
                await self.on_trade_closed_callback(trade)

        # Dispatch all active agents for this symbol concurrently
        symbol_agents = [a for a in self.active_agents if a.symbol == symbol]
        await asyncio.gather(*[self._process_agent(agent, state) for agent in symbol_agents], return_exceptions=True)

    async def _process_agent(self, agent: BaseAgent, state: MarketState) -> None:
        try:
            proposal = await agent.analyze(state)
            if proposal is None:
                return

            metrics = agent.get_metrics()
            approved, reason = self._risk.validate(proposal, metrics, is_news_blackout=state.is_news_blackout)

            if not approved:
                logger.debug(f"[{agent.agent_id}] REJECTED: {reason}")
                return

            # Send to broker
            trade = await self._broker.execute(proposal)
            self._risk.on_order_opened(proposal)
            logger.info(f"[{agent.agent_id}] ORDER SENT: {trade.side.value} {trade.symbol.value} @ {trade.entry_price}")
            await self._broadcast({"type": "trade_opened", "data": _trade_payload(trade)})

        except Exception as exc:
            logger.warning(f"[{agent.agent_id}] Error during processing: {exc}")

    async def on_trade_closed_callback(self, trade: ExecutedTrade) -> None:
        """Called by broker adapter when a position is closed."""
        self._risk.on_trade_closed(trade)
        agent = self._agents.get(trade.agent_id)
        if agent:
            await agent.on_trade_closed(trade)
            logger.info(f"[{trade.agent_id}] Trade closed | PnL={trade.pnl:+.4f}")
            await self._broadcast({"type": "trade_closed", "data": _trade_payload(trade)})

        # Persisted independent of the in-memory agent lookup above — a
        # closed trade should still land in the DB even if its agent was
        # since removed from the registry.
        if self._repository:
            self._fire_and_forget(self._repository.save_trade(trade))

    def stop(self) -> None:
        self._running = False
        logger.info("[Swarm] Stop signal sent")

    # ─── Risk-state persistence ──────────────────────────────────────────────

    async def restore_risk_state(self) -> None:
        """Loads any persisted daily/total-loss halt state before the tick
        loop starts, so a process restart can never silently clear a halt.
        With repository=None (no DB configured), halt state simply doesn't
        survive restarts — consistent with this project's "DB is optional"
        philosophy elsewhere. Deliberately unguarded otherwise: a failure here
        should crash startup rather than silently proceed unhalted — see
        AsyncRepository.load_risk_state's docstring and ADR-0010."""
        if self._repository is None:
            return
        snapshot = await self._repository.load_risk_state()
        if snapshot is not None:
            self._risk.restore_state(snapshot)

    def _compute_total_equity(self) -> float:
        """Realized (sum of agent.equity) + floating (mark-to-market) — same
        formula as get_swarm_summary's total_equity. Kept as its own helper
        since the daily-loss halt check needs it every tick, independent of
        the full summary payload."""
        realized_equity = sum(a.get_metrics().equity for a in self._agents.values())
        return realized_equity + sum(self._floating_pnl.values())

    # ─── Floating (mark-to-market) PnL ─────────────────────────────────────────

    async def _compute_floating_pnl(self) -> dict[str, float]:
        """Unrealized PnL per agent from currently open positions, using each
        symbol's last known tick price. Same pct-of-notional formula as the
        offline broker's realized TP/SL close (see IBKRBroker.check_tp_sl) so
        floating and realized PnL stay on a consistent scale."""
        floating: dict[str, float] = {}
        try:
            open_positions = await self._broker.get_open_positions()
        except Exception as exc:
            logger.warning(f"[Swarm] get_open_positions failed: {exc}")
            return floating

        for trade in open_positions:
            price = self._last_price.get(trade.symbol)
            if price is None or not trade.entry_price:
                continue
            direction = 1 if trade.side == Side.LONG else -1
            pct_change = (price - trade.entry_price) / trade.entry_price
            floating[trade.agent_id] = floating.get(trade.agent_id, 0.0) + trade.quantity * pct_change * direction
        return floating

    def floating_pnl_for(self, agent_id: str) -> float:
        return round(self._floating_pnl.get(agent_id, 0.0), 4)

    # ─── Control panel methods ────────────────────────────────────────────────

    def pause_group(self, agent_type: AgentType) -> int:
        from swarm_trading.core.models import AgentStatus

        count = 0
        for a in self._agents.values():
            if a.agent_type == agent_type:
                a.status = AgentStatus.PAUSED
                count += 1
        logger.warning(f"[Swarm] Paused {count} agents of type {agent_type.value}")
        return count

    def get_swarm_summary(self) -> JSONDict:
        metrics = [a.get_metrics() for a in self._agents.values()]
        realized_equity = sum(m.equity for m in metrics)
        floating_pnl = round(sum(self._floating_pnl.values()), 4)
        total_equity = round(realized_equity + floating_pnl, 4)
        total_trades = sum(m.total_trades for m in metrics)
        active_count = sum(1 for m in metrics if m.current_status.value == "ACTIVE")
        retired_count = sum(1 for m in metrics if m.current_status.value == "RETIRED")
        avg_win_rate = sum(m.win_rate for m in metrics) / len(metrics) if metrics else 0
        uptime_seconds = (datetime.utcnow() - self._started_at).total_seconds() if self._started_at else 0
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "uptime_seconds": round(uptime_seconds),
            "total_equity": total_equity,
            "realized_equity": round(realized_equity, 4),
            "floating_pnl": floating_pnl,
            "initial_capital": settings.swarm_total_capital_usd,
            "pnl": round(total_equity - settings.swarm_total_capital_usd, 4),
            "total_trades": total_trades,
            "active_agents": active_count,
            "retired_agents": retired_count,
            "avg_win_rate": round(avg_win_rate, 4),
            "daily_pnl": round(self._risk.daily_pnl, 4),
            "is_halted": self._risk.is_halted,
            "halt_cause": self._risk.halt_cause,
        }
