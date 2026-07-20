"""Unit tests for RiskEngine."""

import asyncio
from datetime import datetime, timedelta

import pytest

from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentMetrics,
    AgentStatus,
    ExecutedTrade,
    OrderProposal,
    OrderStatus,
    Side,
    Symbol,
)
from swarm_trading.risk.engine.risk_engine import RiskEngine


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


def _close_with_pnl(engine: RiskEngine, pnl: float, commission: float = 0.0) -> None:
    engine.on_trade_closed(
        ExecutedTrade(
            trade_id="t",
            agent_id="test_agent_001",
            symbol=Symbol.XAUUSD,
            side=Side.LONG,
            entry_price=1.0,
            quantity=1.0,
            sl_price=1.0,
            tp_price=1.0,
            status=OrderStatus.FILLED,
            pnl=pnl,
            commission=commission,
        )
    )


# ─── Total loss limit (sticky halt, requires explicit resume()) ───────────────


def test_total_loss_limit_halts_at_30_percent():
    engine = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    assert threshold == pytest.approx(0.30 * settings.swarm_total_capital_usd)

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
    assert engine.halt_cause == "total_loss"


def test_total_loss_limit_halts_exactly_at_30_percent():
    engine = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    _close_with_pnl(engine, -threshold)
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "TOTAL_LOSS_LIMIT" in reason
    assert engine.is_halted is True


def test_total_halt_requires_explicit_resume_and_does_not_clear_on_day_change():
    engine = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    _close_with_pnl(engine, -threshold)
    engine.validate(make_proposal(), make_metrics())
    assert engine.is_halted is True

    # A UTC day rollover must not clear a total-loss halt.
    engine.update_daily_tracking(total_equity=1_000_000.0, now=datetime.utcnow() + timedelta(days=1))
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"

    # Only an explicit resume() clears the flag itself.
    engine.resume()
    assert engine.is_halted is False

    # resume() clears the flag, not the recorded loss — if total_pnl is
    # still breached the very next evaluation correctly re-halts. Trading
    # only continues once the underlying condition itself recovers.
    _close_with_pnl(engine, threshold + 10.0)  # a win that recovers above the limit
    ok, _reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True


# ─── Daily loss limit (auto-resets at UTC day rollover) ───────────────────────


def test_daily_loss_below_threshold_permits_trading():
    engine = RiskEngine()
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference)  # sets today's reference
    engine.update_daily_tracking(total_equity=reference * (1 - 0.10))  # -10%, under 15%

    assert engine.is_halted is False
    ok, _reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True


def test_daily_loss_exactly_at_threshold_halts():
    engine = RiskEngine()
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference)
    engine.update_daily_tracking(total_equity=reference * (1 - settings.risk_max_daily_loss_pct))

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason


def test_daily_loss_above_threshold_halts():
    engine = RiskEngine()
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference)
    engine.update_daily_tracking(total_equity=reference * (1 - 0.20))  # -20%, above 15%

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"


def test_daily_loss_combines_realized_and_unrealized_pnl():
    """The equity fed to update_daily_tracking is realized + floating — a mix
    of closed-trade losses and an open, still-losing position should trip the
    halt exactly like a single loss of the same total size would."""
    engine = RiskEngine()
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference)

    realized_loss = 9_000.0  # from closed trades
    unrealized_loss = 7_000.0  # from an open, currently-losing position
    current_equity = reference - realized_loss - unrealized_loss  # -16%, above 15%
    engine.update_daily_tracking(total_equity=current_equity)

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"


def test_daily_loss_purely_from_open_position_crossing_threshold():
    """No trade has closed at all — an open position's floating loss alone
    must be able to trip the daily halt."""
    engine = RiskEngine()
    reference = 50_000.0
    engine.update_daily_tracking(total_equity=reference)

    floating_only_equity = reference * (1 - 0.16)
    engine.update_daily_tracking(total_equity=floating_only_equity)

    assert engine.is_halted is True
    assert engine.daily_pnl == 0.0  # no trade ever closed
    assert engine.halt_cause == "daily_loss"


def test_daily_halt_rejects_new_orders():
    engine = RiskEngine()
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference)
    engine.update_daily_tracking(total_equity=reference * (1 - 0.15))
    assert engine.is_halted is True

    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason
    assert "daily_loss" in reason


def test_daily_halt_resets_on_new_utc_day():
    engine = RiskEngine()
    day1 = datetime(2026, 7, 20, 10, 0, 0)
    reference = 100_000.0
    engine.update_daily_tracking(total_equity=reference, now=day1)
    engine.update_daily_tracking(total_equity=reference * (1 - 0.20), now=day1 + timedelta(hours=2))
    assert engine.is_halted is True

    day2 = datetime(2026, 7, 21, 0, 5, 0)
    new_reference = 85_000.0
    engine.update_daily_tracking(total_equity=new_reference, now=day2)

    assert engine.is_halted is False
    ok, _reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True


