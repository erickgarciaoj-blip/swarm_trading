#!/bin/bash
# First-time setup script
echo "=== Swarm Trading Setup ==="
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
echo ""
echo "✅ Done. Next steps:"
echo "  1. Edit .env with your broker credentials"
echo "  2. Run: make run"
echo "  3. Dashboard: http://localhost:8000/docs"
echo "  4. For Claude Code MCP: python core/mcp_server.py"
