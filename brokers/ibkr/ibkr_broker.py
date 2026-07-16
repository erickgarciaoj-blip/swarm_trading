"""
Interactive Brokers broker adapter (via ibapi).
Configured for paper trading by default (port 7497, see settings.ibkr_*).

ibapi's EClient/EWrapper is synchronous and callback-driven, running its own
socket-reader loop. This adapter runs that loop on a background thread and
bridges its callbacks back into asyncio with loop.call_soon_threadsafe +
asyncio.Event, so the rest of the codebase can keep doing
`await broker.execute(...)` like any other BrokerInterface.

IMPORTANT: IBKR nets positions per contract at the account level — it has no
concept of "agent". So get_open_positions()/close_position() are backed by an
in-memory registry this adapter keeps of trades it itself opened (keyed by
our own trade_id), not by querying IBKR's netted position feed.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime

from loguru import logger

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.order import Order as IBOrder
    from ibapi.wrapper import EWrapper

    IBKR_AVAILABLE = True
except ImportError:
    # ibapi isn't on PyPI — see requirements.txt for manual install steps.
    # Keeping this import optional means main.py/swarm_factory.py stay
    # importable even when ibapi hasn't been installed.
    IBKR_AVAILABLE = False

from swarm_trading.brokers.adapters.broker_interface import BrokerInterface
from swarm_trading.core.config import settings
from swarm_trading.core.models import ExecutedTrade, OrderProposal, OrderStatus, Side


def get_front_month() -> str:
    """Retorna el mes de vencimiento más próximo en formato YYYYMM."""
    now = datetime.utcnow()
    # Futuros NQ/CL vencen el 3er viernes del mes
    # Si estamos en la segunda mitad del mes, usar el siguiente
    if now.day >= 15:
        month = now.month + 1 if now.month < 12 else 1
        year = now.year if now.month < 12 else now.year + 1
    else:
        month = now.month
        year = now.year
    return f"{year}{month:02d}"


# Symbol map: internal symbol → ibapi Contract, verified manually against
# TWS paper account. NAS100/US100 and OIL trade as CME/NYMEX futures (front
# month, computed by get_front_month()) rather than CFDs.
def get_contract(symbol: str) -> Contract:
    c = Contract()

    if symbol == "XAUUSD":
        c.symbol = "XAUUSD"
        c.secType = "CMDTY"
        c.exchange = "IBCMDTY"
        c.currency = "USD"

    elif symbol == "PLTR":
        c.symbol = "PLTR"
        c.secType = "STK"
        c.exchange = "SMART"
        c.currency = "USD"

    elif symbol in ("NAS100", "US100"):
        c.symbol = "NQ"
        c.secType = "FUT"
        c.exchange = "CME"
        c.currency = "USD"
        c.includeExpired = False
        c.lastTradeDateOrContractMonth = get_front_month()

    elif symbol == "OIL":
        c.symbol = "CL"
        c.secType = "FUT"
        c.exchange = "NYMEX"
        c.currency = "USD"
        c.includeExpired = False
        c.lastTradeDateOrContractMonth = get_front_month()

    else:
        c.symbol = symbol
        c.secType = "STK"
        c.exchange = "SMART"
        c.currency = "USD"

    return c


# Futures point values, used to convert OrderProposal.quantity (USD-notional)
# into a whole number of contracts.
NQ_POINT_VALUE = 20.0  # 1 point NQ = $20 USD
CL_POINT_VALUE = 1_000.0  # 1 point CL = $1,000 USD


def resolve_order_quantity(symbol: str, notional_usd: float, price: float) -> int:
    """USD-notional + reference price → whole units to send to IBKR
    (contracts for futures, oz for XAUUSD, shares for stocks)."""
    if symbol in ("NAS100", "US100"):
        point_value = NQ_POINT_VALUE
        # notional / (price * point_value) = contratos
        contracts = notional_usd / (price * point_value)
        return max(1, int(contracts))

    elif symbol == "OIL":
        point_value = CL_POINT_VALUE
        contracts = notional_usd / (price * point_value)
        return max(1, int(contracts))

    elif symbol == "XAUUSD":
        # 1 oz de oro ~ precio actual
        # mínimo 1 oz, máximo lo que alcance el notional
        oz = notional_usd / price
        return max(1, int(oz))

    else:  # STK (PLTR, etc.)
        shares = notional_usd / price
        return max(1, int(shares))


CONNECT_TIMEOUT_SEC = 10
ORDER_FILL_TIMEOUT_SEC = 30


if IBKR_AVAILABLE:
    # ibapi isn't on PyPI (see the import try/except above) — mypy has no
    # stubs for it even with ignore_missing_imports, so EWrapper/EClient
    # type as Any and subclassing them can't be verified statically. No
    # feasible fix short of hand-maintaining stubs for a package that isn't
    # installed on this platform.
    class _IBClient(EWrapper, EClient):  # type: ignore[misc]
        """Glues raw ibapi callbacks (network thread) to IBKRBroker's state."""

        def __init__(self, broker: IBKRBroker):
            EClient.__init__(self, self)
            self._broker = broker

        def nextValidId(self, orderId: int) -> None:
            self._broker._on_next_valid_id(orderId)

        def error(self, reqId, errorCode, errorString, *args) -> None:
            # *args absorbs extra positional params (e.g. errorTime / advanced
            # order reject JSON) added across ibapi versions — signature drift
            # here would otherwise crash the whole reader thread on every error.
            if errorCode in (2104, 2106, 2107, 2158):  # benign market-data-farm notices
                logger.debug(f"[IBKR] info {errorCode}: {errorString}")
            else:
                logger.warning(f"[IBKR] error reqId={reqId} code={errorCode}: {errorString}")

        def orderStatus(
            self,
            orderId,
            status,
            filled,
            remaining,
            avgFillPrice,
            permId,
            parentId,
            lastFillPrice,
            clientId,
            whyHeld,
            mktCapPrice,
        ) -> None:
            self._broker._on_order_status(orderId, status, avgFillPrice)
