"""
SwarmMCPServer — MCP server that exposes swarm tools to Claude Code / LLM agents.
Tools available: get_market_state, get_news_events, submit_order, get_swarm_metrics,
                 halt_swarm, list_agents, get_agent_metrics.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("swarm-trading")


@server.list_tools()  # type: ignore[misc]  # mcp's decorator has no type stubs
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_swarm_summary",
            description="Returns overall swarm PnL, equity, trade count and status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_agents",
            description="Lists all agents with id, type, symbol, equity, status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="halt_swarm",
            description="Immediately halt all trading.",
            inputSchema={"type": "object", "properties": {"reason": {"type": "string"}}},
        ),
        Tool(
            name="resume_swarm",
            description="Resume trading after halt.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="pause_agent_type",
            description="Pause all agents of a given type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_type": {"type": "string", "enum": ["SCALPER", "SWING", "NEWS_REACTIVE", "HEDGER"]}
                },
                "required": ["agent_type"],
            },
        ),
        Tool(
            name="get_agent_metrics",
            description="Get metrics for a specific agent.",
            inputSchema={
                "type": "object",
                "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"],
            },
        ),
    ]


@server.call_tool()  # type: ignore[misc]  # mcp's decorator has no type stubs
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Import here to avoid circular deps at module load time
    from swarm_trading.dashboard.api.routes import _orchestrator as orch

    if name == "get_swarm_summary":
        data = orch.get_swarm_summary() if orch else {"error": "not initialized"}
    elif name == "list_agents":
        data = (
            [
                {
                    "id": a.agent_id,
                    "type": a.agent_type.value,
                    "symbol": a.symbol.value,
                    "equity": a.equity,
                    "status": a.status.value,
                }
                for a in orch._agents.values()
            ]
            if orch
            else []
        )
    elif name == "halt_swarm":
        if orch:
            await orch._risk.halt(arguments.get("reason", "mcp_halt"))
        data = {"halted": True}
    elif name == "resume_swarm":
        if orch:
            await orch._risk.resume()
        data = {"halted": False}
    elif name == "pause_agent_type":
        from swarm_trading.core.models import AgentType

        atype = AgentType(arguments["agent_type"])
        count = orch.pause_group(atype) if orch else 0
        data = {"paused_count": count}
    elif name == "get_agent_metrics":
        agent = orch._agents.get(arguments["agent_id"]) if orch else None
        data = agent.get_metrics().__dict__ if agent else {"error": "not found"}
    else:
        data = {"error": f"unknown tool {name}"}

    return [TextContent(type="text", text=json.dumps(data, default=str))]


async def run_mcp():
    async with stdio_server() as streams:
        await server.run(
            streams[0],
            streams[1],
            InitializationOptions(
                server_name="swarm-trading",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(run_mcp())
