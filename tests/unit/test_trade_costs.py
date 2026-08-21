"""
Fase 1A — transaction-cost model for paper trading.

Every test here injects deterministic costs. None reads
settings.instrument_costs: the defaults are illustrative placeholders (see
core/costs.py) and a test that depended on them would start failing the day
someone calibrates them against a real broker, which is a change these tests
have no opinion about.

The properties being pinned:

  1. zero costs reproduce the pre-cost PnL formula EXACTLY (bit-for-bit, not
     to within a tolerance) — otherwise a costed run can't be compared with
     history recorded before this module existed;
  2. costs are always adverse, on both legs, for both sides;
  3. gross - total_costs == net, exactly;
  4. net is spent exactly once — never re-charged downstream.
"""

from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool

from swarm_trading.agents.base.base_agent import BaseAgent
from swarm_trading.brokers.ibkr.ibkr_broker import IBKRBroker
from swarm_trading.core.costs import (
    CostBook,
    InstrumentCosts,
    compute_costed_pnl,
    entry_cost_usd,
    entry_fill_price,
    exit_fill_price,
)
from swarm_trading.core.models import (
    AgentType,
    ExecutedTrade,
    MarketState,
    OrderProposal,
    OrderStatus,
    Side,
    Symbol,
)
from swarm_trading.data.historic.db_models import Base
from swarm_trading.data.historic.repository import AsyncRepository
from swarm_trading.risk.engine.risk_engine import RiskEngine

MEMORY_DB_URL = "sqlite+aiosqlite:///:memory:"

# Deliberately round and large enough that every effect is visible in the
# assertions rather than lost in the fifth decimal. Not a claim about any
# real instrument.
COSTS = InstrumentCosts(spread_bps=10.0, slippage_bps=5.0, commission_bps=2.0)
FREE = InstrumentCosts()

NOTIONAL = 1_000.0
ENTRY = 100.0
UP = 110.0  # +10%
DOWN = 90.0  # -10%


def _legacy_pnl(notional: float, entry: float, exit_: float, side: Side) -> float:
    """The exact formula IBKRBroker.check_tp_sl() used before the cost model.
    Reproduced here so the zero-cost equivalence is asserted against a
    literal copy of the old arithmetic, not against the new code's own
    idea of what the old arithmetic was."""
    direction = 1 if side == Side.LONG else -1
    pct_change = (exit_ - entry) / entry
    return notional * pct_change * direction


# ─── 1. Zero costs preserve legacy PnL ────────────────────────────────────


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("entry", "exit_"),
    [(100.0, 110.0), (100.0, 90.0), (1950.0, 2000.0), (4575.7998, 4582.7), (29236.1113, 29221.0), (86.69, 86.5)],
)
def test_zero_costs_reproduce_legacy_pnl_exactly(side, entry, exit_):
    """Bit-for-bit, not approx: `==`, no pytest.approx.

    This is what makes a before/after comparison meaningful. It also pins
    the reason compute_costed_pnl() spells the move as (exit - entry)/entry
    rather than (exit/entry - 1.0) — the two differ in float, and the second
    would drift from every historical row.
    """
    result = compute_costed_pnl(NOTIONAL, entry, exit_, side, FREE)

    assert result.net_pnl == _legacy_pnl(NOTIONAL, entry, exit_, side)
    assert result.gross_pnl == result.net_pnl
    assert result.total_costs == 0.0


def test_zero_costs_leave_the_fill_on_the_reference_price():
    for side in (Side.LONG, Side.SHORT):
        assert entry_fill_price(ENTRY, side, FREE) == ENTRY
        assert exit_fill_price(UP, side, FREE) == UP


# ─── 2. Winning / losing trades, with and without costs ───────────────────


def test_long_winner_without_costs():
    result = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, FREE)

    assert result.gross_pnl == pytest.approx(100.0)
    assert result.net_pnl == pytest.approx(100.0)
    assert result.total_costs == 0.0


