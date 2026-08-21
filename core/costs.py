"""
Transaction-cost model for paper trading — pure domain, no I/O.

WHY
---
Before this module, paper fills were perfect: entry exactly at the candle's
close, exit exactly at the TP/SL level, no commission, no spread, no
slippage. The audit measured an average edge of +$0.004 per trade against
costs that are realistically 100-500x larger, which makes every "profitable"
paper result untrustworthy. This makes the simulated result comparable to a
real one.

WHAT A COST IS HERE
-------------------
Three components, each configurable per instrument, each ALWAYS adverse:

- spread     — half the quoted spread is paid on each side. A LONG buys at
               the ask and sells at the bid; a SHORT does the reverse.
- slippage   — execution away from the quote, in the direction that hurts.
               Never improves a fill (see adverse_price_offset_bps).
- commission — broker fee, charged once per side (entry and exit).

All three are expressed in basis points (1 bp = 0.01%) so they are
scale-free: the same configuration behaves consistently whether the
instrument prints at 86 (OIL) or 29,236 (NAS100). Commission is charged on
notional rather than per contract because `quantity` in this codebase is
USD-notional, not a unit count (see BaseAgent.calc_notional and the audit's
finding 01 — unifying that semantics is a separate, later change).

THE DECOMPOSITION
-----------------
Everything is computed against the *reference* prices (the mid/quote the
strategy actually saw), with the effective fill prices derived from them:

    gross_pnl   = notional x (ref_exit - ref_entry) / ref_entry x direction
    entry_costs = notional x offset + commission_per_side
    exit_costs  = notional x offset x (ref_exit / ref_entry) + commission_per_side
    net_pnl     = gross_pnl - (entry_costs + exit_costs)

ref_entry is deliberately the denominator for BOTH legs. Using each leg's
own fill price instead would make the decomposition non-linear, and
`gross_pnl - total_costs == net_pnl` would only hold approximately. It holds
exactly here, which is what makes the invariant testable and the persisted
audit trail reconstructable.

With every cost set to zero this reduces exactly to the legacy formula
(`quantity x pct_change x direction`), so a zero-cost run is bit-for-bit
comparable with results recorded before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass

from swarm_trading.core.models import Side

BPS = 10_000.0


@dataclass(frozen=True)
class InstrumentCosts:
    """Per-instrument cost configuration, all in basis points.

    Frozen: a cost book is configuration, and a fill must never be able to
    mutate the rates it was priced with.
    """

    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    commission_bps: float = 0.0  # charged per side, on notional

    def __post_init__(self) -> None:
        # A negative cost would be a subsidy — it would make fills better
        # than the quote and silently manufacture edge, which is the exact
        # failure mode this whole module exists to prevent.
        for name in ("spread_bps", "slippage_bps", "commission_bps"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be >= 0 (got {value!r})")

    @property
    def adverse_price_offset_bps(self) -> float:
        """Basis points the fill price moves AGAINST the trader on one side.

        Half the spread (crossing from mid to the near side of the book)
        plus slippage. Both are added, never subtracted: there is no input
        to this model that produces a fill better than the reference price.
        """
        return self.spread_bps / 2.0 + self.slippage_bps


# Default cost book.
#
# THESE ARE ILLUSTRATIVE PLACEHOLDERS, NOT MEASURED MARKET DATA. They are
# order-of-magnitude estimates for a retail account, chosen so a default run
# produces a *plausible* cost drag rather than a fictitious zero. Every one
# of them must be replaced with figures measured against the actual broker
# and account tier before any result from this system is treated as a real
# estimate of profitability.
#
# To reproduce pre-cost (legacy) behaviour exactly, configure every
# instrument to zero rather than editing this table — see
# InstrumentCosts() with no arguments, and SwarmSettings.instrument_costs.
DEFAULT_COST_BOOK: dict[str, InstrumentCosts] = {
    # Spot/CFD gold. Typical retail quote ~0.30 wide on a ~4,500 price.
    "XAUUSD": InstrumentCosts(spread_bps=0.7, slippage_bps=0.3, commission_bps=0.2),
    # US equity. Penny-wide quote on a ~175 price, plus per-share commission.
    "PLTR": InstrumentCosts(spread_bps=0.6, slippage_bps=0.3, commission_bps=0.5),
    # NQ future. One tick (0.25 index points) on a ~29,000 price is very
    # tight in bps; commission per contract is likewise tiny relative to the
    # contract's notional.
    "NAS100": InstrumentCosts(spread_bps=0.1, slippage_bps=0.1, commission_bps=0.05),
    "US100": InstrumentCosts(spread_bps=0.1, slippage_bps=0.1, commission_bps=0.05),
    # CL future. One tick (0.01) on a ~86 price is wide in bps terms.
    "OIL": InstrumentCosts(spread_bps=1.2, slippage_bps=0.5, commission_bps=0.1),
}

# Applied to an instrument with no entry in the cost book. Deliberately zero
# and NOT a guess: silently inventing a cost for an unknown instrument would
# be worse than reporting an obviously-uncosted result.
UNKNOWN_INSTRUMENT_COSTS = InstrumentCosts()


class CostBook:
    """Symbol -> InstrumentCosts lookup, with an explicit fallback.

    A thin wrapper rather than a bare dict so callers get the fallback
    behaviour for free and tests can inject a deterministic book in one
    argument.
    """

    def __init__(
        self,
        costs: dict[str, InstrumentCosts] | None = None,
        default: InstrumentCosts = UNKNOWN_INSTRUMENT_COSTS,
    ) -> None:
        self._costs = dict(costs) if costs is not None else dict(DEFAULT_COST_BOOK)
        self._default = default

    def for_symbol(self, symbol: str) -> InstrumentCosts:
        return self._costs.get(symbol, self._default)

    @classmethod
    def zero(cls) -> CostBook:
        """A cost book that charges nothing, for reproducing legacy results
        and for tests that isolate non-cost behaviour."""
        return cls(costs={}, default=InstrumentCosts())

    def __repr__(self) -> str:
        return f"<CostBook symbols={sorted(self._costs)} default={self._default}>"


# ─── Fill pricing ─────────────────────────────────────────────────────────


def _direction(side: Side) -> int:
    return 1 if side == Side.LONG else -1


def entry_fill_price(reference_price: float, side: Side, costs: InstrumentCosts) -> float:
    """Price actually paid to OPEN, given the quote the strategy saw.

    A LONG lifts the ask, so it pays more than the reference. A SHORT hits
    the bid, so it receives less. Slippage pushes further in the same
    (adverse) direction for both.
    """
    offset = costs.adverse_price_offset_bps / BPS
    return reference_price * (1.0 + _direction(side) * offset)


def exit_fill_price(reference_price: float, side: Side, costs: InstrumentCosts) -> float:
    """Price actually received to CLOSE — the mirror of entry_fill_price.

    A LONG sells into the bid (receives less than the reference); a SHORT
    buys back at the ask (pays more). Note the sign is inverted relative to
    the entry, because closing trades in the opposite direction.
    """
    offset = costs.adverse_price_offset_bps / BPS
    return reference_price * (1.0 - _direction(side) * offset)


# ─── PnL decomposition ────────────────────────────────────────────────────


def entry_cost_usd(notional_usd: float, costs: InstrumentCosts) -> float:
    """Cost incurred the moment a position OPENS: half-spread + slippage on
    the entry leg, plus one side of commission.

    Charged and recorded at open, not deferred to the close, because it is
    already sunk — an open position is worth its mark-to-market minus what
    it cost to get into. compute_costed_pnl() recomputes the identical
    figure for the closed trade, and the closed trade replaces the open one
    wholesale, so nothing is charged twice.
    """
    offset = costs.adverse_price_offset_bps / BPS
    return notional_usd * offset + notional_usd * costs.commission_bps / BPS


@dataclass(frozen=True)
class CostedPnL:
    """A closed trade's result, decomposed so it can be audited later.

    Invariant, exact (not approximate) by construction, in this exact
    grouping — costs summed first, subtracted once:

        total_costs == entry_costs + exit_costs
        net_pnl     == gross_pnl - total_costs

    The grouping is load-bearing: float addition is not associative, so
    `(gross - entry) - exit` may differ from `gross - (entry + exit)` in the
    last bit. Everything downstream that reconstructs a result must use the
    same order, or aggregate figures will drift apart by a few ulps.
    """

    gross_pnl: float
    entry_costs: float
    exit_costs: float
    commission: float
    entry_fill_price: float
    exit_fill_price: float

    @property
    def total_costs(self) -> float:
        return self.entry_costs + self.exit_costs

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_costs


def compute_costed_pnl(
    notional_usd: float,
    reference_entry_price: float,
    reference_exit_price: float,
    side: Side,
    costs: InstrumentCosts,
) -> CostedPnL:
    """Full result of a round trip, gross and net, from reference prices.

    `notional_usd` is the USD exposure (OrderProposal.quantity), not a unit
    count. `reference_*` are the prices the strategy actually acted on: the
    candle close at entry, and the TP/SL level at exit.

    Costs are charged on BOTH sides. The exit-side price cost scales by
    ref_exit / ref_entry because the same bps applies to a position whose
    value has moved — a position that doubled costs twice as much to
    unwind.
    """
    if reference_entry_price <= 0:
        raise ValueError(f"reference_entry_price must be > 0 (got {reference_entry_price!r})")

    direction = _direction(side)
    price_ratio = reference_exit_price / reference_entry_price

    # Written as (exit - entry) / entry, NOT as (exit / entry - 1.0), so a
    # zero-cost run reproduces the pre-cost formula bit-for-bit rather than
    # merely to within float tolerance. The two are algebraically identical
    # and numerically are not: for entry=100, exit=110 the first yields
    # exactly 0.1 and the second 0.10000000000000009. Any drift here would
    # show up as a spurious difference when comparing a costed run against
    # results recorded before this module existed.
    pct_change = (reference_exit_price - reference_entry_price) / reference_entry_price
    gross_pnl = notional_usd * pct_change * direction

    offset = costs.adverse_price_offset_bps / BPS
    commission_per_side = notional_usd * costs.commission_bps / BPS

    # Both legs are unconditionally positive: an adverse offset costs the
    # same whichever way the position is facing (verified in both branches
    # of the sign algebra — see this module's docstring).
    entry_price_cost = notional_usd * offset
    exit_price_cost = notional_usd * offset * price_ratio

    return CostedPnL(
        gross_pnl=gross_pnl,
        entry_costs=entry_price_cost + commission_per_side,
        exit_costs=exit_price_cost + commission_per_side,
        commission=commission_per_side * 2.0,
        entry_fill_price=entry_fill_price(reference_entry_price, side, costs),
        exit_fill_price=exit_fill_price(reference_exit_price, side, costs),
    )
