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
from swarm_trading.brokers.mt5.mt5_broker import LOT_SIZING_DISABLED_MSG, MT5Broker
from swarm_trading.core.models import OrderProposal, Side, Symbol


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


# ─── Fase 0: live execution is disabled until lot sizing is safe ──────────
# These replace the previous test_execute_places_order_off_the_event_loop /
# test_execute_marks_rejected_on_bad_retcode pair, which asserted that
# execute() reached mt5.order_send(). It deliberately no longer does; the
# ADR-0002 off-the-event-loop coverage for order_send returns together with
# execute() itself, once symbol_info()-backed lot conversion exists.


@pytest.mark.asyncio
async def test_mt5_execute_is_disabled_until_safe_lot_sizing_exists(fake_mt5):
    """execute() must fail closed: no order reaches the terminal, and the
    reason names lot sizing explicitly rather than failing generically."""
    fake_mt5.order_send_result = type("Result", (), {"retcode": _FakeMT5.TRADE_RETCODE_DONE, "order": 555})()
    broker = MT5Broker()

    with pytest.raises(NotImplementedError) as excinfo:
        await broker.execute(_proposal(side=Side.LONG))

    message = str(excinfo.value)
    assert LOT_SIZING_DISABLED_MSG in message
    # The operator reading this traceback must be able to tell *why* it is
    # disabled, not just that it is.
    assert "LOTS" in message
    assert "USD-notional" in message

    # Nothing was sent, and nothing was even quoted for.
    assert "order_send" not in fake_mt5.call_threads
    assert "symbol_info_tick" not in fake_mt5.call_threads


@pytest.mark.asyncio
async def test_mt5_execute_stays_disabled_even_when_terminal_is_available(fake_mt5):
    """The guard is not a stand-in for the "MT5 not installed on this OS"
    RuntimeError — it fires first, so a real Windows host with a live
    terminal is blocked too. That host is exactly the dangerous case."""
    broker = MT5Broker()
    broker._connected = True

    with pytest.raises(NotImplementedError):
        await broker.execute(_proposal())

    assert fake_mt5.call_threads == {}


@pytest.mark.asyncio
async def test_mt5_execute_disabled_guard_precedes_availability_check(monkeypatch):
    """With MT5_AVAILABLE False the old code raised RuntimeError. The sizing
    guard must win, so the failure reason stays accurate on every platform."""
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", False)
    broker = MT5Broker()

    with pytest.raises(NotImplementedError):
        await broker.execute(_proposal())


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


# test_execute_raises_when_mt5_unavailable was removed here: execute() now
# raises NotImplementedError (a RuntimeError subclass) from the sizing guard
# before ever reaching _require_mt5(), so the assertion still passed but no
# longer proved what its name claimed. Replaced by
# test_mt5_execute_disabled_guard_precedes_availability_check above.


@pytest.mark.asyncio
async def test_get_open_positions_raises_when_mt5_unavailable(monkeypatch):
    monkeypatch.setattr(mt5_broker_module, "MT5_AVAILABLE", False)
    broker = MT5Broker()

    with pytest.raises(RuntimeError):
        await broker.get_open_positions()