def test_long_winner_with_costs_keeps_less_than_it_earned():
    result = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, COSTS)

    assert result.gross_pnl == pytest.approx(100.0)  # unchanged by costs
    assert result.total_costs > 0
    assert result.net_pnl < result.gross_pnl
    # offset = 10/2 + 5 = 10 bps; entry leg 1000 x 0.001 = 1.0,
    # exit leg 1000 x 0.001 x (110/100) = 1.1, commission 0.2 x 2 = 0.4
    assert result.entry_costs == pytest.approx(1.0 + 0.2)
    assert result.exit_costs == pytest.approx(1.1 + 0.2)
    assert result.net_pnl == pytest.approx(100.0 - 2.5)


def test_short_winner_with_costs_keeps_less_than_it_earned():
    """A SHORT profits when price FALLS, so its winning case is the down move."""
    result = compute_costed_pnl(NOTIONAL, ENTRY, DOWN, Side.SHORT, COSTS)

    assert result.gross_pnl == pytest.approx(100.0)
    assert result.total_costs > 0
    assert result.net_pnl < result.gross_pnl
    # Exit leg is cheaper here: the position is marked at 90, not 110.
    assert result.exit_costs == pytest.approx(0.9 + 0.2)
    assert result.net_pnl == pytest.approx(100.0 - 2.3)


@pytest.mark.parametrize(
    ("side", "exit_"),
    [(Side.LONG, DOWN), (Side.SHORT, UP)],
)
def test_losing_trade_with_costs_loses_more_than_gross(side, exit_):
    """Costs deepen a loss — they never offset one."""
    result = compute_costed_pnl(NOTIONAL, ENTRY, exit_, side, COSTS)

    assert result.gross_pnl == pytest.approx(-100.0)
    assert result.net_pnl < result.gross_pnl
    assert result.net_pnl == pytest.approx(-100.0 - result.total_costs)


def test_costs_can_turn_a_gross_winner_into_a_net_loser():
    """The whole point of the phase: a move too small to cover its own costs
    is not a profitable trade, however green it looks gross."""
    tiny_move = ENTRY * 1.0001  # +1 bp, against a ~20 bp round trip
    result = compute_costed_pnl(NOTIONAL, ENTRY, tiny_move, Side.LONG, COSTS)

    assert result.gross_pnl > 0
    assert result.net_pnl < 0


# ─── 3. Fill direction: spread and slippage are always adverse ────────────


def test_spread_worsens_the_fill_on_both_legs_and_both_sides():
    spread_only = InstrumentCosts(spread_bps=10.0)

    # LONG lifts the ask on entry, hits the bid on exit.
    assert entry_fill_price(ENTRY, Side.LONG, spread_only) > ENTRY
    assert exit_fill_price(UP, Side.LONG, spread_only) < UP
    # SHORT does the reverse.
    assert entry_fill_price(ENTRY, Side.SHORT, spread_only) < ENTRY
    assert exit_fill_price(UP, Side.SHORT, spread_only) > UP


def test_half_the_spread_is_charged_per_leg():
    """Crossing from mid to one side of the book costs half the quote."""
    spread_only = InstrumentCosts(spread_bps=10.0)  # 10 bps wide -> 5 bps per leg

    assert entry_fill_price(ENTRY, Side.LONG, spread_only) == pytest.approx(ENTRY * 1.0005)
    assert exit_fill_price(ENTRY, Side.LONG, spread_only) == pytest.approx(ENTRY * 0.9995)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("slippage_bps", [0.5, 5.0, 50.0])
def test_slippage_never_improves_a_fill(side, slippage_bps):
    """Slippage is strictly adverse — there is no configuration of this
    model that hands a trade a better price than it asked for. A negative
    rate, which would be a subsidy, is rejected outright."""
    without = InstrumentCosts(spread_bps=0.0, slippage_bps=0.0)
    with_slip = InstrumentCosts(spread_bps=0.0, slippage_bps=slippage_bps)

    entry_before = entry_fill_price(ENTRY, side, without)
    entry_after = entry_fill_price(ENTRY, side, with_slip)
    exit_before = exit_fill_price(UP, side, without)
    exit_after = exit_fill_price(UP, side, with_slip)

    if side == Side.LONG:
        assert entry_after > entry_before  # pays more to get in
        assert exit_after < exit_before  # receives less to get out
    else:
        assert entry_after < entry_before  # receives less to get in
        assert exit_after > exit_before  # pays more to get out

    # And it always costs money, whichever way the position faces.
    assert compute_costed_pnl(NOTIONAL, ENTRY, UP, side, with_slip).total_costs > 0


