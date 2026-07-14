"""
Unit tests for the dashboard's /agents/{id}/metrics and /agents/{id}/trades
routes, using FastAPI's TestClient against a fake orchestrator/repository
(no real SwarmOrchestrator/DB needed — routes.py only ever touches
`_orchestrator._agents` and `_repository.get_agent_trades`).
"""
import pytest
from fastapi.testclient import TestClient

from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
from swarm_trading.core.models import Symbol
from swarm_trading.dashboard.api.routes import app, set_orchestrator, set_repository


class _FakeOrchestrator:
    def __init__(self, agents: dict):
        self._agents = agents


class _FakeRepository:
    def __init__(self, trades_by_agent: dict):
        self._trades_by_agent = trades_by_agent
        self.last_limit = "unset"

    async def get_agent_trades(self, agent_id, limit=None):
        self.last_limit = limit
        trades = self._trades_by_agent.get(agent_id, [])
        return trades[:limit] if limit is not None else trades


@pytest.fixture(autouse=True)
def _reset_globals():
    """routes.py keeps _orchestrator/_repository as module globals — reset
    them after every test so tests don't leak state into each other."""
    yield
    set_orchestrator(None)
    set_repository(None)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _agent():
    return ScalperAgent(symbol=Symbol.XAUUSD, initial_capital=5.0)


# ─── /agents/{id}/metrics ──────────────────────────────────────────────────

def test_agent_metrics_returns_metrics_for_known_agent(client):
    agent = _agent()
    set_orchestrator(_FakeOrchestrator({agent.agent_id: agent}))

    res = client.get(f"/agents/{agent.agent_id}/metrics")

    assert res.status_code == 200
    body = res.json()
    assert body["agent_id"] == agent.agent_id
    assert body["equity"] == 5.0
    assert body["status"] == "ACTIVE"
    assert set(body) == {
        "agent_id", "equity", "win_rate", "sharpe", "max_drawdown", "total_trades", "status",
    }


def test_agent_metrics_returns_error_for_unknown_agent(client):
    set_orchestrator(_FakeOrchestrator({}))

    res = client.get("/agents/does-not-exist/metrics")

    assert res.status_code == 200
    assert res.json() == {"error": "Agent not found"}


def test_agent_metrics_without_orchestrator_returns_empty_dict(client):
    res = client.get("/agents/whatever/metrics")
    assert res.json() == {}


# ─── /agents/{id}/trades ────────────────────────────────────────────────────

def test_agent_trades_defaults_to_last_10(client):
    trades = [{"trade_id": f"t{i}", "pnl": i} for i in range(15)]
    fake_repo = _FakeRepository({"agent_1": trades})
    set_repository(fake_repo)

    res = client.get("/agents/agent_1/trades")

    assert res.status_code == 200
    assert len(res.json()) == 10
    assert fake_repo.last_limit == 10


def test_agent_trades_respects_limit_query_param(client):
    trades = [{"trade_id": f"t{i}", "pnl": i} for i in range(15)]
    fake_repo = _FakeRepository({"agent_1": trades})
    set_repository(fake_repo)

    res = client.get("/agents/agent_1/trades?limit=3")

    assert res.status_code == 200
    assert len(res.json()) == 3
    assert fake_repo.last_limit == 3


def test_agent_trades_returns_empty_list_for_unknown_agent(client):
    set_repository(_FakeRepository({}))

    res = client.get("/agents/nobody/trades")

    assert res.status_code == 200
    assert res.json() == []


def test_agent_trades_returns_empty_list_when_repository_is_none(client):
    res = client.get("/agents/agent_1/trades")

    assert res.status_code == 200
    assert res.json() == []
