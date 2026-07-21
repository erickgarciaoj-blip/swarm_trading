"""Unit tests for RiskEngine."""

import asyncio
from datetime import datetime, timedelta

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
from swarm_trading.risk.engine.risk_engine import RiskEngine, RiskStateSnapshot


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


def _equity_for_drawdown(pct: float) -> float:
    """The total_equity that represents exactly `pct` drawdown from the
    swarm's fixed initial capital (settings.swarm_total_capital_usd) — the
    same reference update_daily_tracking()'s total-loss check uses."""
    return settings.swarm_total_capital_usd * (1 - pct)


class FakeRiskStatePersistor:
    """Test double for RiskStatePersistor (see risk_engine.py) — records
    every persisted snapshot instead of touching a real DB, so these tests
    exercise RiskEngine's persistence *decisions* (when/whether/how often
    it persists) independent of AsyncRepository/SQLAlchemy. Optionally
    fails its first `fail_times` calls to simulate a DB outage."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[RiskStateSnapshot] = []
        self._fail_times = fail_times

    async def save_risk_state(self, snapshot: RiskStateSnapshot) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("simulated DB outage")
        self.calls.append(snapshot)


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


async def test_halted_engine_blocks_all():
    engine = RiskEngine()
    await engine.halt("test")
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


# ─── Total loss limit (equity-based, sticky halt, requires explicit resume()) ─
#
# total_drawdown = (swarm_total_capital_usd - current_swarm_equity) / swarm_total_capital_usd
# current_swarm_equity is realized + floating PnL, fed in via
# update_daily_tracking()'s `total_equity` argument — see ADR-0010.


async def test_total_loss_purely_realized_triggers_halt():
    """All of the drawdown comes from closed trades — no open position
    contributes anything."""
    engine = RiskEngine()
    realized_loss = settings.swarm_total_capital_usd * (settings.risk_max_total_loss_pct + 0.01)
    _close_with_pnl(engine, -realized_loss)
    equity = settings.swarm_total_capital_usd - realized_loss  # floating = 0
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"


async def test_total_loss_purely_floating_triggers_halt():
    """No trade ever closed — the entire drawdown comes from an open,
    currently-losing position's floating PnL alone. Must trip the halt
    exactly like an equivalent realized loss would (this is the core
    behavior change from before ADR-0010's equity-based total halt)."""
    engine = RiskEngine()
    equity = _equity_for_drawdown(0.31)
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"
    assert engine.total_pnl == 0.0  # on_trade_closed() never called


async def test_total_loss_realized_and_floating_combine_to_trigger_halt():
    engine = RiskEngine()
    realized_loss = 18_000.0
    floating_loss = 14_000.0  # together: -32% of the default 100_000 swarm capital
    _close_with_pnl(engine, -realized_loss)
    equity = settings.swarm_total_capital_usd - realized_loss - floating_loss
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"


async def test_total_loss_below_30_percent_does_not_halt():
    engine = RiskEngine()
    equity = _equity_for_drawdown(settings.risk_max_total_loss_pct - 0.01)
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is False


async def test_total_loss_exactly_at_30_percent_halts():
    engine = RiskEngine()
    equity = _equity_for_drawdown(settings.risk_max_total_loss_pct)
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"


async def test_total_loss_above_30_percent_halts():
    engine = RiskEngine()
    equity = _equity_for_drawdown(0.45)
    await engine.update_daily_tracking(total_equity=equity)
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"


async def test_total_loss_recovery_does_not_auto_clear_halt():
    """Equity climbing back above the 30% line on a later tick must not
    silently un-halt the swarm — only resume() (an explicit admin action)
    can."""
    engine = RiskEngine()
    await engine.update_daily_tracking(total_equity=_equity_for_drawdown(0.35))
    assert engine.is_halted is True

    await engine.update_daily_tracking(total_equity=_equity_for_drawdown(0.05))  # equity recovers
    assert engine.is_halted is True  # recovery alone does not clear it

    await engine.resume()
    assert engine.is_halted is False


async def test_total_halt_requires_explicit_resume_and_does_not_clear_on_day_change():
    engine = RiskEngine()
    await engine.update_daily_tracking(total_equity=_equity_for_drawdown(0.35))
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"

    # A UTC day rollover must not clear a total-loss halt.
    await engine.update_daily_tracking(total_equity=1_000_000.0, now=datetime.utcnow() + timedelta(days=1))
    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"

    # Only an explicit resume() clears the flag itself.
    await engine.resume()
    assert engine.is_halted is False


async def test_total_loss_takes_priority_over_daily_when_both_trip_together():
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    await engine.update_daily_tracking(total_equity=settings.swarm_total_capital_usd, now=day)

    # A single subsequent tick's equity drop breaches both daily (15%) and
    # total (30%) at once.
    equity = _equity_for_drawdown(0.40)
    await engine.update_daily_tracking(total_equity=equity, now=day + timedelta(hours=1))

    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"
    assert engine.snapshot_state().daily_halted is True  # both actually tripped


# ─── Daily loss limit (auto-resets at UTC day rollover) ───────────────────────


async def test_daily_loss_below_threshold_permits_trading():
    engine = RiskEngine()
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)  # sets today's reference
    await engine.update_daily_tracking(total_equity=reference * (1 - 0.10))  # -10%, under 15%

    assert engine.is_halted is False
    ok, _reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True