@pytest.mark.parametrize("field", ["spread_bps", "slippage_bps", "commission_bps"])
def test_negative_cost_rates_are_rejected(field):
    """A negative rate would manufacture edge out of nothing."""
    with pytest.raises(ValueError, match=field):
        InstrumentCosts(**{field: -1.0})


def test_more_expensive_instrument_costs_strictly_more():
    cheap = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, InstrumentCosts(spread_bps=1.0))
    dear = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, InstrumentCosts(spread_bps=20.0))

    assert dear.total_costs > cheap.total_costs
    assert dear.net_pnl < cheap.net_pnl
    assert dear.gross_pnl == cheap.gross_pnl  # gross is cost-independent


# ─── 4. Commission ────────────────────────────────────────────────────────


def test_commission_alone_reduces_net_pnl():
    comm_only = InstrumentCosts(commission_bps=2.0)
    result = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, comm_only)

    # Charged per side: 1000 x 2bps x 2 legs = 0.4
    assert result.commission == pytest.approx(0.4)
    assert result.total_costs == pytest.approx(0.4)
    assert result.net_pnl == pytest.approx(result.gross_pnl - 0.4)
    # Commission does not move the fill price — only spread and slippage do.
    assert result.entry_fill_price == pytest.approx(ENTRY)
    assert result.exit_fill_price == pytest.approx(UP)


def test_commission_is_split_evenly_between_the_two_legs():
    comm_only = InstrumentCosts(commission_bps=2.0)
    result = compute_costed_pnl(NOTIONAL, ENTRY, UP, Side.LONG, comm_only)

    assert result.entry_costs == pytest.approx(result.exit_costs)
    assert result.entry_costs + result.exit_costs == pytest.approx(result.commission)


# ─── 5. The invariant ─────────────────────────────────────────────────────


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("exit_", [110.0, 90.0, 100.0, 100.01, 250.0, 1.0])
@pytest.mark.parametrize(
    "costs",
    [
        FREE,
        COSTS,
        InstrumentCosts(spread_bps=100.0),
        InstrumentCosts(slippage_bps=0.001),
        InstrumentCosts(commission_bps=33.0),
    ],
)
def test_gross_minus_total_costs_equals_net_exactly(side, exit_, costs):
    """Exact equality, not approx: net is DEFINED as the subtraction, so any
    drift would mean the decomposition had stopped being a decomposition.

    The grouping matters and is canonical: `gross - (entry + exit)`, i.e.
    costs are summed first and subtracted once. Float addition is not
    associative, so `(gross - entry) - exit` can differ in the last bit —
    asserted separately below, to tolerance, so the difference is documented
    rather than hidden.
    """
    result = compute_costed_pnl(NOTIONAL, ENTRY, exit_, side, costs)

    assert result.total_costs == result.entry_costs + result.exit_costs
    assert result.net_pnl == result.gross_pnl - result.total_costs
    # Same identity, other grouping — equal to within float representation.
    assert result.net_pnl == pytest.approx(result.gross_pnl - result.entry_costs - result.exit_costs)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_net_pnl_agrees_with_the_effective_fill_prices(side):
    """The decomposition is not just internally consistent — it matches what
    trading at the effective fills would actually have produced."""
    result = compute_costed_pnl(NOTIONAL, ENTRY, UP, side, COSTS)

    direction = 1 if side == Side.LONG else -1
    pnl_from_fills = NOTIONAL * (result.exit_fill_price - result.entry_fill_price) / ENTRY * direction
    commission = NOTIONAL * COSTS.commission_bps / 10_000 * 2

    assert result.net_pnl == pytest.approx(pnl_from_fills - commission)


