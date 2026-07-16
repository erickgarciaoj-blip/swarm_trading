.PHONY: install run mcp test coverage lint format typecheck precommit clean

# docker/postgres/migrate targets (build, up, down, restart, logs, shell,
# migrate, makemigrations) land in Fase 3/4 once Postgres + Docker Compose
# are wired in — see ARCHITECTURE_REVIEW.md. Not added early as stubs that
# would reference infrastructure that doesn't exist yet.

install:
	python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pre-commit install

run:
	.venv/bin/python main.py

mcp:
	.venv/bin/python core/mcp_server.py

test:
	.venv/bin/pytest tests/ -v

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
