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

## Database (PostgreSQL + Alembic)

PostgreSQL is the real runtime database (required whenever `APP_ENV` is
`paper` or `live` — the app fails fast at startup otherwise, see
`core/config.py`). SQLite is only for local `APP_ENV=development` and
`tests/unit/*` — it's never treated as production-equivalent. See
[ADR-0008](docs/architecture/adr/0008-postgresql-alembic-schema-authority.md)
for the full reasoning.

**Alembic is the only thing that ever creates or changes schema.** The app
itself (`AsyncRepository.init()`) only checks connectivity at startup — it
never runs `CREATE TABLE`. There is no scenario where `create_all()` (or any
equivalent auto-create) should be called in application code again.

```bash
# Bring up Postgres + apply migrations + start the app, in that order:
make docker-up

# Or step by step:
docker compose up -d postgres          # starts Postgres, waits for pg_isready
docker compose run --rm migrate        # applies `alembic upgrade head`
docker compose up -d swarm             # only now start the app

# Check migration status:
docker compose run --rm swarm alembic current   # revision currently applied
docker compose run --rm swarm alembic check     # models vs. migrations drift

# Create a new migration after changing data/historic/db_models.py:
make makemigrations msg="describe the schema change"

# If a migration fails, revert to a previous revision (each migration keeps
# a working downgrade()):
docker compose run --rm swarm alembic downgrade <previous_revision>
```

See [ADR-0008's "Rollback operativo"](docs/architecture/adr/0008-postgresql-alembic-schema-authority.md#rollback-operativo)
for the full operational rollback procedure (reverting code vs. reverting
schema, what to do when a migration fails partway).

If you land on a database with **no migrations applied** (a fresh Postgres,
or one Alembic has never touched), the app will start (connectivity is
fine) but every query will fail or return empty — that's the intended,
visible failure mode. Run `make migrate` (or `alembic upgrade head`) before
expecting reads/writes to work.

Outside Docker, point `DATABASE_URL` (see `.env.example`) at any reachable
PostgreSQL instance and run `alembic upgrade head` directly.

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
make test              # unit tests — fast, no external services, no Postgres needed
make test-integration  # requires a real PostgreSQL (see `make docker-up` above)
```

Integration tests (`tests/integration/`) are marked `@pytest.mark.integration`
and excluded from `make test`'s default run (see `pyproject.toml`'s
`addopts`). They exercise things a SQLite-backed unit test can't prove:
real upsert conflict resolution, connection pooling, and persistence across
reconnects against actual PostgreSQL — never simulated with SQLite.
