"""
ReplayBroker — deterministic fills against historical bars.

WHY A SEPARATE ADAPTER AND NOT IBKRBroker(offline=True)
-------------------------------------------------------
The paper broker resolves TP/SL from a single price (the tick's close),
because that is genuinely all the live runtime has. A backtest has the full
bar — open, high, low, close — and the high/low are precisely what decide
whether a level was touched. Reusing the paper adapter would throw that
information away and, worse, inherit its implicit "TP wins" bias without
saying so.

So this is a different execution venue implementing the same
BrokerInterface. What it does NOT do is reimplement the money: every figure
comes from core/costs.py — the same entry_fill_price, entry_cost_usd and
compute_costed_pnl the paper broker uses. There is exactly one PnL formula
in this codebase and it lives there.

INTRABAR AMBIGUITY
------------------
When a bar's range covers both the stop and the target, daily/minute OHLCV
cannot say which came first. The paper broker checks TP before SL, so it
silently resolves every such bar in the position's favour — a bias that
inflates backtest results precisely on the volatile bars where it matters
most. This module makes the choice explicit and configurable, and defaults
to the pessimistic reading. See IntrabarPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from swarm_trading.brokers.adapters.broker_interface import BrokerInterface
from swarm_trading.core.costs import (
    CostBook,
    compute_costed_pnl,
    entry_cost_usd,
    entry_fill_price,
)
from swarm_trading.core.models import (
    Candle,
    ExecutedTrade,
    OrderProposal,
    OrderStatus,
    Side,
    Symbol,
)


class IntrabarPolicy(StrEnum):
    """How to resolve a bar whose range touches both the stop and the target.

    CONSERVATIVE (the default) — assume the stop filled first. The worst
        outcome the bar permits. Chosen as the default because a backtest's
        job is to find out whether an edge survives adversity, and an
        optimistic tie-break is unfalsifiable: it can only ever flatter the
        result, and it does so most often on high-range bars, which are
        exactly the ones that decide whether a strategy is viable.

    OPTIMISTIC — assume the target filled first. Reproduces the paper
        broker's current implicit behaviour (IBKRBroker.check_tp_sl tests TP
        before SL), so it exists to quantify that bias, not to be traded on.
        Running the same backtest under both bounds the true result.

    A BETTER OPTION WE DO NOT HAVE: neither branch is a measurement, they are
    bounds. The only real fix is finer data — sub-bar (tick or second)
    quotes, replayed in order, where "which came first" is observed rather
    than assumed. That belongs to whatever phase introduces a data provider
    that offers it; the interface here would not change, only the bar size.
    Until then, report both bounds and treat the gap between them as a
    measure of how much the answer depends on an assumption.
    """

    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"


@dataclass(frozen=True)
class BarTouch:
    """Which levels a bar's range reached, before ordering is decided."""

    hit_tp: bool
    hit_sl: bool

    @property
    def ambiguous(self) -> bool:
        return self.hit_tp and self.hit_sl


def levels_touched(trade: ExecutedTrade, candle: Candle) -> BarTouch:
    """Whether the bar's range covers the trade's target and/or stop.

    Uses high/low, not close: a level inside the bar's range was reachable
    during the bar even if price closed elsewhere. A close-only test (what
    the live paper broker is limited to) misses every level that was touched
    and then retraced, which understates both winners and losers.
    """
    if trade.side == Side.LONG:
        return BarTouch(hit_tp=candle.high >= trade.tp_price, hit_sl=candle.low <= trade.sl_price)
    return BarTouch(hit_tp=candle.low <= trade.tp_price, hit_sl=candle.high >= trade.sl_price)


def resolve_exit_price(trade: ExecutedTrade, candle: Candle, policy: IntrabarPolicy) -> float | None:
    """Exit reference price for this bar, or None if neither level was hit.

    Returns the LEVEL, not the bar's close: a resting stop or limit fills at
    its price, not at wherever the bar happened to end. Gaps are deliberately
    not modelled here (a stop gapped through would fill worse than its
    level) — see the module's limitations in the phase report.
    """
    touch = levels_touched(trade, candle)
    if not (touch.hit_tp or touch.hit_sl):
        return None
    if touch.ambiguous:
        return trade.sl_price if policy == IntrabarPolicy.CONSERVATIVE else trade.tp_price
    return trade.tp_price if touch.hit_tp else trade.sl_price


