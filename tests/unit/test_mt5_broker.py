"""
Unit tests for MT5Broker. The real `MetaTrader5` package only ships Windows
wheels (MT5_AVAILABLE is False on macOS/Linux, including this dev machine
and the target Ubuntu VPS — see module docstring), so every test here
monkeypatches a fake `mt5` namespace onto the module rather than requiring
the real package. Before this file, MT5Broker had zero test coverage.

Also a regression suite for ADR-0002: every mt5.* call is synchronous IPC to
the MT5 terminal process and must run via asyncio.to_thread, never directly
on the event loop's own thread.
"""

import threading
from typing import Any

import pytest

import swarm_trading.brokers.mt5.mt5_broker as mt5_broker_module
from swarm_trading.brokers.mt5.mt5_broker import MT5Broker
from swarm_trading.core.models import OrderProposal, OrderStatus, Side, Symbol


class _FakeMT5:
    """Stands in for the `MetaTrader5` module — records which thread each
    call ran on, so tests can assert it was never the event loop's thread."""

    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 2
    TRADE_RETCODE_DONE = 10009

    def __init__(self):
        self.call_threads: dict[str, str] = {}
        self.initialize_result = True
        self.order_send_result = None
        self.positions: list[Any] = []

    def _record(self, name: str) -> None:
        self.call_threads[name] = threading.current_thread().name

    def initialize(self, login=None, password=None, server=None):
        self._record("initialize")
        return self.initialize_result

    def shutdown(self):
        self._record("shutdown")

    def symbol_info_tick(self, symbol):
        self._record("symbol_info_tick")
        return type("Tick", (), {"ask": 1950.5, "bid": 1950.0})()

    def order_send(self, request):
        self._record("order_send")
        return self.order_send_result

    def positions_get(self):
        self._record("positions_get")
        return self.positions


def _proposal(side=Side.LONG) -> OrderProposal:
    return OrderProposal(
        agent_id="scalper_XAUUSD_test1234",
        symbol=Symbol.XAUUSD,
        side=side,
        quantity=0.05,
        sl_price=1900.0,
        tp_price=2000.0,
        confidence=0.8,
        reason="unit-test",
    )


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = _FakeMT5()
    # raising=False: on this OS the real `import MetaTrader5 as mt5` never
    # succeeded, so the module has no `mt5` attribute to overwrite yet.
    monkeypatch.setattr(mt5_broker_module, "mt5", fake, raising=False)
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", True)
    return fake


@pytest.mark.asyncio
async def test_connect_runs_initialize_off_the_event_loop(fake_mt5):
    event_loop_thread = threading.current_thread().name
    broker = MT5Broker()

    connected = await broker.connect()

    assert connected is True
    assert broker._connected is True
    assert fake_mt5.call_threads["initialize"] != event_loop_thread


@pytest.mark.asyncio
async def test_connect_returns_false_when_mt5_unavailable(monkeypatch):
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", False)
    broker = MT5Broker()

    assert await broker.connect() is False


@pytest.mark.asyncio
async def test_disconnect_runs_shutdown_off_the_event_loop(fake_mt5):
    event_loop_thread = threading.current_thread().name
    broker = MT5Broker()
    broker._connected = True

    await broker.disconnect()

    assert broker._connected is False
    assert fake_mt5.call_threads["shutdown"] != event_loop_thread


@pytest.mark.asyncio
async def test_execute_places_order_off_the_event_loop(fake_mt5):
    event_loop_thread = threading.current_thread().name
    fake_mt5.order_send_result = type("Result", (), {"retcode": _FakeMT5.TRADE_RETCODE_DONE, "order": 555})()

    broker = MT5Broker()
    trade = await broker.execute(_proposal(side=Side.LONG))

    assert trade.status == OrderStatus.FILLED
    assert trade.trade_id == "555"
    assert trade.entry_price == 1950.5  # ask price for a LONG
    assert fake_mt5.call_threads["symbol_info_tick"] != event_loop_thread
    assert fake_mt5.call_threads["order_send"] != event_loop_thread


@pytest.mark.asyncio
async def test_execute_marks_rejected_on_bad_retcode(fake_mt5):
    fake_mt5.order_send_result = type("Result", (), {"retcode": 99999, "order": 0})()

    broker = MT5Broker()
    trade = await broker.execute(_proposal())

    assert trade.status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_get_open_positions_maps_mt5_positions_off_the_event_loop(fake_mt5):
    event_loop_thread = threading.current_thread().name
    fake_mt5.positions = [
        type(
            "Position",
            (),
            {
                "ticket": 42,
                "comment": "swarm|scalper_XAUUSD_abc",
                "symbol": "XAUUSD",
                "type": 0,
                "price_open": 1950.0,
                "volume": 0.05,
                "sl": 1900.0,
                "tp": 2000.0,
                "profit": 12.5,
            },
        )()
    ]

    broker = MT5Broker()
    positions = await broker.get_open_positions()

    assert len(positions) == 1
    assert positions[0].trade_id == "42"
    assert positions[0].agent_id == "scalper_XAUUSD_abc"
    assert positions[0].side == Side.LONG
    assert positions[0].pnl == 12.5
    assert fake_mt5.call_threads["positions_get"] != event_loop_thread


@pytest.mark.asyncio
async def test_execute_raises_when_mt5_unavailable(monkeypatch):
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", False)
    broker = MT5Broker()

    with pytest.raises(RuntimeError):
        await broker.execute(_proposal())


@pytest.mark.asyncio
async def test_get_open_positions_raises_when_mt5_unavailable(monkeypatch):
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", False)
    broker = MT5Broker()

    with pytest.raises(RuntimeError):
        await broker.get_open_positions()