async def test_daily_loss_exactly_at_threshold_halts():
    engine = RiskEngine()
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)
    await engine.update_daily_tracking(total_equity=reference * (1 - settings.risk_max_daily_loss_pct))

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason


async def test_daily_loss_above_threshold_halts():
    engine = RiskEngine()
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)
    await engine.update_daily_tracking(total_equity=reference * (1 - 0.20))  # -20%, above 15%

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"


async def test_daily_loss_combines_realized_and_unrealized_pnl():
    """The equity fed to update_daily_tracking is realized + floating — a mix
    of closed-trade losses and an open, still-losing position should trip the
    halt exactly like a single loss of the same total size would."""
    engine = RiskEngine()
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)

    realized_loss = 9_000.0  # from closed trades
    unrealized_loss = 7_000.0  # from an open, currently-losing position
    current_equity = reference - realized_loss - unrealized_loss  # -16%, above 15%
    await engine.update_daily_tracking(total_equity=current_equity)

    assert engine.is_halted is True
    assert engine.halt_cause == "daily_loss"


async def test_daily_loss_purely_from_open_position_crossing_threshold():
    """No trade has closed at all — an open position's floating loss alone
    must be able to trip the daily halt."""
    engine = RiskEngine()
    reference = settings.swarm_total_capital_usd  # keep in scale with the 30% total check
    await engine.update_daily_tracking(total_equity=reference)

    floating_only_equity = reference * (1 - 0.16)  # -16% daily, still well under the 30% total line
    await engine.update_daily_tracking(total_equity=floating_only_equity)

    assert engine.is_halted is True
    assert engine.daily_pnl == 0.0  # no trade ever closed
    assert engine.halt_cause == "daily_loss"


async def test_daily_halt_rejects_new_orders():
    engine = RiskEngine()
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)
    await engine.update_daily_tracking(total_equity=reference * (1 - 0.15))
    assert engine.is_halted is True

    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason
    assert "daily_loss" in reason


async def test_daily_halt_resets_on_new_utc_day():
    engine = RiskEngine()
    day1 = datetime(2026, 7, 20, 10, 0, 0)
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference, now=day1)
    await engine.update_daily_tracking(total_equity=reference * (1 - 0.20), now=day1 + timedelta(hours=2))
    assert engine.is_halted is True

    day2 = datetime(2026, 7, 21, 0, 5, 0)
    new_reference = 85_000.0
    await engine.update_daily_tracking(total_equity=new_reference, now=day2)

    assert engine.is_halted is False
    ok, _reason = engine.validate(make_proposal(), make_metrics())
    assert ok is True


async def test_daily_reference_equity_does_not_reset_within_the_same_day():
    """A process restart mid-day (same UTC date) must not re-snapshot the
    reference — it should keep evaluating against the original start-of-day
    equity, matching restore_state()'s use of this same method."""
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    await engine.update_daily_tracking(total_equity=100_000.0, now=day)

    # Same day, later tick: reference must be unchanged.
    await engine.update_daily_tracking(total_equity=99_000.0, now=day + timedelta(hours=10))

    snapshot = engine.snapshot_state()
    assert snapshot.daily_reference_equity == 100_000.0


# ─── Concurrency ────────────────────────────────────────────────────────────


