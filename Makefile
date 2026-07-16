.PHONY: install run mcp test test-integration coverage lint format typecheck precommit clean \
	docker-build docker-up docker-down docker-restart docker-logs docker-shell migrate makemigrations

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

# Brings up Postgres, waits for it to be healthy, applies migrations, then
# starts the app — in that order. `docker compose up swarm` alone would
# start the app against a DB with no schema (see ADR-0008: the app never
# creates tables itself).
docker-up:
	docker compose up -d postgres
	docker compose run --rm migrate
	docker compose up -d swarm

docker-down:
	docker compose down

docker-restart:
	docker compose restart swarm

docker-logs:
	docker compose logs -f swarm

docker-shell:
	docker compose exec swarm /bin/bash

# Applies pending migrations. Never `alembic upgrade head` run from inside
# the app itself — see data/historic/repository.py::init().
migrate:
	docker compose run --rm migrate

# Usage: make makemigrations msg="add index on trades.opened_at"
makemigrations:
	docker compose run --rm swarm alembic revision --autogenerate -m "$(msg)"
