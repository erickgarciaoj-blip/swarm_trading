"""
MetaTrader 5 broker adapter.
Requires MT5 terminal running locally with AutoTrading enabled.

MetaTrader5 (the PyPI package) only ships Windows wheels — there is no
macOS/Linux build. Importing it is wrapped in try/except so this module (and
therefore main.py/swarm_factory.py) stays importable on any OS; on Mac, run
with app_env != "live" so IBKRBroker (paper trading) is used instead.

STATUS: execute() is deliberately DISABLED and raises NotImplementedError
on every call — see _require_safe_lot_sizing() and LOT_SIZING_DISABLED_MSG.
Order sizing here would send USD-notional into MT5's lot-denominated
`volume` field. connect()/disconnect()/get_open_positions() are unaffected;
only order placement is blocked.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from loguru import logger

try:
    import MetaTrader5 as mt5

    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from swarm_trading.brokers.adapters.broker_interface import BrokerInterface
from swarm_trading.core.config import settings
from swarm_trading.core.models import (
    ExecutedTrade,
    OrderProposal,
    OrderStatus,
    Side,
)

# Raised by every MT5Broker.execute() call while USD-notional → lot-size
# conversion is unimplemented. Kept as a module constant so tests assert on
# the real message instead of duplicating a copy of it.
LOT_SIZING_DISABLED_MSG = (
    "MT5 live execution is disabled: OrderProposal.quantity is USD-notional, but MT5's "
    "order `volume` field is LOTS. Converting one to the other safely requires "
    "symbol_info() contract_size/volume_min/volume_step plus a margin check, which is "
    "not implemented yet. Refusing to send an order rather than risk a wrong-by-orders-"
    "of-magnitude position size."
)

# Symbol map: internal symbol → MT5 symbol name
MT5_SYMBOL_MAP = {
    "XAUUSD": "XAUUSD",
    "PLTR": "PLTR",
    "NAS100": "NAS100",
    "US100": "US100",
    "OIL": "USOIL",
}


class MT5Broker(BrokerInterface):
    def __init__(self):
        self._connected = False

    def _require_mt5(self) -> None:
        if not MT5_AVAILABLE:
            raise RuntimeError(
                "[MT5] MetaTrader5 no disponible en este OS — solo soporta Windows. "
                "En Mac usa modo paper_offline (IBKRBroker)."
            )

    def _require_safe_lot_sizing(self, proposal: OrderProposal) -> None:
        """Hard gate on live MT5 execution — see LOT_SIZING_DISABLED_MSG.

        `OrderProposal.quantity` carries USD-notional (see
        BaseAgent.calc_notional), but MT5's order request field `volume`
        means *lots*. Sending one as the other is not an approximation, it
        is a unit error of several orders of magnitude: a $30 notional
        becomes 30 lots, which for XAUUSD (100 oz/lot) is roughly $13.7M of
        exposure backed by an agent holding $1,000.

        Re-enabling this requires a real conversion validated against
        mt5.symbol_info(): trade_contract_size, volume_min, volume_max,
        volume_step, plus a margin check. Until that exists, every call
        raises here rather than sending a partially-correct order — a
        wrong-by-1000x fill is far worse than a failed run.
        """
        raise NotImplementedError(
            f"{LOT_SIZING_DISABLED_MSG} (agent={proposal.agent_id} symbol={proposal.symbol.value} "
            f"requested_notional_usd={proposal.quantity})"
        )

    async def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.error("[MT5] MT5 no disponible en este OS — solo Windows. Usa IBKRBroker en Mac.")
            return False
        try:
            # mt5.* calls are synchronous IPC to the MT5 terminal process —
            # off the event loop (see
            # docs/architecture/adr/0002-async-io-blocking-calls-must-use-executor.md).
            ok = await asyncio.to_thread(
                mt5.initialize,
                login=settings.mt5_login,
                password=settings.mt5_password,
                server=settings.mt5_server,
            )
            self._connected = ok
            logger.info(f"[MT5] Connected={ok}")
            return ok
        except Exception as e:
            logger.error(f"[MT5] Connection error: {e}")
            return False

    async def disconnect(self) -> None:
        if not MT5_AVAILABLE:
            return
        try:
            await asyncio.to_thread(mt5.shutdown)
            self._connected = False
        except Exception:
            pass

    async def execute(self, proposal: OrderProposal) -> ExecutedTrade:
        # FAIL CLOSED — see _require_safe_lot_sizing()'s docstring. Checked
        # before _require_mt5() and before any mt5.* call, so no code path
        # here can reach mt5.order_send() while the sizing conversion is
        # missing, regardless of OS or terminal availability.
        self._require_safe_lot_sizing(proposal)
        self._require_mt5()
        mt5_symbol = MT5_SYMBOL_MAP.get(proposal.symbol.value, proposal.symbol.value)
        order_type = mt5.ORDER_TYPE_BUY if proposal.side == Side.LONG else mt5.ORDER_TYPE_SELL
        price = await asyncio.to_thread(mt5.symbol_info_tick, mt5_symbol)
        entry = price.ask if proposal.side == Side.LONG else price.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mt5_symbol,
            "volume": round(float(proposal.quantity), 2),
            "type": order_type,
            "price": entry,
            "sl": proposal.sl_price,
            "tp": proposal.tp_price,
            "comment": f"swarm|{proposal.agent_id[:16]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = await asyncio.to_thread(mt5.order_send, request)
        status = OrderStatus.FILLED if result.retcode == mt5.TRADE_RETCODE_DONE else OrderStatus.REJECTED
        logger.info(f"[MT5] Order result: retcode={result.retcode} | agent={proposal.agent_id}")

        return ExecutedTrade(
            trade_id=str(result.order) if status == OrderStatus.FILLED else str(uuid.uuid4()),
            agent_id=proposal.agent_id,
            symbol=proposal.symbol,
            side=proposal.side,
            entry_price=entry,
            quantity=proposal.quantity,
            sl_price=proposal.sl_price,
            tp_price=proposal.tp_price,
            status=status,
            opened_at=datetime.utcnow(),
        )

    async def get_open_positions(self) -> list[ExecutedTrade]:
        self._require_mt5()
        positions = await asyncio.to_thread(mt5.positions_get)
        result = []
        if positions:
            for p in positions:
                result.append(
                    ExecutedTrade(
                        trade_id=str(p.ticket),
                        agent_id=p.comment.split("|")[-1] if "|" in p.comment else "unknown",
                        symbol=p.symbol,
                        side=Side.LONG if p.type == 0 else Side.SHORT,
                        entry_price=p.price_open,
                        quantity=p.volume,
                        sl_price=p.sl,
                        tp_price=p.tp,
                        status=OrderStatus.FILLED,
                        pnl=p.profit,
                    )
                )
        return result

    async def close_position(self, trade_id: str) -> ExecutedTrade:
        raise NotImplementedError("Implement close_position for MT5")
