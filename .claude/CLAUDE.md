# SWARM TRADING — Claude Code Context

## Project overview
This is a 100-agent autonomous trading swarm built in Python.
Each agent trades one of: XAUUSD, PLTR, NAS100, US100, OIL.
Agents are independent — no inter-agent communication. All coordination goes through `SwarmOrchestrator`.

## Architecture
```
main.py
  └─ SwarmOrchestrator (core/orchestrator/)
        ├─ MarketFeed      (data/feeds/)          ← prices + indicators
        ├─ NewsFeed        (data/news/)            ← economic calendar
        ├─ RiskEngine      (risk/engine/)          ← FTMO-style validation gate
        ├─ BrokerInterface (brokers/adapters/)     ← MT5 or IBKR
        └─ 100 Agents      (agents/)
              ├─ ScalperAgent
              ├─ SwingAgent
              ├─ NewsReactiveAgent
              └─ HedgerAgent
```

## Key rules — do NOT violate
1. NO agent talks directly to another agent.
2. ALL orders must pass through `RiskEngine.validate()` before reaching the broker.
3. Every agent has its own `equity` — never share capital between agents.
4. `settings` (core/config.py) is the single source of truth for all parameters.
5. Use `async/await` everywhere; this is a fully async codebase.
6. Log with `loguru`, not `print` or `logging`.

## Adding a new agent type
1. Create `agents/<name>/<name>_agent.py`
2. Inherit from `BaseAgent`
3. Implement `analyze()` and `on_trade_closed()`
4. Register in `agents/templates/swarm_factory.py`

## Adding a new broker
1. Create `brokers/<name>/<name>_broker.py`
2. Implement `BrokerInterface`
3. Wire in `main.py` switch

## Running
```bash
cp .env.example .env   # fill credentials
python main.py         # starts swarm + dashboard on :8000
```

## MCP server (for Claude Code tool use)
```bash
python core/mcp_server.py
```
Tools: get_swarm_summary, list_agents, halt_swarm, resume_swarm, pause_agent_type, get_agent_metrics

## Testing
```bash
pytest tests/ -v
```
