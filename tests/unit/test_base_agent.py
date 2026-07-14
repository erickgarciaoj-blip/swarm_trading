"""Unit tests for BaseAgent."""
import pytest
from swarm_trading.agents.scalper.scalper_agent import ScalperAgent
from swarm_trading.core.models import Symbol, AgentStatus


def test_agent_initializes_correctly():
    agent = ScalperAgent(symbol=Symbol.XAUUSD, initial_capital=1.0, target_multiplier=10.0)
    assert agent.equity == 1.0
    assert agent.target_equity == 10.0
    assert agent.is_alive


def test_agent_retires_when_equity_reaches_zero():
    agent = ScalperAgent(symbol=Symbol.XAUUSD, initial_capital=1.0)
    agent.update_equity(0.0)
    assert agent.status == AgentStatus.RETIRED
    assert not agent.is_alive


def test_equity_updates_correctly():
    agent = ScalperAgent(symbol=Symbol.XAUUSD, initial_capital=1.0)
    agent.update_equity(1.5)
    assert agent.equity == 1.5
