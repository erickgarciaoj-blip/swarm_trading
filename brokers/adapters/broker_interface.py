"""Abstract broker interface. All concrete brokers implement this."""
from __future__ import annotations
from abc import ABC, abstractmethod
from swarm_trading.core.models import ExecutedTrade, OrderProposal, Symbol


class BrokerInterface(ABC):
    @abstractmethod
    async def connect(self) -> bool: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def execute(self, proposal: OrderProposal) -> ExecutedTrade: ...

    @abstractmethod
    async def get_open_positions(self) -> list[ExecutedTrade]: ...

    @abstractmethod
    async def close_position(self, trade_id: str) -> ExecutedTrade: ...

    async def check_tp_sl(self, symbol: Symbol, current_price: float) -> list[ExecutedTrade]:
        """Compare current_price against open trades' sl_price/tp_price and
        close whichever were hit. Default no-op: real brokers (IBKR live,
        MT5) place actual bracket/OCO orders that resolve on their own —
        only a broker with no server-side TP/SL (e.g. an offline/simulated
        adapter) needs to override this."""
        return []
