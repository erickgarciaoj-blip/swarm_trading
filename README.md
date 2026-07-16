# 🐝 Swarm Trading — 100 Independent AI Agents

> Each agent starts with **$1 and targets $10**. All 100 agents run concurrently,
> independently, and safely — a single agent failure never cascades to others.

---

## Architecture

```
main.py
  └── SwarmOrchestrator
        ├── MarketFeed     → prices (XAUUSD, PLTR, NAS100, US100, OIL)
        ├── NewsFeed       → economic calendar (NFP, CPI, FOMC...)
        ├── RiskEngine     → FTMO-style gate (blocks bad orders)
        ├── BrokerAdapter  → MT5 (live) | IBKR (paper)
        └── 100 Agents
              ├── 30 Scalpers      (RSI + ATR, 1m–5m)
              ├── 30 Swing Traders (trend + EMA, 15m–1h)
              ├── 20 News Reactive (momentum on high-impact events)
              └── 10 Hedgers       (correlation risk reduction)
```

## Quick Start

```bash
# 1. Setup
bash scripts/setup.sh

# 2. Configure credentials
nano .env

# 3. Run swarm (paper mode by default)
make run

# 4. Open dashboard
open http://localhost:8000/docs
```

## MCP Server (Claude Code integration)

```bash
python core/mcp_server.py
```

Available tools: `get_swarm_summary`, `list_agents`, `halt_swarm`,
`resume_swarm`, `pause_agent_type`, `get_agent_metrics`

## Dashboard endpoints

| Endpoint | Description |
|---|---|
| `GET /swarm/summary` | Global PnL, equity, trade count |
| `GET /agents` | All 100 agents status |
| `GET /agents/{id}/metrics` | Per-agent metrics |
| `POST /swarm/halt` | Emergency stop |
| `POST /swarm/resume` | Resume after halt |

## Risk rules (FTMO-style)

- Max daily loss: 5% of total capital
- Max total drawdown: 10% of total capital
- Max agents per symbol: 10
- News blackout: ±5 min around HIGH impact events
- Individual agent drawdown limit: 10%

## Adding a new agent type

```python
# 1. Create agents/my_strategy/my_agent.py
class MyAgent(BaseAgent):
    async def analyze(self, state: MarketState) -> OrderProposal | None: ...
    async def on_trade_closed(self, trade: ExecutedTrade) -> None: ...

# 2. Register in agents/templates/swarm_factory.py
```

## Running tests

```bash
make test
```
