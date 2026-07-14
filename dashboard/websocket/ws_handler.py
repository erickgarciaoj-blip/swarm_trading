"""
WebSocket connection manager for the real-time dashboard feed.

Kept free of orchestrator-specific knowledge on purpose: it only knows how
to track connected clients and fan a JSON dict out to all of them. Anything
that decides *what* to send (periodic snapshots, event pushes) lives in
dashboard/api/routes.py or is injected via SwarmOrchestrator.set_broadcaster.
"""
from __future__ import annotations
import asyncio
from fastapi import WebSocket
from loguru import logger


class WebSocketManager:
    """Tracks connected dashboard clients and fans out JSON messages to all of them."""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"[WS] Client connected ({len(self._connections)} total)")

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"[WS] Client disconnected ({len(self._connections)} total)")

    async def broadcast(self, data: dict) -> None:
        async with self._lock:
            connections = list(self._connections)
        if not connections:
            return

        dead: list[WebSocket] = []
        for ws in connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
