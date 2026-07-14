"""
Unit tests for IBKRBroker's execution/closing logic, with the real ibapi
socket layer stubbed out — these never touch a live TWS/Gateway connection.
"""
import re

import pytest
from swarm_trading.brokers.ibkr.ibkr_broker import IBKRBroker, get_front_month
from swarm_trading.core.models import OrderProposal, OrderStatus, Side, Symbol


class _FakeIBClient:
    """Records placeOrder/cancelOrder calls and lets tests simulate fills."""

    def __init__(self):
        self.placed: dict[int, dict] = {}
        self.cancelled: list[int] = []

    def placeOrder(self, order_id, contract, order):
        self.placed[order_id] = {"contract": contract, "order": order}

    def cancelOrder(self, order_id, *args):
        self.cancelled.append(order_id)


@pytest.fixture
async def broker():
    import asyncio
    b = IBKRBroker()
    b._client = _FakeIBClient()
    b._loop = asyncio.get_running_loop()
    b._next_order_id = 1
    return b


def _proposal(side=Side.LONG) -> OrderProposal:
    return OrderProposal(
        agent_id="swing_XAUUSD_test1234",
        symbol=Symbol.XAUUSD,
        side=side,
        quantity=0.05,
        sl_price=1900.0,
        tp_price=2000.0,
        confidence=0.8,
        reason="unit-test",
    )


@pytest.mark.asyncio
async def test_execute_fills_and_registers_open_trade(broker):
    async def run_and_fill():
        task_coro = broker.execute(_proposal())
        import asyncio
        task = asyncio.ensure_future(task_coro)
        await asyncio.sleep(0)  # let execute() place orders and start waiting
        parent_id = min(broker._client.placed.keys())
        broker._on_order_status(parent_id, "Filled", 1950.0)
        return await task

    trade = await run_and_fill()

    assert trade.status == OrderStatus.FILLED
    assert trade.entry_price == 1950.0
    assert trade.side == Side.LONG
    assert len(broker._client.placed) == 3  # parent + TP + SL
    assert trade.trade_id in broker._open_trades


@pytest.mark.asyncio
async def test_execute_times_out_gracefully(broker, monkeypatch):
    import swarm_trading.brokers.ibkr.ibkr_broker as mod
    monkeypatch.setattr(mod, "ORDER_FILL_TIMEOUT_SEC", 0.05)

    trade = await broker.execute(_proposal())

    assert trade.status == OrderStatus.REJECTED
    assert trade.trade_id not in broker._open_trades


@pytest.mark.asyncio
async def test_close_position_cancels_children_and_computes_pnl(broker):
    import asyncio

    task = asyncio.ensure_future(broker.execute(_proposal(side=Side.LONG)))
    await asyncio.sleep(0)
    parent_id = min(broker._client.placed.keys())
    broker._on_order_status(parent_id, "Filled", 1950.0)
    trade = await task

    close_task = asyncio.ensure_future(broker.close_position(trade.trade_id))
    await asyncio.sleep(0)
    close_id = max(broker._client.placed.keys())
    broker._on_order_status(close_id, "Filled", 1975.0)
    closed = await close_task

    assert closed.pnl == pytest.approx((1975.0 - 1950.0) * trade.quantity)
    assert closed.trade_id not in broker._open_trades
    assert len(broker._client.cancelled) == 2  # TP + SL cancelled


@pytest.mark.asyncio
async def test_close_position_unknown_trade_raises(broker):
    with pytest.raises(ValueError):
        await broker.close_position("does-not-exist")


def test_get_front_month_returns_valid_yyyymm_string():
    result = get_front_month()
    assert re.fullmatch(r"\d{6}", result)
    year, month = int(result[:4]), int(result[4:])
    assert 2020 <= year <= 2100
    assert 1 <= month <= 12


# ─── Offline mode (no TWS/Gateway — used for local/dev runs of main.py) ───────

@pytest.mark.asyncio
async def test_offline_connect_returns_true():
    broker = IBKRBroker(offline=True)
    assert await broker.connect() is True


@pytest.mark.asyncio
async def test_offline_execute_fills_immediately_at_proposal_price():
    broker = IBKRBroker(offline=True)
    proposal = _proposal()
    proposal.price = 1950.0

    trade = await broker.execute(proposal)

    assert trade.status == OrderStatus.FILLED
    assert trade.entry_price == 1950.0
    assert trade.pnl == 0.0


@pytest.mark.asyncio
async def test_offline_get_open_positions_is_empty():
    broker = IBKRBroker(offline=True)
    assert await broker.get_open_positions() == []


@pytest.mark.asyncio
async def test_offline_close_position_raises_not_implemented():
    broker = IBKRBroker(offline=True)
    with pytest.raises(NotImplementedError):
        await broker.close_position("any-id")


# ─── check_tp_sl (offline TP/SL simulation) ────────────────────────────────

