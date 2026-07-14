from swarm_trading.dashboard.websocket.ws_handler import WebSocketManager

# Single shared manager: the FastAPI /ws route registers clients on it,
# main.py wires SwarmOrchestrator to broadcast through it.
ws_manager = WebSocketManager()

__all__ = ["WebSocketManager", "ws_manager"]