def test_costs_are_positive_on_both_legs_for_both_sides():
    for side in (Side.LONG, Side.SHORT):
        for exit_ in (UP, DOWN):
            result = compute_costed_pnl(NOTIONAL, ENTRY, exit_, side, COSTS)
            assert result.entry_costs > 0, (side, exit_)
            assert result.exit_costs > 0, (side, exit_)


def test_zero_entry_price_is_rejected_rather_than_dividing_by_zero():
    with pytest.raises(ValueError, match="reference_entry_price"):
        compute_costed_pnl(NOTIONAL, 0.0, UP, Side.LONG, COSTS)


# ─── 6. Cost book ─────────────────────────────────────────────────────────


def test_cost_book_is_per_symbol_not_universal():
    book = CostBook({"OIL": InstrumentCosts(spread_bps=20.0), "PLTR": InstrumentCosts(spread_bps=1.0)})

    assert book.for_symbol("OIL").spread_bps == 20.0
    assert book.for_symbol("PLTR").spread_bps == 1.0


def test_unknown_symbol_falls_back_to_zero_rather_than_a_guess():
    """Inventing a cost for an unrecognised instrument would be worse than
    reporting an obviously-uncosted result."""
    book = CostBook({"OIL": InstrumentCosts(spread_bps=20.0)})

    assert book.for_symbol("SOMETHING_NEW").adverse_price_offset_bps == 0.0


def test_zero_cost_book_charges_nothing_for_any_symbol():
    book = CostBook.zero()

    for symbol in Symbol:
        assert book.for_symbol(symbol.value).adverse_price_offset_bps == 0.0


# ─── 7. Broker integration ────────────────────────────────────────────────


async def _open_and_close(cost_book: CostBook, side: Side, entry: float, tp: float, sl: float, exit_at: float):
    broker = IBKRBroker(offline=True, cost_book=cost_book)
    await broker.execute(
        OrderProposal(
            agent_id="a1",
            symbol=Symbol.XAUUSD,
            side=side,
            quantity=NOTIONAL,
            sl_price=sl,
            tp_price=tp,
            confidence=0.9,
            price=entry,
        )
    )
    closed = await broker.check_tp_sl(Symbol.XAUUSD, exit_at)
    assert len(closed) == 1
    return closed[0]


@pytest.mark.asyncio
async def test_broker_records_the_full_decomposition_on_close():
    book = CostBook({"XAUUSD": COSTS})
    trade = await _open_and_close(book, Side.LONG, entry=100.0, tp=110.0, sl=90.0, exit_at=110.0)

    assert trade.gross_pnl == pytest.approx(100.0)
    assert trade.total_costs == pytest.approx(2.5)
    assert trade.pnl == pytest.approx(97.5)
    assert trade.pnl == trade.gross_pnl - trade.entry_costs - trade.exit_costs
    # Reference prices and effective fills are both preserved.
    assert trade.entry_price == 100.0
    assert trade.exit_price == 110.0
    assert trade.entry_fill_price > trade.entry_price  # LONG paid up
    assert trade.exit_fill_price < trade.exit_price  # LONG sold down


@pytest.mark.asyncio
async def test_broker_entry_fill_respects_side_direction():
    book = CostBook({"XAUUSD": COSTS})
    broker = IBKRBroker(offline=True, cost_book=book)

    def _proposal(side):
        return OrderProposal(
            agent_id="a",
            symbol=Symbol.XAUUSD,
            side=side,
            quantity=NOTIONAL,
            sl_price=1.0,
            tp_price=9999.0,
            confidence=0.9,
            price=ENTRY,
        )

    long_trade = await broker.execute(_proposal(Side.LONG))
    short_trade = await broker.execute(_proposal(Side.SHORT))

    assert long_trade.entry_fill_price > ENTRY  # bought the ask
    assert short_trade.entry_fill_price < ENTRY  # sold the bid
    # Entry cost is booked at open, not deferred to the close.
    assert long_trade.entry_costs == pytest.approx(entry_cost_usd(NOTIONAL, COSTS))
    assert short_trade.entry_costs == pytest.approx(entry_cost_usd(NOTIONAL, COSTS))


