#!/bin/bash
# macOS setup (incl. Apple Silicon / M-series) — installs the ta-lib native
# library via Homebrew first, then the Python dependencies.
#
# NOTE ON APPLE SILICON: recent PyPI releases of the "TA-Lib" Python package
# ship a prebuilt arm64 wheel with the C library statically bundled, so
# `pip install` may succeed even without this brew step. It's kept here as a
# fallback for older pip resolvers / pinned versions that don't pick up the
# wheel and fall back to compiling from source, which does need libta-lib
# on your system.
#
# Two brokers are NOT installed by this script (see requirements.txt for why):
#   - MetaTrader5: Windows-only, no macOS build. On Mac, run with
#     app_env != "live" so IBKRBroker (paper trading) is used instead.
#   - ibapi: not on PyPI — install manually from Interactive Brokers
#     (instructions printed at the end of this script).
set -euo pipefail

echo "=== Swarm Trading — macOS setup ==="

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Install it from https://brew.sh first." >&2
    exit 1
fi

echo "--- Installing ta-lib (native library) via Homebrew ---"
brew install ta-lib

echo "--- Creating virtualenv (.venv) ---"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

echo "--- Installing Python dependencies ---"
pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
fi

echo ""
echo "✅ Done. Next steps:"
echo "  1. Edit .env with your broker credentials"
echo "  2. Run: make run   (defaults to IBKRBroker/paper trading on macOS)"
echo "  3. Dashboard: http://localhost:8000/docs"
echo "  4. For Claude Code MCP: python core/mcp_server.py"
echo ""
echo "Optional — to trade live via Interactive Brokers, install ibapi manually:"
echo "  1. Download the TWS API installer: https://interactivebrokers.github.io"
echo "  2. cd IBJts/source/pythonclient && python setup.py install"
echo ""
echo "MetaTrader5 is Windows-only and is not installed on macOS."