async def test_concurrent_halt_calls_trigger_exactly_one_transition_and_persist():
    """Multiple concurrent halt() calls (e.g. a racing dashboard click and
    an MCP halt_swarm call arriving at the same time) must not each try to
    persist — _transition_lock inside _trigger_sticky_halt makes the
    check-then-set-then-persist sequence atomic even though the method now
    awaits (this replaces the old 'no method here ever awaits' atomicity
    argument from before immediate persistence was added — see ADR-0010's
    Concurrencia section)."""
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)

    await asyncio.gather(*[engine.halt(f"caller-{i}") for i in range(20)])

    assert engine.is_halted is True
    assert engine.halt_cause == "manual"
    assert len(persistor.calls) == 1  # only the winning transition actually persisted


async def test_concurrent_total_loss_trigger_and_manual_halt_do_not_race():
    """A total-loss breach discovered by the tick loop and a concurrent
    operator halt() call must not corrupt state or double-persist —
    whichever acquires the transition lock first wins; the other sees
    `_is_halted` already True and is a no-op."""
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    # Seed today's daily reference sequentially first (its own, unrelated
    # persist) so the race below isolates just the sticky-halt transition.
    await engine.update_daily_tracking(total_equity=settings.swarm_total_capital_usd)
    calls_before_race = len(persistor.calls)

    # A 40% drop breaches both total (30%) and daily (15%) at once — that's
    # two legitimate, distinct persists from update_daily_tracking() alone.
    # What this test actually checks is that the concurrent halt() call
    # contributes zero *additional* persists — it must see the sticky halt
    # already active (set by the total-loss trigger) and no-op, rather than
    # racing to set/persist it a second time.
    equity = _equity_for_drawdown(0.40)
    await asyncio.gather(
        engine.update_daily_tracking(total_equity=equity),
        engine.halt("operator"),
    )

    assert engine.is_halted is True
    assert engine.halt_cause == "total_loss"
    assert len(persistor.calls) == calls_before_race + 2  # total_loss + daily_loss, not +3


# ─── Persistence / restart ─────────────────────────────────────────────────


async def test_snapshot_and_restore_state_round_trip():
    engine = RiskEngine()
    day = datetime(2026, 7, 20, 6, 0, 0)
    await engine.update_daily_tracking(total_equity=100_000.0, now=day)
    await engine.update_daily_tracking(total_equity=100_000.0 * (1 - 0.20), now=day + timedelta(hours=1))
    assert engine.is_halted is True

    snapshot = engine.snapshot_state()

    restored = RiskEngine()
    restored.restore_state(snapshot)

    assert restored.is_halted is True
    assert restored.halt_cause == "daily_loss"
    assert restored.snapshot_state() == snapshot


async def test_restart_does_not_silently_reactivate_a_halted_swarm():
    """Simulates a process restart: a fresh RiskEngine (as main.py creates on
    every boot) must come back up still halted once restore_state() is fed
    the last persisted snapshot — never silently defaulting to unhalted."""
    original = RiskEngine()
    await original.update_daily_tracking(total_equity=_equity_for_drawdown(0.35))
    assert original.is_halted is True
    assert original.halt_cause == "total_loss"

    snapshot = original.snapshot_state()

    fresh_engine_after_restart = RiskEngine()
    assert fresh_engine_after_restart.is_halted is False  # unhalted before restore

    fresh_engine_after_restart.restore_state(snapshot)

    assert fresh_engine_after_restart.is_halted is True
    ok, reason = fresh_engine_after_restart.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason


async def test_restore_state_does_not_trigger_a_persist():
    """Loading persisted state at startup must not itself write back to the
    same row — restore_state() is pure state assignment, no I/O."""
    persistor = FakeRiskStatePersistor()
    setup_engine = RiskEngine()
    await setup_engine.halt("test")
    snapshot = setup_engine.snapshot_state()

    restored = RiskEngine(persistor=persistor)
    restored.restore_state(snapshot)

    assert persistor.calls == []


# ─── Immediate persistence (see ADR-0010 §Persistencia inmediata) ─────────────
#
# Every halt/resume transition persists synchronously, awaited at the exact
# point it happens — not deferred to SwarmOrchestrator's next periodic tick.


async def test_persist_if_dirty_is_noop_when_nothing_changed():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    await engine.persist_if_dirty()
    assert persistor.calls == []


async def test_manual_halt_persists_immediately():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    await engine.halt("operator requested pause")
    assert len(persistor.calls) == 1
    assert persistor.calls[0].sticky_halted is True
    assert persistor.calls[0].halt_cause == "manual"


