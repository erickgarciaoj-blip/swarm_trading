"""Unit tests for /health (liveness) and /health/ready (readiness).

Uses FastAPI's TestClient against a fake repository — no real DB needed,
mirrors the pattern in test_routes_agents.py.
"""

import pytest
from fastapi.testclient import TestClient

from swarm_trading.dashboard.api.routes import app, set_repository


class _FakeRepository:
    def __init__(self, ready: bool):
        self._ready = ready

    async def is_ready(self) -> bool:
        return self._ready


@pytest.fixture(autouse=True)
def _reset_globals():
    yield
    set_repository(None)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health_live_returns_ok_without_any_repository(client):
    # No set_repository() call at all — liveness must not depend on it.
    res = client.get("/health")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_ready_returns_ok_when_repository_is_ready(client):
    set_repository(_FakeRepository(ready=True))

    res = client.get("/health/ready")

    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_ready_returns_503_when_repository_not_ready(client):
    set_repository(_FakeRepository(ready=False))

    res = client.get("/health/ready")

    assert res.status_code == 503
    assert res.json() == {"status": "not_ready"}


def test_health_ready_returns_503_when_repository_is_none(client):
    res = client.get("/health/ready")

    assert res.status_code == 503
    assert res.json() == {"status": "not_ready"}


def test_health_ready_body_never_leaks_driver_or_connection_detail(client):
    """The 503 body must stay generic — no exception text, no DATABASE_URL,
    no driver-specific error message. See AsyncRepository.is_ready()."""
    set_repository(_FakeRepository(ready=False))

    res = client.get("/health/ready")

    assert set(res.json().keys()) == {"status"}