class ReplayBroker(BrokerInterface):
    """Fills orders against a bar series, with costs and an explicit
    intrabar policy.

    Not thread-safe and not concurrent: a replay is a single deterministic
    sequence, which is the point.
    """

    def __init__(
        self,
        cost_book: CostBook | None = None,
        intrabar_policy: IntrabarPolicy = IntrabarPolicy.CONSERVATIVE,
    ) -> None:
        self._costs = cost_book if cost_book is not None else CostBook()
        self.intrabar_policy = intrabar_policy
        self._open_trades: dict[str, ExecutedTrade] = {}
        # Bar index at which each trade opened. A trade may not be closed on
        # the bar that opened it: the signal was computed from that bar's
        # close, so the position exists only from the NEXT bar onward.
        # Without this, a wide bar could open and stop out on the same close
        # the strategy had not finished reacting to — a subtle look-ahead.
        self._opened_at_bar: dict[str, int] = {}
        # Every trade_id ever closed, so a double exit is impossible even if
        # a caller resolves the same bar twice.
        self._closed_ids: set[str] = set()
        self._current_bar: int = -1
        self._current_time: datetime | None = None
        # Monotonic counter feeding deterministic trade ids. uuid4() would
        # make every run produce different ids for identical trades, which
        # breaks byte-for-byte reproducibility of the JSON/CSV output even
        # though the numbers matched.
        self._order_seq: int = 0
        self.closed_trades: list[ExecutedTrade] = []
        # Exits resolved on a bar that touched BOTH levels — the count of
        # decisions the data could not settle, and therefore a direct
        # measure of how much of a result rests on intrabar_policy rather
        # than on observation.
        self.ambiguous_exits: int = 0

    # ─── Replay clock ─────────────────────────────────────────────────────

    def set_bar_index(self, index: int, timestamp: datetime | None = None) -> None:
        """Advance the replay clock. Monotonic by construction — going
        backwards would let a position close before it opened.

        `timestamp` is the current bar's own time and becomes the opened_at
        of anything filled on it. Without it a trade would be stamped with
        OrderProposal.timestamp, which defaults to datetime.utcnow() — wall
        clock, not simulated time — making every backtest's equity curve
        and CSV carry today's date instead of the bar's.
        """
        if index < self._current_bar:
            raise ValueError(f"replay clock cannot move backwards ({self._current_bar} -> {index})")
        self._current_bar = index
        if timestamp is not None:
            self._current_time = timestamp

    # ─── BrokerInterface ──────────────────────────────────────────────────

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def execute(self, proposal: OrderProposal) -> ExecutedTrade:
        """Open at the proposal's reference price, filled adversely.

        Same shape as IBKRBroker's offline fill: entry_price keeps the
        reference the strategy acted on, entry_fill_price is what was
        actually transacted, and the entry cost is booked immediately
        because it is already sunk.
        """
        costs = self._costs.for_symbol(proposal.symbol.value)
        self._order_seq += 1
        # Deterministic and readable: agent, bar, sequence. Unique because
        # _order_seq is monotonic for the life of the broker.
        trade_id = f"{proposal.agent_id}#b{self._current_bar}#{self._order_seq}"
        trade = ExecutedTrade(
            trade_id=trade_id,
            agent_id=proposal.agent_id,
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=proposal.price,
            entry_fill_price=entry_fill_price(proposal.price, proposal.side, costs),
            entry_costs=entry_cost_usd(proposal.quantity, costs),
            quantity=proposal.quantity,
            sl_price=proposal.sl_price,
            tp_price=proposal.tp_price,
            status=OrderStatus.FILLED,
            pnl=0.0,
            # Simulated time, not wall clock — see set_bar_index().
            opened_at=self._current_time or proposal.timestamp,
        )
        self._open_trades[trade.trade_id] = trade
        self._opened_at_bar[trade.trade_id] = self._current_bar
        return trade

    async def get_open_positions(self) -> list[ExecutedTrade]:
        return [replace(trade) for trade in self._open_trades.values()]

    async def close_position(self, trade_id: str) -> ExecutedTrade:
        raise NotImplementedError("ReplayBroker closes positions through resolve_bar(), not by id")

    async def check_tp_sl(self, symbol: Symbol, current_price: float) -> list[ExecutedTrade]:
        """Not the replay path — resolve_bar() is, because it needs the
        bar's high/low. Present only to satisfy BrokerInterface; raising
        rather than silently degrading to close-only resolution."""
        raise NotImplementedError("ReplayBroker resolves exits via resolve_bar(bar), not check_tp_sl()")

    # ─── Replay exit resolution ───────────────────────────────────────────

    def resolve_bar(self, symbol: Symbol, candle: Candle, bar_index: int) -> list[ExecutedTrade]:
        """Close every open position on `symbol` whose level this bar reached.

        Deterministic ordering: positions are resolved in the order they were
        opened, so the same inputs always produce the same sequence of closed
        trades regardless of dict iteration details.
        """
        closed: list[ExecutedTrade] = []

        for trade_id in sorted(self._open_trades, key=lambda tid: (self._opened_at_bar[tid], tid)):
            trade = self._open_trades[trade_id]
            if trade.symbol != symbol:
                continue
            # A position cannot close on the bar that opened it.
            if bar_index <= self._opened_at_bar[trade_id]:
                continue

            exit_price = resolve_exit_price(trade, candle, self.intrabar_policy)
            if exit_price is None:
                continue
            if levels_touched(trade, candle).ambiguous:
                self.ambiguous_exits += 1

            result = compute_costed_pnl(
                notional_usd=trade.quantity,
                reference_entry_price=trade.entry_price,
                reference_exit_price=exit_price,
                side=trade.side,
                costs=self._costs.for_symbol(trade.symbol.value),
            )
            closed_trade = replace(
                trade,
                status=OrderStatus.FILLED,
                pnl=result.net_pnl,
                gross_pnl=result.gross_pnl,
                entry_costs=result.entry_costs,
                exit_costs=result.exit_costs,
                commission=result.commission,
                entry_fill_price=result.entry_fill_price,
                exit_price=exit_price,
                exit_fill_price=result.exit_fill_price,
                closed_at=candle.timestamp,
            )

            del self._open_trades[trade_id]
            del self._opened_at_bar[trade_id]
            self._closed_ids.add(trade_id)
            closed.append(closed_trade)
            self.closed_trades.append(closed_trade)

        return closed

    @property
    def open_position_count(self) -> int:
        return len(self._open_trades)

    def was_closed(self, trade_id: str) -> bool:
        return trade_id in self._closed_ids
