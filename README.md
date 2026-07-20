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

## Infrastructure (Docker Compose)

Everything — Postgres, Redis, the migration gate, the app itself, and an
optional reverse proxy — is one stack, brought up with one command:

```bash
cp .env.example .env    # fill in POSTGRES_PASSWORD at minimum
make docker-up          # docker compose up -d --wait
```

There is no manual ordering to remember: `docker-compose.yml`'s own
`depends_on`/`condition` graph brings up `postgres`, waits for it to be
healthy, runs `migrate` (`alembic upgrade head`) to completion, and only then
starts `swarm` — a failing migration blocks `swarm` from ever starting (see
[ADR-0009](docs/architecture/adr/0009-docker-stack-gate-liveness-readiness.md)).
`--wait` makes `make docker-up` itself block until `swarm` reports healthy,
instead of returning as soon as containers exist.

| Service | Purpose | Exposed on host? |
|---|---|---|
| `postgres` | Runtime database, schema owned by Alembic | No — internal to `swarm_net` only |
| `redis` | Ephemeral cache/pub-sub, not yet used by app code (see ARCHITECTURE_REVIEW.md Fase 5) | No |
| `migrate` | One-shot: applies pending migrations, gates `swarm` | N/A (runs to completion, doesn't stay up) |
| `swarm` | The app + dashboard | `127.0.0.1:8000` (localhost only — no auth on the dashboard) |
| `nginx` | Optional reverse proxy (HTTP + WebSocket), opt-in via `profiles: [proxy]` | `127.0.0.1:8080` (only when enabled) |

```bash
make docker-up          # postgres + redis + migrate + swarm
make docker-up-proxy    # same, plus nginx in front of swarm (127.0.0.1:8080)
make docker-down        # stop everything
make docker-logs        # follow swarm's logs
make docker-shell       # shell inside the running swarm container
```

The dashboard has no authentication and `/swarm/halt` is an unauthenticated
POST — reach it over an SSH tunnel rather than exposing the port publicly:

```bash
ssh -L 8000:localhost:8000 <user>@<vps-ip>
# then open http://localhost:8000/frontend/ on your own machine
```

`nginx` (profile `proxy`) doesn't change that guidance — it terminates
HTTP/WebSocket connections and forwards real client headers, but it has no
TLS yet (see `nginx/nginx.conf`'s commented-out `443` block and ADR-0009 for
what's needed to turn it on).

### Environment variables

Every variable is documented inline in `.env.example`; the ones actually
required to bring the stack up are:

| Variable | Required for | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | `docker compose` to parse at all | No default — compose refuses to start rather than run with a blank password |
| `DATABASE_URL` | `swarm`, `migrate` | Points at the `postgres` service by hostname when run via Compose |
| `APP_ENV` | schema-safety check | `paper`/`live` reject a `sqlite://` `DATABASE_URL` at startup |
| `REDIS_URL` | nothing yet | Provisioned ahead of Fase 5; safe to leave at its default |

### Backups

```bash
make db-backup                              # timestamped file under backups/
make db-backup file=backups/before-migration.sql.gz
make db-restore file=backups/before-migration.sql.gz
```

Both fail loudly on any error, never print `POSTGRES_PASSWORD` (the dump/
restore runs inside the `postgres` container over its local socket, which
trusts local connections without a password), `db-backup` refuses to
overwrite an existing file, and both validate the archive (non-empty,
passes `gzip -t`) before calling the operation a success.

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
# Brings up postgres, applies migrations, then starts the app — the
# ordering and the migration gate are enforced by docker-compose.yml itself
# (see "Infrastructure" above and ADR-0009), not by running steps by hand.
make docker-up

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

Via `make docker-up`/`docker compose up`, this can't happen — `swarm` is
gated on `migrate` completing successfully (see "Infrastructure" above).
Outside Docker (running `main.py` directly against a real Postgres), the
gate doesn't exist: if you land on a database with **no migrations
applied**, the app will start (connectivity is fine) but every query will
fail or return empty — that's the intended, visible failure mode. Point
`DATABASE_URL` (see `.env.example`) at your Postgres instance and run
`alembic upgrade head` directly before expecting reads/writes to work.

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