def test_daily_reference_equity_does_not_reset_within_the_same_day():
    """A process restart mid-day (same UTC date) must not re-snapshot the
    reference — it should keep evaluating against the original start-of-day
    equity, matching restore_state()'s use of this same method."""
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    engine.update_daily_tracking(total_equity=100_000.0, now=day)

    # Same day, later tick: reference must be unchanged.
    engine.update_daily_tracking(total_equity=99_000.0, now=day + timedelta(hours=10))

    snapshot = engine.snapshot_state()
    assert snapshot.daily_reference_equity == 100_000.0


def test_total_halt_takes_priority_when_both_limits_trip_simultaneously():
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    engine.update_daily_tracking(total_equity=100_000.0, now=day)
    # Trip the daily halt first.
    engine.update_daily_tracking(total_equity=100_000.0 * (1 - 0.20), now=day + timedelta(hours=1))
    assert engine.halt_cause == "daily_loss"

    # Now also trip the total-loss (sticky) halt via a closed trade.
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    _close_with_pnl(engine, -threshold)
    engine.validate(make_proposal(), make_metrics())

    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"


# ─── Concurrency ────────────────────────────────────────────────────────────


async def test_concurrent_validate_calls_trigger_total_halt_exactly_once():
    """RiskEngine.validate()/on_trade_closed() never await internally, so
    asyncio's single-threaded cooperative scheduler makes them atomic with
    respect to each other even under concurrent dispatch (see
    SwarmOrchestrator._process_symbol's asyncio.gather over agents). This
    fires many concurrent proposals right at the total-loss boundary and
    checks the halt trips exactly once with a consistent total_pnl — not a
    torn/corrupted intermediate state."""
    engine = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    _close_with_pnl(engine, -(threshold - 1.0))  # just under, still approved

    async def close_one_more():
        # No genuine `await` needed to prove the point — real callers reach
        # this from independent coroutines dispatched via asyncio.gather.
        await asyncio.sleep(0)
        _close_with_pnl(engine, -1.0)
        return engine.validate(make_proposal(), make_metrics())

    results = await asyncio.gather(*[close_one_more() for _ in range(20)])

    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"
    # Every call after the first crossing must see the halt.
    assert sum(1 for ok, _ in results if ok is False) >= 19


# ─── Persistence / restart ─────────────────────────────────────────────────


def test_snapshot_and_restore_state_round_trip():
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    engine.update_daily_tracking(total_equity=100_000.0, now=day)
    engine.update_daily_tracking(total_equity=100_000.0 * (1 - 0.20), now=day + timedelta(hours=1))
    assert engine.is_halted is True

    snapshot = engine.snapshot_state()

    restored = RiskEngine()
    restored.restore_state(snapshot)

    assert restored.is_halted is True
    assert restored.halt_cause == "daily_loss"
    assert restored.snapshot_state() == snapshot


def test_restart_does_not_silently_reactivate_a_halted_swarm():
    """Simulates a process restart: a fresh RiskEngine (as main.py creates on
    every boot) must come back up still halted once restore_state() is fed
    the last persisted snapshot — never silently defaulting to unhalted."""
    original = RiskEngine()
    threshold = settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct
    _close_with_pnl(original, -threshold)
    original.validate(make_proposal(), make_metrics())
    assert original.is_halted is True

    snapshot = original.snapshot_state()

    fresh_engine_after_restart = RiskEngine()
    assert fresh_engine_after_restart.is_halted is False  # unhalted before restore

    fresh_engine_after_restart.restore_state(snapshot)

    assert fresh_engine_after_restart.is_halted is True
    ok, reason = fresh_engine_after_restart.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "TOTAL_LOSS_LIMIT" not in reason  # this is the pre-halted SWARM_HALTED path
    assert "SWARM_HALTED" in reason


def test_consume_dirty_only_true_once_per_change():
    engine = RiskEngine()
    assert engine.consume_dirty() is False  # nothing changed yet

    engine.halt("test")
    assert engine.consume_dirty() is True
    assert engine.consume_dirty() is False  # already consumed

    engine.resume()
    assert engine.consume_dirty() is True


def test_restore_state_does_not_mark_dirty():
    """Loading persisted state at startup shouldn't itself trigger a
    redundant write back to the same row."""
    engine = RiskEngine()
    engine.halt("test")
    snapshot = engine.snapshot_state()

    restored = RiskEngine()
    restored.restore_state(snapshot)
    assert restored.consume_dirty() is False
