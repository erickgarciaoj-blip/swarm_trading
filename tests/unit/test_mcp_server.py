"""
Unit tests for core/mcp_server.py — previously had zero coverage. Includes a
regression test for the InitializationOptions fix found during Fase 2's
mypy pass: run_mcp() used to construct InitializationOptions without the
`capabilities` argument the installed mcp==1.2.0 actually requires, which
would have raised at the very first real MCP client connection (this only
became visible once `mcp` was actually installed in the venv — see
ADR-0005 — mypy had been treating the whole package as untyped Any before
that, so it couldn't catch a missing required argument).
"""

from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool

import swarm_trading.core.mcp_server as mcp_server_module
from swarm_trading.core.mcp_server import call_tool, list_tools, server


def test_list_tools_returns_all_six_tools():
    tools = _run(list_tools())
    names = {t.name for t in tools}
    assert names == {
        "get_swarm_summary",
        "list_agents",
        "halt_swarm",
        "resume_swarm",
        "pause_agent_type",
        "get_agent_metrics",
    }
    assert all(isinstance(t, Tool) for t in tools)


def test_initialization_options_construction_does_not_raise():
    """Regression test for the missing `capabilities` argument — this is
    the exact call run_mcp() makes; constructing it is what used to raise
    `TypeError: missing 1 required keyword-only argument: 'capabilities'`."""
    options = InitializationOptions(
        server_name="swarm-trading",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    assert options.server_name == "swarm-trading"
    assert options.capabilities is not None


class _FakeAgent:
    def __init__(self, agent_id, agent_type, symbol, equity=1000.0, status="ACTIVE"):
        self.agent_id = agent_id
        self.agent_type = type("T", (), {"value": agent_type})()
        self.symbol = type("S", (), {"value": symbol})()
        self.equity = equity
        self.status = type("St", (), {"value": status})()

    def get_metrics(self):
        return type("M", (), {"__dict__": {"agent_id": self.agent_id, "equity": self.equity}})()


class _FakeRiskEngine:
    def __init__(self):
        self.halted_reason = None
        self.resumed = False

    async def halt(self, reason):
        self.halted_reason = reason

    async def resume(self):
        self.resumed = True


class _FakeOrchestrator:
    def __init__(self, agents=None):
        self._agents = agents or {}
        self._risk = _FakeRiskEngine()

    def get_swarm_summary(self):
        return {"total_equity": 100000.0, "total_trades": 5}

    def pause_group(self, agent_type):
        return sum(1 for a in self._agents.values() if a.agent_type.value == agent_type.value)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _call(name, arguments, orch, monkeypatch):
    monkeypatch.setattr(mcp_server_module, "_orchestrator", orch, raising=False)

    # call_tool() imports `_orchestrator` from dashboard.api.routes inside
    # its own body — patch it there too, matching where it actually reads from.
    import swarm_trading.dashboard.api.routes as routes_module

    monkeypatch.setattr(routes_module, "_orchestrator", orch)
    return _run(call_tool(name, arguments))


def test_call_tool_get_swarm_summary(monkeypatch):
    orch = _FakeOrchestrator()
    result = _call("get_swarm_summary", {}, orch, monkeypatch)
    assert "100000.0" in result[0].text


def test_call_tool_list_agents(monkeypatch):
    agent = _FakeAgent("a1", "SCALPER", "XAUUSD")
    orch = _FakeOrchestrator(agents={"a1": agent})
    result = _call("list_agents", {}, orch, monkeypatch)
    assert '"id": "a1"' in result[0].text


def test_call_tool_halt_swarm(monkeypatch):
    orch = _FakeOrchestrator()
    result = _call("halt_swarm", {"reason": "manual test"}, orch, monkeypatch)
    assert orch._risk.halted_reason == "manual test"
    assert '"halted": true' in result[0].text


def test_call_tool_resume_swarm(monkeypatch):
    orch = _FakeOrchestrator()
    result = _call("resume_swarm", {}, orch, monkeypatch)
    assert orch._risk.resumed is True
    assert '"halted": false' in result[0].text


def test_call_tool_pause_agent_type(monkeypatch):
    agent = _FakeAgent("a1", "SCALPER", "XAUUSD")
    orch = _FakeOrchestrator(agents={"a1": agent})
    result = _call("pause_agent_type", {"agent_type": "SCALPER"}, orch, monkeypatch)
    assert '"paused_count": 1' in result[0].text


def test_call_tool_get_agent_metrics_unknown_agent(monkeypatch):
    orch = _FakeOrchestrator()
    result = _call("get_agent_metrics", {"agent_id": "nope"}, orch, monkeypatch)
    assert "not found" in result[0].text


def test_call_tool_unknown_tool_name(monkeypatch):
    orch = _FakeOrchestrator()
    result = _call("does_not_exist", {}, orch, monkeypatch)
    assert "unknown tool" in result[0].text


def test_call_tool_when_orchestrator_not_initialized(monkeypatch):
    result = _call("get_swarm_summary", {}, None, monkeypatch)
    assert "not initialized" in result[0].text