async def test_daily_halt_persists_immediately():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    reference = 100_000.0
    await engine.update_daily_tracking(total_equity=reference)
    calls_after_reference_tick = len(persistor.calls)  # the rollover itself also persists
    await engine.update_daily_tracking(total_equity=reference * (1 - 0.15))
    assert len(persistor.calls) == calls_after_reference_tick + 1
    assert persistor.calls[-1].daily_halted is True


async def test_total_halt_persists_immediately():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    await engine.update_daily_tracking(total_equity=_equity_for_drawdown(settings.risk_max_total_loss_pct))
    assert engine.is_halted is True
    assert persistor.calls[-1].sticky_halted is True
    assert persistor.calls[-1].halt_cause == "total_loss"


async def test_resume_persists_immediately():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)
    await engine.halt("test")
    calls_after_halt = len(persistor.calls)

    await engine.resume()

    assert len(persistor.calls) == calls_after_halt + 1
    assert persistor.calls[-1].sticky_halted is False


async def test_persist_failure_keeps_halt_active_and_does_not_raise():
    """If the immediate persist attempt fails, the system must stay safe:
    the in-memory halt (already set before persistence was even attempted)
    keeps rejecting orders regardless, and the failure must not propagate
    out of halt()/validate() as an exception."""
    persistor = FakeRiskStatePersistor(fail_times=1)
    engine = RiskEngine(persistor=persistor)

    await engine.halt("test")  # must not raise even though the persist attempt fails

    assert engine.is_halted is True  # in-memory state is authoritative regardless
    ok, reason = engine.validate(make_proposal(), make_metrics())
    assert ok is False  # a failed persist must never let an order through
    assert reason.startswith("SWARM_HALTED")
    assert persistor.calls == []  # the one attempt failed and wasn't recorded


async def test_persist_failure_is_retried_by_persist_if_dirty():
    persistor = FakeRiskStatePersistor(fail_times=1)
    engine = RiskEngine(persistor=persistor)

    await engine.halt("test")
    assert persistor.calls == []  # first attempt failed

    await engine.persist_if_dirty()  # SwarmOrchestrator's per-tick backstop retries

    assert len(persistor.calls) == 1  # succeeds this time
    assert persistor.calls[0].sticky_halted is True


async def test_persist_if_dirty_does_not_duplicate_after_a_successful_immediate_persist():
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)

    await engine.halt("test")
    assert len(persistor.calls) == 1

    await engine.persist_if_dirty()

    assert len(persistor.calls) == 1  # unchanged — already durable, nothing left to retry


async def test_no_persistor_configured_does_not_raise():
    """repository=None (no DB configured) must behave like today: halts
    still work in-memory, they just don't survive a restart — the
    'DB is optional' stance elsewhere in this project."""
    engine = RiskEngine(persistor=None)
    await engine.halt("test")
    assert engine.is_halted is True
    await engine.persist_if_dirty()  # must not raise


async def test_halt_persists_immediately_then_survives_a_simulated_restart():
    """The exact 6-step scenario ADR-0010's 'persistencia inmediata' design
    exists to close: a halt must be durable the moment it's raised, not
    just eventually-consistent by the next tick, so it survives a process
    death that happens before any periodic persistence would have run."""
    persistor = FakeRiskStatePersistor()
    engine = RiskEngine(persistor=persistor)

    # 1. activar halt
    await engine.halt("operator requested pause")
    assert engine.is_halted is True

    # 2. persistir inmediatamente — no tick, no orchestrator, no
    # persist_if_dirty() call anywhere in this test; the write already
    # happened as part of the `await engine.halt(...)` call above.
    assert len(persistor.calls) == 1
    persisted_snapshot = persistor.calls[0]
    assert persisted_snapshot.sticky_halted is True
    assert persisted_snapshot.halt_cause == "manual"

    # 3. destruir el engine antes del siguiente tick
    del engine

    # 4. crear un engine nuevo
    fresh_engine = RiskEngine(persistor=persistor)
    assert fresh_engine.is_halted is False  # unhalted before restore

    # 5. restaurar el estado
    fresh_engine.restore_state(persisted_snapshot)

    # 6. confirmar que continúa halted
    assert fresh_engine.is_halted is True
    assert fresh_engine.halt_cause == "manual"
    ok, reason = fresh_engine.validate(make_proposal(), make_metrics())
    assert ok is False
    assert "SWARM_HALTED" in reason
