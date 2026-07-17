"""
FastAPI dashboard — exposes swarm metrics and control endpoints.
Frontend (dashboard/frontend/index.html) connects via REST + WebSocket.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.dashboard.websocket import ws_manager
from swarm_trading.data.feeds.market_hours import market_status_by_symbol

app = FastAPI(title="Swarm Trading Dashboard", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

SNAPSHOT_INTERVAL_SEC = 1
SNAPSHOT_PERSIST_EVERY = 60  # ticks of SNAPSHOT_INTERVAL_SEC ≈ once a minute

# Global references, set at startup by main.py
_orchestrator = None
_repository = None


def set_orchestrator(orch) -> None:
    global _orchestrator
    _orchestrator = orch


def set_repository(repo) -> None:
    global _repository
    _repository = repo


@app.get("/health")
async def health():
    """Liveness only — the process is up and answering HTTP. Deliberately
    never touches Postgres/Redis/yfinance/brokers: an external service being
    down must not make Docker think *this* container is dead and restart it.
    See /health/ready for the readiness check, and ADR-0009 for why those two
    are kept separate."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness — confirms the app can actually serve traffic (Postgres
    reachable), not just that the process is alive. Body deliberately stays
    generic (no driver error text, no connection details) — the real cause
    is only logged internally (see AsyncRepository.is_ready())."""
    if _repository is None or not await _repository.is_ready():
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ok"}


@app.get("/swarm/summary")
async def swarm_summary():
    if not _orchestrator:
        return {"error": "Orchestrator not initialized"}
    return _orchestrator.get_swarm_summary()


@app.get("/agents")
async def list_agents():
    if not _orchestrator:
        return []
    return [
        {
            "id": a.agent_id,
            "type": a.agent_type.value,
            "symbol": a.symbol.value,
            "equity": a.equity,
            "status": a.status.value,
        }
        for a in _orchestrator._agents.values()
    ]


@app.get("/agents/{agent_id}/metrics")
async def agent_metrics(agent_id: str):
    if not _orchestrator:
        return {}
    a = _orchestrator._agents.get(agent_id)
    if not a:
        return {"error": "Agent not found"}
    m = a.get_metrics()
    floating = _orchestrator.floating_pnl_for(agent_id)
    return {
        "agent_id": m.agent_id,
        "equity": round(m.equity + floating, 4),
        "realized_equity": m.equity,
        "floating_pnl": floating,
        "win_rate": m.win_rate,
        "sharpe": m.sharpe,
        "max_drawdown": m.max_drawdown,
        "total_trades": m.total_trades,
        "status": m.current_status.value,
    }


@app.get("/agents/{agent_id}/trades")
async def agent_trades(agent_id: str, limit: int = 10):
    if not _repository:
        return []
    return await _repository.get_agent_trades(agent_id, limit=limit)


@app.get("/agents/{agent_id}/equity_curve")
async def agent_equity_curve(agent_id: str):
    if not _repository:
        return []
    return await _repository.get_agent_equity_curve(agent_id, settings.swarm_capital_per_agent)


@app.get("/swarm/history")
async def swarm_history(limit: int | None = None):
    """Full swarm-equity history since inception (or last `limit` snapshots),
    oldest-first — backs the dashboard's full-history equity chart."""
    if not _repository:
        return []
    return await _repository.get_recent_snapshots(limit=limit)


@app.post("/swarm/halt")
async def halt_swarm():
    if _orchestrator:
        _orchestrator._risk.halt("manual via dashboard")
    return {"halted": True}


@app.post("/swarm/resume")
async def resume_swarm():
    if _orchestrator:
        _orchestrator._risk.resume()
    return {"halted": False}


def _build_snapshot() -> dict[str, Any]:
    if not _orchestrator:
        return {"status": "waiting"}

    # All agents, mark-to-market equity descending — the dashboard table shows
    # the full swarm (not just a "top N" slice) and scrolls internally.
    agents_payload = []
    for a in _orchestrator._agents.values():
        m = a.get_metrics()
        floating = _orchestrator.floating_pnl_for(a.agent_id)
        agents_payload.append(
            {
                "id": m.agent_id,
                "type": a.agent_type.value,
                "symbol": a.symbol.value,
                "equity": round(m.equity + floating, 4),
                "realized_equity": round(m.equity, 4),
                "floating_pnl": floating,
                "win_rate": round(m.win_rate, 4),
                "status": m.current_status.value,
            }
        )
    agents_payload.sort(key=lambda a: a["equity"], reverse=True)

    last_trades = [
        {
            "trade_id": t.trade_id,
            "agent_id": t.agent_id,
            "symbol": t.symbol.value,
            "side": t.side.value,
            "pnl": round(t.pnl, 4),
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in _orchestrator._risk.recent_trades
    ]

    now = datetime.utcnow()
    market_status = market_status_by_symbol(now)
    return {
        "timestamp": now.isoformat(),
        "app_env": settings.app_env,
        # True if at least one tracked market is open — drives the header's
        # single overall badge. Per-symbol accuracy lives in market_status.
        "market_open": any(market_status.values()),
        "market_status": market_status,
        "swarm": _orchestrator.get_swarm_summary(),
        "agents": agents_payload,
        "last_trades": last_trades,
    }


async def _snapshot_broadcaster() -> None:
    """Ticks once per second, pushing one snapshot to every connected client.
    Every SNAPSHOT_PERSIST_EVERY ticks (~once a minute), also persists a
    SwarmSnapshot row if a repository is wired."""
    tick = 0
    while True:
        try:
            await ws_manager.broadcast(_build_snapshot())
            tick += 1
            if _repository and _orchestrator and tick % SNAPSHOT_PERSIST_EVERY == 0:
                await _repository.save_snapshot(_orchestrator.get_swarm_summary())
        except Exception as exc:
            logger.warning(f"[WS] snapshot broadcast error: {exc}")
        await asyncio.sleep(SNAPSHOT_INTERVAL_SEC)


@app.on_event("startup")
async def _start_snapshot_broadcaster() -> None:
    app.state.snapshot_task = asyncio.create_task(_snapshot_broadcaster())


@app.on_event("shutdown")
async def _stop_snapshot_broadcaster() -> None:
    task = getattr(app.state, "snapshot_task", None)
    if task:
        task.cancel()


@app.websocket("/ws")
async def swarm_websocket(websocket: WebSocket):
    """Real-time feed: a full snapshot (swarm summary, top agents, last trades)
    pushed to every connected client once per second by _snapshot_broadcaster."""
    await ws_manager.connect(websocket)
    try:
        await websocket.send_json(_build_snapshot())  # don't make the client wait up to 1s for first paint
        while True:
            # Clients don't need to send anything; this just detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
