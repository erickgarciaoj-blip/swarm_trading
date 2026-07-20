"""
Entry point — run with: python main.py
"""

import asyncio

import uvicorn
from loguru import logger

from swarm_trading.agents.templates.swarm_factory import build_swarm
from swarm_trading.brokers.adapters.broker_interface import BrokerInterface
from swarm_trading.brokers.ibkr.ibkr_broker import IBKRBroker
from swarm_trading.brokers.mt5.mt5_broker import MT5Broker
from swarm_trading.core.config import settings
from swarm_trading.core.orchestrator.orchestrator import SwarmOrchestrator
from swarm_trading.dashboard.api.routes import app as dashboard_app
from swarm_trading.dashboard.api.routes import set_orchestrator, set_repository
from swarm_trading.dashboard.websocket import ws_manager
from swarm_trading.data.feeds.market_feed import MarketFeed
from swarm_trading.data.historic.repository import AsyncRepository
from swarm_trading.data.news.news_feed import NewsFeed


async def main():
    logger.info(f"=== SWARM TRADING | mode={settings.app_env} ===")

    # ── Select broker based on env ─────────────────────────────────────
    # TODO(Fase 10): this couples broker choice to app_env — replaced by an
    # explicit BROKER_PROVIDER/BROKER_MODE selector (see ARCHITECTURE_REVIEW.md
    # §3.7). Left as if/else (not a ternary) so the explanatory comment below
    # stays attached to its branch.
    broker: BrokerInterface
    if settings.app_env == "live":  # noqa: SIM108
        broker = MT5Broker()
    else:
        broker = IBKRBroker(offline=True)  # no TWS/Gateway needed for paper/dev runs

    connected = await broker.connect()
    if not connected:
        logger.error("Broker connection failed. Running in data-only mode.")

    # ── Build data feeds ───────────────────────────────────────────────
    market_feed = MarketFeed(backend="yfinance")
    news_feed = NewsFeed(backend="demo")  # change to "forexfactory" in prod

    # ── Database is optional — a dead/unreachable DB must not stop the swarm ──
    repo: AsyncRepository | None = AsyncRepository(settings.database_url)
    try:
        # mypy widens `repo` to the full try block's eventual type (it
        # becomes AsyncRepository | None a few lines down in the except
        # branch) rather than narrowing to the assignment just above.
        assert repo is not None
        await repo.init()
    except Exception as exc:
        logger.error(f"[Main] Database unavailable, continuing without persistence: {exc}")
        repo = None

    # ── Build orchestrator and register agents ─────────────────────────
    orch = SwarmOrchestrator(broker=broker, market_feed=market_feed, news_feed=news_feed, repository=repo)
    orch.set_broadcaster(ws_manager.broadcast)
    # Deliberately unguarded — a restart must never silently clear a
    # persisted daily/total-loss halt. See ADR-0010 and
    # AsyncRepository.load_risk_state's docstring.
    await orch.restore_risk_state()
    total = await build_swarm(orch, repository=repo)
    logger.info(f"[Main] Swarm ready — {total} agents registered")

    # ── Expose orchestrator/repository to dashboard ────────────────────
    set_orchestrator(orch)
    set_repository(repo)

    # ── Run dashboard + swarm concurrently ────────────────────────────
    config = uvicorn.Config(
        dashboard_app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",  # "warning" silences uvicorn's own startup banner
    )
    server = uvicorn.Server(config)

    try:
        await asyncio.gather(
            orch.run(),
            server.serve(),
        )
    finally:
        if repo:
            await repo.close()


if __name__ == "__main__":
    asyncio.run(main())