@pytest.mark.asyncio
async def test_broker_with_zero_costs_matches_the_pre_cost_result():
    trade = await _open_and_close(CostBook.zero(), Side.LONG, entry=100.0, tp=110.0, sl=90.0, exit_at=110.0)

    assert trade.pnl == _legacy_pnl(NOTIONAL, 100.0, 110.0, Side.LONG)
    assert trade.total_costs == 0.0
    assert trade.gross_pnl == trade.pnl


@pytest.mark.asyncio
async def test_broker_costs_are_per_symbol():
    """Two symbols, same move, different drag — the configuration is not a
    single universal number."""
    book = CostBook({"XAUUSD": InstrumentCosts(spread_bps=40.0), "OIL": InstrumentCosts(spread_bps=2.0)})
    broker = IBKRBroker(offline=True, cost_book=book)

    for symbol in (Symbol.XAUUSD, Symbol.OIL):
        await broker.execute(
            OrderProposal(
                agent_id=f"a_{symbol.value}",
                symbol=symbol,
                side=Side.LONG,
                quantity=NOTIONAL,
                sl_price=90.0,
                tp_price=110.0,
                confidence=0.9,
                price=100.0,
            )
        )
    gold = (await broker.check_tp_sl(Symbol.XAUUSD, 110.0))[0]
    oil = (await broker.check_tp_sl(Symbol.OIL, 110.0))[0]

    assert gold.gross_pnl == pytest.approx(oil.gross_pnl)
    assert gold.total_costs > oil.total_costs
    assert gold.pnl < oil.pnl


# ─── 8. Net is spent exactly once ─────────────────────────────────────────


class _Agent(BaseAgent):
    def __init__(self):
        super().__init__(symbol=Symbol.XAUUSD, agent_type=AgentType.SCALPER, initial_capital=1_000.0)

    async def analyze(self, market_state: MarketState) -> OrderProposal | None:
        return None

    async def on_trade_closed(self, trade: ExecutedTrade) -> None:
        self.record_trade(trade)


def _closed_trade(gross: float, entry_costs: float, exit_costs: float, commission: float) -> ExecutedTrade:
    return ExecutedTrade(
        trade_id="t1",
        agent_id="a1",
        symbol=Symbol.XAUUSD,
        side=Side.LONG,
        entry_price=100.0,
        quantity=NOTIONAL,
        sl_price=90.0,
        tp_price=110.0,
        status=OrderStatus.FILLED,
        pnl=gross - entry_costs - exit_costs,
        gross_pnl=gross,
        entry_costs=entry_costs,
        exit_costs=exit_costs,
        commission=commission,
        closed_at=datetime.utcnow(),
    )


def test_agent_equity_charges_costs_exactly_once():
    """record_trade() used to read `+ pnl - commission`. With pnl now net,
    that subtracted commission a second time. Equity must move by exactly
    the net figure."""
    agent = _Agent()
    trade = _closed_trade(gross=100.0, entry_costs=1.2, exit_costs=1.3, commission=0.4)

    agent.record_trade(trade)

    assert trade.pnl == pytest.approx(97.5)
    assert agent.equity == pytest.approx(1_000.0 + 97.5)
    # Explicitly NOT 1000 + 97.5 - 0.4 (the old double charge).
    assert agent.equity != pytest.approx(1_000.0 + 97.5 - 0.4)


def test_risk_engine_charges_costs_exactly_once_and_tracks_the_decomposition():
    engine = RiskEngine()
    engine.on_trade_closed(_closed_trade(gross=100.0, entry_costs=1.2, exit_costs=1.3, commission=0.4))

    assert engine.daily_pnl == pytest.approx(97.5)
    assert engine.total_pnl == pytest.approx(97.5)
    assert engine.gross_pnl == pytest.approx(100.0)
    assert engine.total_costs == pytest.approx(2.5)
    # daily and total no longer disagree the moment a fee is reported.
    assert engine.daily_pnl == pytest.approx(engine.total_pnl)


