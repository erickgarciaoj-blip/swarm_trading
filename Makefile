.PHONY: install run mcp test test-integration coverage lint format typecheck precommit clean \
	docker-build docker-up docker-up-proxy docker-down docker-restart docker-logs docker-shell \
	migrate makemigrations db-backup db-restore

install:
	python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pre-commit install

run:
	.venv/bin/python main.py

mcp:
	.venv/bin/python core/mcp_server.py

test:
	.venv/bin/pytest tests/ -v

# Requires a real PostgreSQL — see docker-up/migrate below, or
# docs/architecture/adr/0008. Never runs against SQLite.
test-integration:
	.venv/bin/pytest tests/integration/ -m integration -v --no-cov

coverage:
	.venv/bin/pytest tests/ --cov=. --cov-report=term-missing --cov-report=html

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .
	.venv/bin/ruff check --fix .

typecheck:
	.venv/bin/mypy --config-file pyproject.toml .

precommit:
	.venv/bin/pre-commit run --all-files

clean:
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

docker-build:
	docker compose build

# Postgres -> migrate -> swarm ordering is now encoded directly in
# docker-compose.yml's depends_on/condition graph (migrate is a required
# gate, not an opt-in profile — see ADR-0009), so a single `up` does
# everything the old three-command version did by hand. --wait blocks until
# "swarm" reports healthy rather than returning as soon as containers start.
# "nginx" stays excluded (profile "proxy") — use docker-up-proxy for that.
docker-up:
	docker compose up -d --wait

# Same as docker-up, plus nginx in front of "swarm" (profile "proxy" — see
# nginx/nginx.conf). Still no TLS; 127.0.0.1:8000 keeps working directly too.
docker-up-proxy:
	docker compose --profile proxy up -d --wait

docker-down:
	docker compose down

docker-restart:
	docker compose restart swarm

docker-logs:
	docker compose logs -f swarm

docker-shell:
	docker compose exec swarm /bin/bash

# Applies pending migrations. Never `alembic upgrade head` run from inside
# the app itself — see data/historic/repository.py::init(). Runs
# automatically as part of docker-up too; this is for running it standalone
# (e.g. after pulling a new image without restarting "swarm").
migrate:
	docker compose run --rm migrate

# Usage: make makemigrations msg="add index on trades.opened_at"
makemigrations:
	docker compose run --rm swarm alembic revision --autogenerate -m "$(msg)"

# Usage: make db-backup [file=backups/my-backup.sql.gz]
# Defaults to a timestamped path under backups/. Refuses to overwrite an
# existing file, never prints POSTGRES_PASSWORD (pg_dump runs inside the
# "postgres" container over its local socket, which the official postgres
# image trusts without a password for local connections — the credential
# never touches this shell command), and validates the dump is a
# non-empty, structurally intact archive before calling it a success.
db-backup:
	@set -euo pipefail; \
	file="$(file)"; \
	if [ -z "$$file" ]; then file="backups/swarm_trading_$$(date +%Y%m%dT%H%M%SZ).sql.gz"; fi; \
	if [ -e "$$file" ]; then echo "Refusing to overwrite existing backup: $$file" >&2; exit 1; fi; \
	mkdir -p "$$(dirname "$$file")"; \
	docker compose exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --format=custom' | gzip > "$$file"; \
	if [ ! -s "$$file" ]; then echo "Backup file is empty — deleting: $$file" >&2; rm -f "$$file"; exit 1; fi; \
	gzip -t "$$file" || { echo "Backup failed gzip integrity check — deleting: $$file" >&2; rm -f "$$file"; exit 1; }; \
	echo "Backup written and validated: $$file"

# Usage: make db-restore file=backups/swarm_trading_20260720T120000Z.sql.gz
# Requires an explicit file (no default — restoring the wrong backup by
# accident is worse than a missing default). Validates gzip integrity
# before touching the database at all, and pg_restore's --clean --if-exists
# drops existing objects first so the restore doesn't fail on conflicts
# with the current schema.
db-restore:
	@set -euo pipefail; \
	file="$(file)"; \
	if [ -z "$$file" ]; then echo "Usage: make db-restore file=<path>" >&2; exit 1; fi; \
	if [ ! -f "$$file" ]; then echo "Backup file not found: $$file" >&2; exit 1; fi; \
	gzip -t "$$file" || { echo "Backup file failed gzip integrity check — refusing to restore: $$file" >&2; exit 1; }; \
	gunzip -c "$$file" | docker compose exec -T postgres sh -c 'pg_restore -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --clean --if-exists'; \
	echo "Restore complete from: $$file"
