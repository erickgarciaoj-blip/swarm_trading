"""
RiskEngine — the single gate every OrderProposal must pass through.
No order reaches the broker without this validation.
FTMO-style rules + swarm-level correlation limits.

Two independent halts, see docs/architecture/adr/0010-daily-and-total-loss-halt.md:
- Daily-loss halt (RISK_MAX_DAILY_LOSS_PCT): resets automatically at UTC day
  rollover, never by resume().
- Sticky halt (manual halt() or the total-loss limit, RISK_MAX_TOTAL_LOSS_PCT):
  only clears via an explicit resume() call — a day change does not touch it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime

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
    """Persistable halt state. See AsyncRepository.save_risk_state /
    load_risk_state and RiskEngine.snapshot_state / restore_state."""

    daily_reference_equity: float | None
    daily_reference_date: date | None
    daily_halted: bool
    daily_halted_at: datetime | None
    daily_halt_observed_value: float | None
    sticky_halted: bool
    halt_cause: str | None  # "manual" | "total_loss" | None
    halted_at: datetime | None
    halt_observed_value: float | None


class RiskEngine:
    def __init__(self) -> None:
        self._daily_pnl: float = 0.0
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

        # Set whenever any of the persistable fields above change; cleared by
        # consume_dirty(). Lets the caller (SwarmOrchestrator) only write to
        # the DB when something actually changed.
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

        Concurrency: this method never awaits, and neither does
        on_trade_closed()/on_order_opened() below — under asyncio's
        single-threaded cooperative scheduler that makes every read-then-write
        here atomic with respect to other coroutines (e.g. the concurrent
        asyncio.gather(*[_process_agent(...) ...]) in SwarmOrchestrator), with
        no explicit lock required. If either method ever needs an `await`
        internally, this guarantee breaks and a lock becomes necessary.
        """
        # Sticky halt (manual or a prior total-loss breach) always
        # short-circuits everything else.
        if self._is_halted:
            return False, f"SWARM_HALTED: cause={self.halt_cause}"

        # Total swarm PnL is checked here — before the daily-halt short-circuit
        # below — so a total-loss breach is never masked by an already-active
        # daily halt: total_loss must take priority when both trip (see
        # ADR-0010). Sticky halt, requires an explicit resume() to clear.
        # Reference is realized PnL vs. swarm_total_capital_usd, the same
        # reference this check already used before RISK_MAX_TOTAL_LOSS_PCT was
        # tightened to 30% (see ADR-0010 §Alternativas).
        if self._total_pnl <= -(settings.swarm_total_capital_usd * settings.risk_max_total_loss_pct):
            self._trigger_sticky_halt(cause="total_loss", observed_value=self._total_pnl)
            return False, f"TOTAL_LOSS_LIMIT: pnl={self._total_pnl:.2f}"

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

    def update_daily_tracking(self, total_equity: float, now: datetime | None = None) -> None:
        """Call once per orchestrator tick with the swarm's current
        mark-to-market equity (realized + floating, net of any commission
        already folded into equity — see BaseAgent.record_trade).

        Detects UTC-day rollover (snapshots a fresh daily reference equity and
        clears any active daily halt) and evaluates the daily-loss halt
        against that reference. Must run before validate() calls in a given
        tick are trusted to reflect the current day.
        """
        now = now or datetime.utcnow()
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

    def reset_daily(self) -> None:
        """Manual reset of the daily_pnl display accumulator only — does not
        touch the reference-equity halt machinery (see update_daily_tracking)
        or clear any halt. Kept for callers that only care about the display
        figure."""
        self._daily_pnl = 0.0
        self._last_reset = datetime.utcnow()

    def halt(self, reason: str = "") -> None:
        self._trigger_sticky_halt(cause="manual", observed_value=None, reason=reason)

    def resume(self) -> None:
        """Explicit admin reactivation — clears the sticky halt (manual or
        total-loss). Does NOT clear a daily halt; that only resets at UTC day
        rollover, by design (see ADR-0010)."""
        self._is_halted = False
        self._halt_cause = None
        self._halted_at = None
        self._halt_observed_value = None
        self._dirty = True
        logger.warning("[RiskEngine] RESUMED by operator")

    def _trigger_sticky_halt(self, cause: str, observed_value: float | None, reason: str = "") -> None:
        if self._is_halted:
            return
        self._is_halted = True
        self._halt_cause = cause
        self._halted_at = datetime.utcnow()
        self._halt_observed_value = observed_value
        self._dirty = True
        logger.critical(f"[RiskEngine] HALT | cause={cause} reason={reason}")

    # ─── Persistence ──────────────────────────────────────────────────────────

    def consume_dirty(self) -> bool:
        """Returns True (and clears the flag) iff persistable state changed
        since the last call."""
        was_dirty = self._dirty
        self._dirty = False
        return was_dirty

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
        a process restart must not silently clear a persisted halt."""
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
        return self._total_pnl

    @property
    def recent_trades(self) -> list[ExecutedTrade]:
        """Newest-first, capped at RECENT_TRADES_MAXLEN."""
        return list(self._recent_trades)
