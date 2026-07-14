"""Unit tests for the dashboard's WebSocketManager (dashboard/websocket/ws_handler.py)."""
import pytest
from swarm_trading.dashboard.websocket.ws_handler import WebSocketManager


class _FakeWebSocket:
    def __init__(self, fail_send=False):
        self.accepted = False
        self.sent: list[dict] = []
        self._fail_send = fail_send

    async def accept(self):
        self.accepted = True

    async def send_json(self, message: dict):
        if self._fail_send:
            raise RuntimeError("connection reset")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_connect_accepts_and_registers():
    manager = WebSocketManager()
    ws = _FakeWebSocket()
    await manager.connect(ws)
    assert ws.accepted
    assert manager.connection_count == 1


@pytest.mark.asyncio
async def test_disconnect_removes_client():
    manager = WebSocketManager()
    ws = _FakeWebSocket()
    await manager.connect(ws)
    await manager.disconnect(ws)
    assert manager.connection_count == 0


@pytest.mark.asyncio
async def test_broadcast_fans_out_to_all_clients():
    manager = WebSocketManager()
    ws1, ws2 = _FakeWebSocket(), _FakeWebSocket()
    await manager.connect(ws1)
    await manager.connect(ws2)

    await manager.broadcast({"type": "swarm_summary", "data": {"total_equity": 42}})

    assert ws1.sent == [{"type": "swarm_summary", "data": {"total_equity": 42}}]
    assert ws2.sent == ws1.sent


@pytest.mark.asyncio
async def test_broadcast_drops_dead_connections():
    manager = WebSocketManager()
    healthy = _FakeWebSocket()
    broken = _FakeWebSocket(fail_send=True)
    await manager.connect(healthy)
    await manager.connect(broken)

    await manager.broadcast({"type": "ping"})

    assert healthy.sent == [{"type": "ping"}]
    assert manager.connection_count == 1  # broken one pruned


@pytest.mark.asyncio
async def test_broadcast_with_no_clients_is_a_noop():
    manager = WebSocketManager()
    await manager.broadcast({"type": "swarm_summary"})  # must not raise
