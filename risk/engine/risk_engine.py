"""
RiskEngine — the single gate every OrderProposal must pass through.
No order reaches the broker without this validation.
FTMO-style rules + swarm-level correlation limits.

Two independent halts, see docs/architecture/adr/0010-daily-and-total-loss-halt.md:
- Daily-loss halt (RISK_MAX_DAILY_LOSS_PCT): resets automatically at UTC day
  rollover, never by resume().
- Sticky halt (manual halt() or the total-loss limit, RISK_MAX_TOTAL_LOSS_PCT):
  only clears via an explicit resume() call — a day change does not touch it.
  Total-loss is equity-based (realized + floating PnL vs. the swarm's fixed
  initial capital), not realized-PnL-only — see update_daily_tracking().

Both halts persist immediately (awaited, not deferred to the next tick) the
moment they transition — see RiskStatePersistor / _persist_now() below.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    AgentMetrics,
    ExecutedTrade,
    OrderProposal,
    Symbol,
)

RECENT_TRADES_MAXLEN = 10


@dataclass
class RiskStateSnapshot:
    """Persistable halt state. See RiskStatePersistor / RiskEngine.snapshot_state
    / restore_state, and AsyncRepository.save_risk_state / load_risk_state,
    which structurally implements RiskStatePersistor."""

    daily_reference_equity: float | None
    daily_reference_date: date | None
    daily_halted: bool
    daily_halted_at: datetime | None
    daily_halt_observed_value: float | None
    sticky_halted: bool
    halt_cause: str | None  # "manual" | "total_loss" | None
    halted_at: datetime | None
    halt_observed_value: float | None


class RiskStatePersistor(Protocol):
    """Persistence port for RiskEngine's halt state — deliberately just this
    one async method, matched structurally (no inheritance, no import of
    SQLAlchemy or any other I/O library here). RiskEngine stays a pure
    domain object that only knows "I can await something that durably
    stores a RiskStateSnapshot"; it never knows or cares that the concrete
    implementation is AsyncRepository.save_risk_state (see ADR-0010:
    persistence stays resolved in the application/orchestrator layer, which
    constructs RiskEngine(persistor=repository) — AsyncRepository already
    has a method with this exact name and signature, so no adapter class is
    needed)."""

    async def save_risk_state(self, snapshot: RiskStateSnapshot) -> None: ...


class RiskEngine:
    def __init__(self, persistor: RiskStatePersistor | None = None) -> None:
        self._persistor = persistor
        # Serializes every halt/resume state transition (not routine reads)
        # against concurrent callers — see _trigger_sticky_halt's docstring
        # for why this replaced the old "no method ever awaits" atomicity
        # argument once immediate persistence introduced real awaits.
        self._transition_lock = asyncio.Lock()

        self._daily_pnl: float = 0.0
        # Realized-PnL bookkeeping only, informational (dashboard/MCP
        # display) — no longer what the total-loss halt evaluates against;
        # see update_daily_tracking()'s total_drawdown calculation instead.
        self._total_pnl: float = 0.0
        self._open_positions_by_symbol: dict[Symbol, int] = defaultdict(int)
        self._last_reset: datetime = datetime.utcnow()
        # Newest first — the single global choke point every closed trade
        # passes through, so it's the natural place for a swarm-wide feed.
        self._recent_trades: deque[ExecutedTrade] = deque(maxlen=RECENT_TRADES_MAXLEN)

        # Daily-loss halt: equity reference snapshotted at the start of each
        # UTC day, fed each tick via update_daily_tracking(). Resets only at
        # day rollover.
        self._daily_reference_equity: float | None = None
        self._daily_reference_date: date | None = None
        self._daily_halted: bool = False
        self._daily_halted_at: datetime | None = None
        self._daily_halt_observed_value: float | None = None

        # Sticky halt: manual operator halt() or the total-loss limit. Only
        # resume() clears it — a restart or a day change must not.
        self._is_halted: bool = False
        self._halt_cause: str | None = None
        self._halted_at: datetime | None = None
        self._halt_observed_value: float | None = None

        # Set whenever any of the persistable fields above change. Every
        # transition already persists immediately (see _persist_now()) and
        # clears this itself on success — it's left set only when that
        # immediate write failed, so persist_if_dirty() (SwarmOrchestrator's
        # per-tick backstop) knows to retry. Not a "pending write" queue for
        # the common case anymore, just a durability-retry signal.
        self._dirty: bool = False

    # ─── Main validation gate ─────────────────────────────────────────────────

    def validate(
        self,
        proposal: OrderProposal,
        agent_metrics: AgentMetrics,
        is_news_blackout: bool = False,
    ) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        Every check must pass for the order to reach the broker.

        Purely a read of already-current flags — never awaits, never
        mutates halt state itself (both halts are decided in
        update_daily_tracking()/_trigger_sticky_halt(), not here anymore).
        That keeps this call atomic with respect to other coroutines under
        asyncio's cooperative scheduler with no lock needed, even though
        the transitions that set these flags now do await (see
        _trigger_sticky_halt's docstring).
        """
        # Sticky halt (manual or a prior total-loss breach) always
        # short-circuits everything else. Checked before the daily halt so
        # a total-loss breach's cause is never masked by an
        # already-active daily halt (see ADR-0010) — halt_cause's own
        # priority logic below handles the case where both are active.
        if self._is_halted:
            return False, f"SWARM_HALTED: cause={self.halt_cause}"

        if self._daily_halted:
            return False, f"SWARM_HALTED: cause={self.halt_cause}"

        if is_news_blackout:
            return False, "NEWS_BLACKOUT: high-impact event window"

        if agent_metrics.current_status.value != "ACTIVE":
            return False, f"AGENT_INACTIVE: status={agent_metrics.current_status.value}"

        # Per-agent drawdown
        dd = (agent_metrics.initial_capital - agent_metrics.equity) / agent_metrics.initial_capital
        if dd >= settings.risk_max_total_loss_pct:
            return False, f"AGENT_MAX_DD: drawdown={dd:.1%}"

        # Concentration per symbol
        active_on_symbol = self._open_positions_by_symbol[proposal.symbol]
        if active_on_symbol >= settings.risk_max_agents_per_symbol:
            return False, f"SYMBOL_CONCENTRATION: {proposal.symbol.value} has {active_on_symbol} agents"

        return True, "OK"

    # ─── State updates ────────────────────────────────────────────────────────

    def on_order_opened(self, proposal: OrderProposal) -> None:
        self._open_positions_by_symbol[proposal.symbol] += 1
        logger.debug(f"[RiskEngine] +1 open on {proposal.symbol.value} by {proposal.agent_id}")

    def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self._daily_pnl += trade.pnl - trade.commission
        self._total_pnl += trade.pnl
        symbol = trade.symbol
        if self._open_positions_by_symbol[symbol] > 0:
            self._open_positions_by_symbol[symbol] -= 1
        self._recent_trades.appendleft(trade)

    async def update_daily_tracking(self, total_equity: float, now: datetime | None = None) -> None:
        """Call once per orchestrator tick with the swarm's current
        mark-to-market equity (realized + floating, net of any commission
        already folded into equity — see BaseAgent.record_trade). Drives
        BOTH halts from this single equity figure:

        - Total-loss halt (RISK_MAX_TOTAL_LOSS_PCT, 30%, sticky): evaluated
          first below, so it always wins halt_cause priority over daily
          when both trip in the same tick (see ADR-0010). Reference is
          settings.swarm_total_capital_usd — the swarm's fixed initial
          capital, an existing stable reference this same check already
          used before ADR-0010 (only its formula changed, from
          realized-PnL-only to full equity drawdown).
        - Daily-loss halt (RISK_MAX_DAILY_LOSS_PCT, 15%): unchanged —
          reference is the equity snapshotted at this UTC day's first tick.

        Must run before validate() calls in a given tick are trusted to
        reflect the current totals — in practice a breach that happens
        mid-tick is only enforced starting the following tick, since both
        halts depend on this once-per-tick equity figure, itself dependent
        on floating PnL that's only recomputed once per tick (see
        SwarmOrchestrator._compute_floating_pnl()).
        """
        now = now or datetime.utcnow()

        if not self._is_halted:
            total_drawdown = (settings.swarm_total_capital_usd - total_equity) / settings.swarm_total_capital_usd
            if total_drawdown >= settings.risk_max_total_loss_pct:
                await self._trigger_sticky_halt(cause="total_loss", observed_value=total_equity)

        today = now.date()

        if self._daily_reference_date != today:
            self._daily_reference_equity = total_equity
            self._daily_reference_date = today
            self._daily_pnl = 0.0
            self._last_reset = now
            if self._daily_halted:
                self._daily_halted = False
                self._daily_halted_at = None
                self._daily_halt_observed_value = None
                logger.info("[RiskEngine] UTC day rollover — daily halt cleared")
            self._dirty = True
            await self._persist_now()

        if self._daily_halted or self._daily_reference_equity is None or self._daily_reference_equity <= 0:
            return

        loss_pct = (self._daily_reference_equity - total_equity) / self._daily_reference_equity
        if loss_pct >= settings.risk_max_daily_loss_pct:
            self._daily_halted = True
            self._daily_halted_at = now
            self._daily_halt_observed_value = total_equity
            self._dirty = True
            logger.critical(
                f"[RiskEngine] DAILY_LOSS_LIMIT breached: equity={total_equity:.2f} "
                f"reference={self._daily_reference_equity:.2f} loss={loss_pct:.1%}"
            )
            await self._persist_now()

    def reset_daily(self) -> None:
        """Manual reset of the daily_pnl display accumulator only — does not
        touch the reference-equity halt machinery (see update_daily_tracking)
        or clear any halt. Kept for callers that only care about the display
        figure."""
        self._daily_pnl = 0.0
        self._last_reset = datetime.utcnow()

    async def halt(self, reason: str = "") -> None:
        await self._trigger_sticky_halt(cause="manual", observed_value=None, reason=reason)

    async def resume(self) -> None:
        """Explicit admin reactivation — clears the sticky halt (manual or
        total-loss). Does NOT clear a daily halt; that only resets at UTC day
        rollover, by design (see ADR-0010). Persists immediately, same as
        every halt transition below."""
        async with self._transition_lock:
            if not self._is_halted:
                return
            self._is_halted = False
            self._halt_cause = None
            self._halted_at = None
            self._halt_observed_value = None
            self._dirty = True
            logger.warning("[RiskEngine] RESUMED by operator")
            await self._persist_now()

    async def _trigger_sticky_halt(self, cause: str, observed_value: float | None, reason: str = "") -> None:
        """The sole mutator of the sticky-halt fields, reachable concurrently
        from multiple paths: validate()-adjacent update_daily_tracking()
        (total-loss, once per tick from the orchestrator loop), halt()
        (operator, via dashboard/MCP — can race with the tick loop or with
        another halt() call), and potentially both at once. _transition_lock
        makes the check-then-set-then-persist sequence atomic across all of
        them: whichever coroutine acquires it first performs the transition
        and the immediate persist; every later one sees `_is_halted` already
        True and returns as a no-op — no duplicate persists, no torn state.
        This lock is what replaced the old "no method here ever awaits, so
        asyncio's cooperative scheduler already makes this atomic" argument
        from before immediate persistence introduced a real await on this
        path (see ADR-0010's Concurrencia section)."""
        async with self._transition_lock:
            if self._is_halted:
                return
            self._is_halted = True
            self._halt_cause = cause
            self._halted_at = datetime.utcnow()
            self._halt_observed_value = observed_value
            self._dirty = True
            logger.critical(f"[RiskEngine] HALT | cause={cause} reason={reason}")
            await self._persist_now()

    # ─── Persistence ──────────────────────────────────────────────────────────

    async def _persist_now(self) -> None:
        """Best-effort immediate durable write of the current halt state,
        awaited right at the point a halt/resume transition happens (inside
        the caller's hold on _transition_lock) — see ADR-0010 §Persistencia
        inmediata. Deliberately never raises: the in-memory flags this is
        always called after are already the ones validate() checks
        synchronously, so a write failure here can never let a rejected
        order through — the swarm is already behaving as halted regardless
        of whether this succeeds. What it *does* affect is whether that
        exact transition would survive a crash landing in the (now much
        smaller — one awaited DB write, not up to one full tick) window
        before this completes. On failure, `_dirty` is deliberately left
        set so persist_if_dirty() (called every tick by
        SwarmOrchestrator.run() as a backstop) retries until it succeeds,
        without this method itself polling or retrying inline."""
        if self._persistor is None:
            self._dirty = False  # nothing configured to persist to — same "DB is optional" stance as elsewhere
            return
        try:
            await self._persistor.save_risk_state(self.snapshot_state())
        except Exception as exc:
            logger.critical(
                f"[RiskEngine] IMMEDIATE PERSIST FAILED (halt_cause={self.halt_cause}, "
                f"daily_halted={self._daily_halted}) — will retry next tick: {exc}"
            )
        else:
            self._dirty = False

    async def persist_if_dirty(self) -> None:
        """Per-tick retry backstop — see SwarmOrchestrator.run(). Every real
        halt/resume transition already persists immediately via
        _persist_now() above and clears `_dirty` on success; this only
        re-attempts a write that failed then. A clean engine (the common
        case, every tick) is a no-op — no duplicate writes."""
        if self._dirty:
            await self._persist_now()

    def snapshot_state(self) -> RiskStateSnapshot:
        return RiskStateSnapshot(
            daily_reference_equity=self._daily_reference_equity,
            daily_reference_date=self._daily_reference_date,
            daily_halted=self._daily_halted,
            daily_halted_at=self._daily_halted_at,
            daily_halt_observed_value=self._daily_halt_observed_value,
            sticky_halted=self._is_halted,
            halt_cause=self._halt_cause,
            halted_at=self._halted_at,
            halt_observed_value=self._halt_observed_value,
        )

    def restore_state(self, snapshot: RiskStateSnapshot) -> None:
        """Called once at startup, before the orchestrator's tick loop runs —
        a process restart must not silently clear a persisted halt. Pure
        state assignment, no I/O and no persist — loading what's already
        durable must never itself trigger a write back to the same row."""
        self._daily_reference_equity = snapshot.daily_reference_equity
        self._daily_reference_date = snapshot.daily_reference_date
        self._daily_halted = snapshot.daily_halted
        self._daily_halted_at = snapshot.daily_halted_at
        self._daily_halt_observed_value = snapshot.daily_halt_observed_value
        self._is_halted = snapshot.sticky_halted
        self._halt_cause = snapshot.halt_cause
        self._halted_at = snapshot.halted_at
        self._halt_observed_value = snapshot.halt_observed_value
        logger.info(
            f"[RiskEngine] Restored state | daily_halted={self._daily_halted} "
            f"sticky_halted={self._is_halted} cause={self._halt_cause}"
        )

    # ─── Properties ───────────────────────────────────────────────────────────

    @property
    def is_halted(self) -> bool:
        return self._is_halted or self._daily_halted

    @property
    def halt_cause(self) -> str | None:
        """total_loss takes priority when both halts are active at once."""
        if self._halt_cause == "total_loss":
            return "total_loss"
        if self._daily_halted:
            return "daily_loss"
        return self._halt_cause

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def total_pnl(self) -> float:
        """Realized PnL only, for display — NOT what the total-loss halt
        evaluates (see update_daily_tracking's total_drawdown, which uses
        full equity including floating PnL)."""
        return self._total_pnl

    @property
    def recent_trades(self) -> list[ExecutedTrade]:
        """Newest-first, capped at RECENT_TRADES_MAXLEN."""
        return list(self._recent_trades)