else:
    _IBClient = None  # type: ignore[assignment,misc]


class IBKRBroker(BrokerInterface):
    """Paper/live trading adapter for Interactive Brokers via ibapi."""

    def __init__(self, offline: bool = False):
        # offline=True never touches ibapi/TWS at all — every call is
        # simulated in-process. Lets main.py run end-to-end (100 agents,
        # dashboard, etc.) without a TWS/Gateway instance or ibapi installed.
        self._offline = offline
        self._client = _IBClient(self) if (IBKR_AVAILABLE and not offline) else None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._next_order_id: int | None = None
        self._order_id_lock = threading.Lock()
        self._connected_event = threading.Event()

        self._order_events: dict[int, asyncio.Event] = {}
        self._order_fill_price: dict[int, float] = {}

        # trade_id (== parent orderId as str) → live trade + its bracket children
        self._open_trades: dict[str, ExecutedTrade] = {}
        self._child_order_ids: dict[str, tuple[int, int]] = {}

    # ─── Connection lifecycle ───────────────────────────────────────────────

    async def connect(self) -> bool:
        if self._offline:
            logger.info("[IBKR] OFFLINE MODE — no TWS/Gateway connection, everything simulated")
            return True

        if not IBKR_AVAILABLE:
            logger.error(
                "[IBKR] ibapi no está instalado — instálalo manualmente desde "
                "Interactive Brokers (ver requirements.txt para las instrucciones)."
            )
            return False

        # __init__ only leaves _client None when offline or ibapi is
        # unavailable — both already handled by the two guards above.
        assert self._client is not None

        self._loop = asyncio.get_running_loop()
        try:
            # ibapi's EClient.connect() is a blocking socket connect — off the
            # event loop, same reasoning as the run_in_executor call below
            # (see docs/architecture/adr/0002-async-io-blocking-calls-must-use-executor.md).
            await self._loop.run_in_executor(
                None, self._client.connect, settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id
            )
            threading.Thread(target=self._client.run, daemon=True, name="ibkr-client").start()

            connected = await self._loop.run_in_executor(None, self._connected_event.wait, CONNECT_TIMEOUT_SEC)
            if not connected:
                logger.error("[IBKR] Timed out waiting for nextValidId — is TWS/Gateway running?")
                return False

            logger.info(
                f"[IBKR] Connected to {settings.ibkr_host}:{settings.ibkr_port} (clientId={settings.ibkr_client_id})"
            )
            return True
        except Exception as e:
            logger.error(f"[IBKR] Connection error: {e}")
            return False

    async def disconnect(self) -> None:
        if self._client:
            self._client.disconnect()

    def _on_next_valid_id(self, order_id: int) -> None:
        self._next_order_id = order_id
        self._connected_event.set()

    def _next_id(self) -> int:
        with self._order_id_lock:
            if self._next_order_id is None:
                raise RuntimeError("[IBKR] not connected — call connect() first")
            oid = self._next_order_id
            self._next_order_id += 1
            return oid

    # ─── Order execution ─────────────────────────────────────────────────────

    async def execute(self, proposal: OrderProposal) -> ExecutedTrade:
        if self._offline:
            trade = ExecutedTrade(
                trade_id=str(uuid.uuid4()),
                agent_id=proposal.agent_id,
                symbol=proposal.symbol,
                side=proposal.side,
                entry_price=proposal.price,
                quantity=proposal.quantity,
                sl_price=proposal.sl_price,
                tp_price=proposal.tp_price,
                status=OrderStatus.FILLED,
                pnl=0.0,
                opened_at=datetime.utcnow(),
            )
            # Tracked internally so check_tp_sl() can resolve it later — kept
            # out of get_open_positions()/close_position(), which stay
            # deliberately unimplemented in offline mode per that method's
            # own contract.
            self._open_trades[trade.trade_id] = trade
            logger.info(
                f"[IBKR] OFFLINE simulated: {trade.side.value} {trade.symbol.value} | agent={proposal.agent_id}"
            )
            return trade

        contract = get_contract(proposal.symbol.value)

        parent_id = self._next_id()
        tp_id = self._next_id()
        sl_id = self._next_id()

        entry_action = "BUY" if proposal.side == Side.LONG else "SELL"
        exit_action = "SELL" if proposal.side == Side.LONG else "BUY"
        qty = resolve_order_quantity(proposal.symbol.value, float(proposal.quantity), float(proposal.price))

        parent = self._new_order(parent_id, entry_action, proposal.agent_id)
        parent.orderType = "MKT"
        parent.totalQuantity = qty
        parent.transmit = False

        take_profit = self._new_order(tp_id, exit_action, proposal.agent_id)
        take_profit.orderType = "LMT"
        take_profit.totalQuantity = qty
        take_profit.lmtPrice = round(proposal.tp_price, 5)
        take_profit.parentId = parent_id
        take_profit.transmit = False

        stop_loss = self._new_order(sl_id, exit_action, proposal.agent_id)
        stop_loss.orderType = "STP"
        stop_loss.totalQuantity = qty
        stop_loss.auxPrice = round(proposal.sl_price, 5)
        stop_loss.parentId = parent_id
        stop_loss.transmit = True  # last child transmits the whole bracket

        fill_event = asyncio.Event()
        self._order_events[parent_id] = fill_event

        # Reaching here past the offline early-return means we're in live
        # mode; _next_id() above would already have raised RuntimeError if
        # connect() (the only place that populates _client) never succeeded.
        assert self._client is not None
        self._client.placeOrder(parent.orderId, contract, parent)
        self._client.placeOrder(take_profit.orderId, contract, take_profit)
        self._client.placeOrder(stop_loss.orderId, contract, stop_loss)

        try:
            await asyncio.wait_for(fill_event.wait(), timeout=ORDER_FILL_TIMEOUT_SEC)
            status = OrderStatus.FILLED
            entry_price = self._order_fill_price.pop(parent_id, 0.0)
        except TimeoutError:
            logger.warning(f"[IBKR] Fill timeout for order {parent_id} (agent={proposal.agent_id})")
            status = OrderStatus.REJECTED
            entry_price = 0.0
        finally:
            self._order_events.pop(parent_id, None)

        trade_id = str(parent_id)
        trade = ExecutedTrade(
            trade_id=trade_id,
            agent_id=proposal.agent_id,
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=entry_price,
            quantity=qty,
            sl_price=proposal.sl_price,
            tp_price=proposal.tp_price,
            status=status,
            opened_at=datetime.utcnow(),
        )

        if status == OrderStatus.FILLED:
            self._open_trades[trade_id] = trade
            self._child_order_ids[trade_id] = (tp_id, sl_id)

        logger.info(
            f"[IBKR] Order {status.value}: {trade.side.value} {trade.symbol.value} "
            f"@ {entry_price} | agent={proposal.agent_id}"
        )
        return trade

    def _on_order_status(self, order_id: int, status: str, avg_fill_price: float) -> None:
        if status != "Filled":
            return
        self._order_fill_price[order_id] = avg_fill_price
        event = self._order_events.get(order_id)
        if event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(event.set)

    # ─── Positions ───────────────────────────────────────────────────────────

    async def get_open_positions(self) -> list[ExecutedTrade]:
        if self._offline:
            return []
        return list(self._open_trades.values())

    async def check_tp_sl(self, symbol, current_price: float) -> list[ExecutedTrade]:
        """Offline-mode-only: live mode's TP/SL are real bracket orders
        already resting at IBKR, so there's nothing to simulate there.
        Closes any offline trade on `symbol` whose sl_price/tp_price the
        current tick's price has crossed, computing real pnl from it."""
        if not self._offline:
            return []

        closed: list[ExecutedTrade] = []
        for trade_id, trade in list(self._open_trades.items()):
            if trade.symbol != symbol:
                continue

            hit_tp = (trade.side == Side.LONG and current_price >= trade.tp_price) or (
                trade.side == Side.SHORT and current_price <= trade.tp_price
            )
            hit_sl = (trade.side == Side.LONG and current_price <= trade.sl_price) or (
                trade.side == Side.SHORT and current_price >= trade.sl_price
            )
            if not (hit_tp or hit_sl):
                continue

            exit_price = trade.tp_price if hit_tp else trade.sl_price
            direction = 1 if trade.side == Side.LONG else -1
            # trade.quantity is USD-notional (see BaseAgent.calc_notional), not a
            # unit count — pnl must scale with the *percentage* move, not the raw
            # price delta. Using a raw delta here previously blew up NAS100/US100
            # (quoted in thousands of index points) out of all proportion to the
            # $ actually at risk, while barely registering on XAUUSD/OIL.
            pct_change = (exit_price - trade.entry_price) / trade.entry_price
            pnl = trade.quantity * pct_change * direction

            closed_trade = ExecutedTrade(
                trade_id=trade.trade_id,
                agent_id=trade.agent_id,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                quantity=trade.quantity,
                sl_price=trade.sl_price,
                tp_price=trade.tp_price,
                status=OrderStatus.FILLED,
                pnl=pnl,
                opened_at=trade.opened_at,
                closed_at=datetime.utcnow(),
            )
            self._open_trades.pop(trade_id, None)
            closed.append(closed_trade)
            logger.info(
                f"[IBKR] OFFLINE {'TP' if hit_tp else 'SL'} hit: {trade.symbol.value} "
                f"@ {exit_price} | pnl={pnl:+.4f} | agent={trade.agent_id}"
            )

        return closed

    # ─── Closing ─────────────────────────────────────────────────────────────

    async def close_position(self, trade_id: str) -> ExecutedTrade:
        if self._offline:
            raise NotImplementedError("[IBKR] close_position is not implemented in offline mode")

        trade = self._open_trades.get(trade_id)
        if trade is None:
            raise ValueError(f"[IBKR] No open trade tracked with id={trade_id}")

        # Same invariant as execute(): live mode + an open tracked trade means
        # connect() already succeeded and populated _client.
        assert self._client is not None

        # Cancel the still-resting TP/SL child orders before flattening,
        # otherwise one could fill later against a position that no longer exists.
        for child_id in self._child_order_ids.get(trade_id, ()):
            self._client.cancelOrder(child_id)

        contract = get_contract(trade.symbol.value)
        close_id = self._next_id()
        close_action = "SELL" if trade.side == Side.LONG else "BUY"

        close_order = self._new_order(close_id, close_action, trade.agent_id)
        close_order.orderType = "MKT"
        close_order.totalQuantity = trade.quantity
        close_order.transmit = True

        fill_event = asyncio.Event()
        self._order_events[close_id] = fill_event
        self._client.placeOrder(close_id, contract, close_order)

        try:
            await asyncio.wait_for(fill_event.wait(), timeout=ORDER_FILL_TIMEOUT_SEC)
            exit_price = self._order_fill_price.pop(close_id, trade.entry_price)
        except TimeoutError:
            logger.warning(f"[IBKR] Close fill timeout for trade {trade_id}")
            exit_price = trade.entry_price
        finally:
            self._order_events.pop(close_id, None)

        direction = 1 if trade.side == Side.LONG else -1
        pnl = (exit_price - trade.entry_price) * direction * trade.quantity

        closed_trade = ExecutedTrade(
            trade_id=trade.trade_id,
            agent_id=trade.agent_id,
            symbol=trade.symbol,
            side=trade.side,
            entry_price=trade.entry_price,
            quantity=trade.quantity,
            sl_price=trade.sl_price,
            tp_price=trade.tp_price,
            status=OrderStatus.FILLED,
            pnl=pnl,
            opened_at=trade.opened_at,
            closed_at=datetime.utcnow(),
        )

        self._open_trades.pop(trade_id, None)
        self._child_order_ids.pop(trade_id, None)
        logger.info(f"[IBKR] Closed {trade_id} @ {exit_price} | pnl={pnl:+.4f}")
        return closed_trade

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _new_order(self, order_id: int, action: str, agent_id: str) -> IBOrder:
        order = IBOrder()
        order.orderId = order_id
        order.action = action
        order.orderRef = agent_id[:40]
        # Deprecated TWS flags that default True on some ibapi builds and
        # silently reject orders unless forced False; absent on newer builds.
        for legacy_flag in ("eTradeOnly", "firmQuoteOnly"):
            if hasattr(order, legacy_flag):
                setattr(order, legacy_flag, False)
        return order
