.PHONY: install run test lint format clean

install:
	python -m venv .venv && .venv/bin/pip install -r requirements.txt

run:
	.venv/bin/python main.py

mcp:
	.venv/bin/python core/mcp_server.py

test:
	.venv/bin/pytest tests/ -v

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/black .

clean:
	find . -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
