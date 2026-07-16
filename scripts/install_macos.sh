#!/bin/bash
# macOS setup (incl. Apple Silicon / M-series).
#
# Two brokers are NOT installed by this script (see requirements.txt for why):
#   - MetaTrader5: Windows-only, no macOS build. On Mac, run with
#     app_env != "live" so IBKRBroker (paper trading) is used instead.
#   - ibapi: not on PyPI — install manually from Interactive Brokers
#     (instructions printed at the end of this script).
set -euo pipefail

echo "=== Swarm Trading — macOS setup ==="

echo "--- Creating virtualenv (.venv) ---"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

echo "--- Installing Python dependencies (incl. dev tools: ruff/mypy/pytest/pre-commit) ---"
pip install -r requirements-dev.txt
pre-commit install

if [ ! -f .env ]; then
    cp .env.example .env
fi

echo ""
echo "✅ Done. Next steps:"
echo "  1. Edit .env with your broker credentials"
echo "  2. Run: make run   (defaults to IBKRBroker/paper trading on macOS)"
echo "  3. Dashboard: http://localhost:8000/docs"
echo "  4. For Claude Code MCP: python core/mcp_server.py"
echo "  5. make lint / make typecheck / make test / make coverage"
echo ""
echo "Optional — to trade live via Interactive Brokers, install ibapi manually:"
echo "  1. Download the TWS API installer: https://interactivebrokers.github.io"
echo "  2. cd IBJts/source/pythonclient && python setup.py install"
echo ""
echo "MetaTrader5 is Windows-only and is not installed on macOS."
