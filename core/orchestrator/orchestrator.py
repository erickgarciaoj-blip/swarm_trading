"""
SwarmOrchestrator — the MCP-layer brain.
Coordinates all 100 agents, data feeds, risk validation and broker execution.
Designed to run as a single async loop; agents are coroutines, not threads.
"""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from typing import Awaitable, Callable, TYPE_CHECKING

from swarm_trading.core.config import settings
from swarm_trading.core.models import AgentType, MarketState, Symbol, ExecutedTrade, OrderStatus
from swarm_trading.risk.engine.risk_engine import RiskEngine

Broadcaster = Callable[[dict], Awaitable[None]]


async def _noop_broadcaster(_message: dict) -> None:
    pass


def _trade_payload(trade: ExecutedTrade) -> dict:
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
    from swarm_trading.data.news.news_feed import NewsFeed
    from swarm_trading.data.historic.repository import AsyncRepository


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
        broker: "BrokerInterface",
        market_feed: "MarketFeed",
        news_feed: "NewsFeed",
        repository: "AsyncRepository | None" = None,
    ):
        self._agents: dict[str, "BaseAgent"] = {}
        self._broker = broker
        self._market_feed = market_feed
        self._news_feed = news_feed
        self._repository = repository
        self._risk = RiskEngine()
        self._running = False
        self._broadcast: Broadcaster = _noop_broadcaster
        # Fire-and-forget DB writes (save_trade) must be kept referenced
        # until they finish, or asyncio may garbage-collect them mid-flight.
        self._background_tasks: set[asyncio.Task] = set()

    def _fire_and_forget(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def set_broadcaster(self, fn: Broadcaster) -> None:
        """Wire a transport (e.g. dashboard/websocket's ConnectionManager.broadcast)
        without the orchestrator importing FastAPI/Starlette types itself."""
        self._broadcast = fn

    # ─── Agent management ────────────────────────────────────────────────────

    def register_agent(self, agent: "BaseAgent") -> None:
        self._agents[agent.agent_id] = agent
        logger.info(f"[Swarm] Registered {agent}")

    def remove_agent(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    @property
    def active_agents(self) -> list["BaseAgent"]:
        return [a for a in self._agents.values() if a.is_alive]

    # ─── Main loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main event loop. Fetches market state every tick and dispatches to agents."""
        self._running = True
        logger.info(f"[Swarm] Starting with {len(self._agents)} agents")

        while self._running:
            try:
                for symbol in Symbol:
                    state: MarketState = await self._market_feed.get_state(symbol)
                    news_events        = await self._news_feed.get_upcoming(symbol)
                    state.upcoming_news = news_events

                    # Resolve TP/SL against this tick's price before dispatching new
                    # signals — frees up SYMBOL_CONCENTRATION slots for brokers (e.g.
                    # offline mode) that don't manage TP/SL server-side themselves.
                    if state.candles:
                        closed_trades = await self._broker.check_tp_sl(symbol, state.candles[-1].close)
                        for trade in closed_trades:
                            await self.on_trade_closed_callback(trade)

                    # Dispatch all active agents for this symbol concurrently
                    symbol_agents = [a for a in self.active_agents if a.symbol == symbol]
                    await asyncio.gather(*[
                        self._process_agent(agent, state) for agent in symbol_agents
                    ], return_exceptions=True)

                await self._broadcast({"type": "swarm_summary", "data": self.get_swarm_summary()})
                await asyncio.sleep(60)  # tick every 60s — adjust per strategy

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[Swarm] Orchestrator error: {exc}")
                await asyncio.sleep(5)

        logger.info("[Swarm] Stopped")

    async def _process_agent(self, agent: "BaseAgent", state: MarketState) -> None:
        try:
            proposal = await agent.analyze(state)
            if proposal is None:
                return

            metrics = agent.get_metrics()
            approved, reason = self._risk.validate(
                proposal, metrics, is_news_blackout=state.is_news_blackout
            )

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

    def get_swarm_summary(self) -> dict:
        metrics = [a.get_metrics() for a in self._agents.values()]
        total_equity   = sum(m.equity for m in metrics)
        total_trades   = sum(m.total_trades for m in metrics)
        active_count   = sum(1 for m in metrics if m.current_status.value == "ACTIVE")
        retired_count  = sum(1 for m in metrics if m.current_status.value == "RETIRED")
        avg_win_rate   = sum(m.win_rate for m in metrics) / len(metrics) if metrics else 0
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "total_equity": round(total_equity, 4),
            "initial_capital": settings.swarm_total_capital_usd,
            "pnl": round(total_equity - settings.swarm_total_capital_usd, 4),
            "total_trades": total_trades,
            "active_agents": active_count,
            "retired_agents": retired_count,
            "avg_win_rate": round(avg_win_rate, 4),
            "daily_pnl": round(self._risk.daily_pnl, 4),
            "is_halted": self._risk.is_halted,
        }