def test_risk_engine_aggregate_identity_holds_across_many_trades():
    engine = RiskEngine()
    for i in range(1, 6):
        engine.on_trade_closed(_closed_trade(gross=10.0 * i, entry_costs=0.1 * i, exit_costs=0.2 * i, commission=0.05))

    assert engine.gross_pnl - engine.total_costs == pytest.approx(engine.total_pnl)


def test_a_net_losing_trade_does_not_count_as_a_win():
    """Gross-positive but cost-negative is a loss, and the win rate must say so."""
    agent = _Agent()
    agent.record_trade(_closed_trade(gross=1.0, entry_costs=1.0, exit_costs=1.0, commission=0.2))

    assert agent.get_metrics().win_rate == 0.0
    assert agent.equity < 1_000.0


# ─── 9. Persistence round-trip ────────────────────────────────────────────


@pytest.fixture
async def repo():
    repository = AsyncRepository(MEMORY_DB_URL)
    async with repository._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield repository
    await repository.close()


@pytest.mark.asyncio
async def test_persistence_preserves_gross_costs_and_net(repo):
    """A reloaded trade must still support the full audit: what it earned
    before costs, what each leg cost, and what was actually transacted."""
    trade = _closed_trade(gross=100.0, entry_costs=1.2, exit_costs=1.3, commission=0.4)
    trade.entry_fill_price = 100.1
    trade.exit_price = 110.0
    trade.exit_fill_price = 109.89

    await repo.save_trade(trade)
    rows = await repo.get_agent_trades("a1")

    assert len(rows) == 1
    row = rows[0]
    assert row["gross_pnl"] == pytest.approx(100.0)
    assert row["entry_costs"] == pytest.approx(1.2)
    assert row["exit_costs"] == pytest.approx(1.3)
    assert row["total_costs"] == pytest.approx(2.5)
    assert row["commission"] == pytest.approx(0.4)
    assert row["pnl"] == pytest.approx(97.5)
    assert row["entry_fill_price"] == pytest.approx(100.1)
    assert row["exit_price"] == pytest.approx(110.0)
    assert row["exit_fill_price"] == pytest.approx(109.89)
    # The invariant survives the round trip.
    assert row["pnl"] == pytest.approx(row["gross_pnl"] - row["total_costs"])


@pytest.mark.asyncio
async def test_equity_curve_uses_net_pnl_once(repo):
    """The reconstructed curve must not re-subtract costs already inside pnl."""
    await repo.save_trade(_closed_trade(gross=100.0, entry_costs=1.2, exit_costs=1.3, commission=0.4))

    curve = await repo.get_agent_equity_curve("a1", initial_capital=1_000.0)

    assert len(curve) == 1
    assert curve[0]["equity"] == pytest.approx(1_097.5)


@pytest.mark.asyncio
async def test_open_trade_persists_entry_costs_with_no_exit_yet(repo):
    """An open position has paid to get in but has no exit — the exit
    columns stay NULL rather than claiming an exit at zero."""
    broker = IBKRBroker(offline=True, cost_book=CostBook({"XAUUSD": COSTS}))
    opened = await broker.execute(
        OrderProposal(
            agent_id="a1",
            symbol=Symbol.XAUUSD,
            side=Side.LONG,
            quantity=NOTIONAL,
            sl_price=90.0,
            tp_price=110.0,
            confidence=0.9,
            price=100.0,
        )
    )

    await repo.save_trade(opened)
    row = (await repo.get_agent_trades("a1"))[0]

    assert row["entry_costs"] == pytest.approx(entry_cost_usd(NOTIONAL, COSTS))
    assert row["exit_costs"] == 0.0
    assert row["exit_price"] is None
    assert row["exit_fill_price"] is None
    assert row["closed_at"] is None


# Keep StaticPool referenced: AsyncRepository selects it for :memory: URLs
# (see repository.py), and this import documents the dependency for anyone
# reading these fixtures.
assert StaticPool is not None
