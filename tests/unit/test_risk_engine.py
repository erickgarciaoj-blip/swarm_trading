"""Unit tests for RiskEngine."""
import pytest
from swarm_trading.risk.engine.risk_engine import RiskEngine
from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentMetrics, AgentStatus, AgentType, ExecutedTrade, OrderProposal,
    OrderStatus, Side, Symbol,
)
from datetime import datetime


def make_proposal():
    return OrderProposal(
        agent_id="test_agent_001",
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        quantity=0.01,
        sl_price=1800.0,
        tp_price=1900.0,
        confidence=0.8,
    )

def make_metrics(equity=1.0, initial=1.0, status=AgentStatus.ACTIVE):
    return AgentMetrics(
        agent_id="test_agent_001",
        equity=equity,
        initial_capital=initial,
        total_trades=0,
        win_rate=0.0,
        sharpe=0.0,
        max_drawdown=0.0,
        current_status=status,
    )


def test_valid_proposal_is_approved():
    engine = RiskEngine()
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True
    assert reason == "OK"


def test_news_blackout_blocks_order():
    engine = RiskEngine()
    ok, reason = engine.validate(make_proposal(), make_metrics(), is_news_blackout=True)
    assert ok is False
    assert "NEWS_BLACKOUT" in reason


def test_halted_engine_blocks_all():
    engine = RiskEngine()
    engine.halt("test")
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "HALTED" in reason


def test_symbol_concentration_limit():
    engine = RiskEngine()
    prop = make_proposal()
    metrics = make_metrics()
    # Fill up to the limit
    for _ in range(10):
        engine.on_order_opened(prop)
    ok, reason = engine.validate(prop, metrics)
    assert ok is False
    assert "SYMBOL_CONCENTRATION" in reason


def _close_with_pnl(engine: RiskEngine, pnl: float) -> None:
    engine.on_trade_closed(ExecutedTrade(
        trade_id="t", agent_id="test_agent_001", symbol=Symbol.XAUUSD, side=Side.LONG,
        entry_price=1.0, quantity=1.0, sl_price=1.0, tp_price=1.0,
        status=OrderStatus.FILLED, pnl=pnl,
    ))


def test_large_daily_loss_does_not_halt_the_swarm():
    """By design there is no daily-loss stop — only the total loss limit
    (below) can halt the swarm, so agents get more room to run."""
    engine = RiskEngine()
    # Bigger than the old 5% daily threshold, but well under the 50% total limit.
    _close_with_pnl(engine, -0.06 * settings.swarm_total_capital_usd)

    ok, reason = engine.validate(make_proposal(), make_metrics())

    assert ok is True
    assert engine.is_halted is False


def test_total_loss_limit_halts_at_50_percent():
    engine = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    assert threshold == pytest.approx(0.50 * settings.swarm_total_capital_usd)

    # Just under the limit: still approved.
    _close_with_pnl(engine, -(threshold - 1.0))
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True

    # Crossing it: rejected and halted.
    _close_with_pnl(engine, -1.0)
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "TOTAL_LOSS_LIMIT" in reason
    assert engine.is_halted is True