async def _offline_open(broker, side=Side.LONG, symbol=Symbol.XAUUSD, price=1950.0,
                         sl_price=1900.0, tp_price=2000.0, quantity=0.05) -> str:
    proposal = OrderProposal(
        agent_id="swing_test", symbol=symbol, side=side, quantity=quantity,
        sl_price=sl_price, tp_price=tp_price, confidence=0.8, price=price, reason="unit-test",
    )
    trade = await broker.execute(proposal)
    return trade.trade_id


@pytest.mark.asyncio
async def test_check_tp_sl_closes_long_on_tp_hit():
    broker = IBKRBroker(offline=True)
    trade_id = await _offline_open(broker, side=Side.LONG, price=1950.0, sl_price=1900.0, tp_price=2000.0, quantity=2.0)

    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=2005.0)

    assert len(closed) == 1
    assert closed[0].trade_id == trade_id
    # pnl scales with % move of notional, not the raw price delta —
    # quantity is USD-notional (BaseAgent.calc_notional), not unit count.
    assert closed[0].pnl == pytest.approx(2.0 * (2000.0 - 1950.0) / 1950.0)
    assert trade_id not in broker._open_trades


@pytest.mark.asyncio
async def test_check_tp_sl_closes_long_on_sl_hit():
    broker = IBKRBroker(offline=True)
    trade_id = await _offline_open(broker, side=Side.LONG, price=1950.0, sl_price=1900.0, tp_price=2000.0, quantity=1.0)

    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=1890.0)

    assert len(closed) == 1
    assert closed[0].pnl == pytest.approx(1.0 * (1900.0 - 1950.0) / 1950.0)
    assert trade_id not in broker._open_trades


@pytest.mark.asyncio
async def test_check_tp_sl_closes_short_on_tp_and_sl():
    broker = IBKRBroker(offline=True)
    tp_trade = await _offline_open(broker, side=Side.SHORT, price=1950.0, sl_price=2000.0, tp_price=1900.0)

    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=1895.0)
    assert len(closed) == 1
    assert closed[0].pnl == pytest.approx(0.05 * (1900.0 - 1950.0) / 1950.0 * -1)

    sl_trade = await _offline_open(broker, side=Side.SHORT, price=1950.0, sl_price=2000.0, tp_price=1900.0)
    closed2 = await broker.check_tp_sl(Symbol.XAUUSD, current_price=2010.0)
    assert len(closed2) == 1
    assert closed2[0].trade_id == sl_trade


@pytest.mark.asyncio
async def test_check_tp_sl_pnl_stays_proportional_for_large_index_prices():
    """Regression test: NAS100/US100 (quoted in ~29,000 index points) must
    not produce a wildly larger pnl than XAUUSD (~$1,950) for an equivalent
    *percentage* stop-out — both should lose ~notional * pct_move."""
    broker = IBKRBroker(offline=True)
    notional = 0.02

    xau_id = await _offline_open(
        broker, symbol=Symbol.XAUUSD, side=Side.SHORT, price=2000.0,
        sl_price=2020.0, tp_price=1900.0, quantity=notional,  # 1% adverse move
    )
    nas_id = await _offline_open(
        broker, symbol=Symbol.NAS100, side=Side.SHORT, price=29000.0,
        sl_price=29290.0, tp_price=27000.0, quantity=notional,  # 1% adverse move
    )

    xau_closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=2020.0)
    nas_closed = await broker.check_tp_sl(Symbol.NAS100, current_price=29290.0)

    assert xau_closed[0].trade_id == xau_id
    assert nas_closed[0].trade_id == nas_id
    # Same % move against the same notional → same pnl, regardless of the
    # instrument's raw price scale.
    assert nas_closed[0].pnl == pytest.approx(xau_closed[0].pnl, rel=1e-3)


@pytest.mark.asyncio
async def test_check_tp_sl_leaves_trade_open_when_price_between_sl_and_tp():
    broker = IBKRBroker(offline=True)
    trade_id = await _offline_open(broker, sl_price=1900.0, tp_price=2000.0)

    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=1950.0)

    assert closed == []
    assert trade_id in broker._open_trades


@pytest.mark.asyncio
async def test_check_tp_sl_only_touches_matching_symbol():
    broker = IBKRBroker(offline=True)
    xau_id = await _offline_open(broker, symbol=Symbol.XAUUSD, sl_price=1900.0, tp_price=2000.0)
    oil_id = await _offline_open(broker, symbol=Symbol.OIL, price=80.0, sl_price=75.0, tp_price=85.0)

    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=2010.0)

    assert [c.trade_id for c in closed] == [xau_id]
    assert oil_id in broker._open_trades
    assert xau_id not in broker._open_trades


@pytest.mark.asyncio
async def test_check_tp_sl_is_noop_in_live_mode(broker):
    closed = await broker.check_tp_sl(Symbol.XAUUSD, current_price=999999.0)
    assert closed == []
