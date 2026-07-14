"""
MarketFeed — fetches OHLCV data and computes indicators.
Supports yfinance (dev) and Polygon (production) backends.
"""
from __future__ import annotations
import pandas as pd
from datetime import datetime
from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import Candle, MarketState, Symbol

# yfinance symbol map
YF_SYMBOL_MAP = {
    "XAUUSD": "GC=F",
    "PLTR":   "PLTR",
    "NAS100": "^NDX",
    "US100":  "^NDX",
    "OIL":    "CL=F",
}


class MarketFeed:
    def __init__(self, backend: str = "yfinance"):
        self.backend = backend
        self._cache: dict[Symbol, MarketState] = {}

    async def get_state(self, symbol: Symbol, period: str = "1d", interval: str = "1m") -> MarketState:
        try:
            if self.backend == "yfinance":
                return await self._fetch_yfinance(symbol, period, interval)
            else:
                raise NotImplementedError(f"Backend '{self.backend}' not yet implemented")
        except Exception as e:
            logger.error(f"[MarketFeed] Error fetching {symbol}: {e}")
            # Return last cached state if available
            if symbol in self._cache:
                return self._cache[symbol]
            raise

    async def _fetch_yfinance(self, symbol: Symbol, period: str, interval: str) -> MarketState:
        import yfinance as yf
        ticker = YF_SYMBOL_MAP.get(symbol.value, symbol.value)
        df: pd.DataFrame = yf.download(ticker, period=period, interval=interval, progress=False)

        if df.empty:
            raise ValueError(f"No data for {ticker}")

        # Recent yfinance versions return MultiIndex columns (field, ticker)
        # even for a single symbol — flatten to plain field names ("Open",
        # "High", ...) so row["Open"] etc. below yield scalars, not Series.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        candles = [
            Candle(
                symbol=symbol,
                timestamp=pd.Timestamp(ts).to_pydatetime(),
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                volume=float(row["Volume"]),
                timeframe=interval,
            )
            for ts, row in df.iterrows()
        ]

        indicators = self._compute_indicators(df)

        state = MarketState(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            candles=candles[-100:],  # last 100 candles
            indicators=indicators,
        )
        self._cache[symbol] = state
        return state

    def _compute_indicators(self, df: pd.DataFrame) -> dict:
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]

        # RSI-14
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        rsi   = 100 - (100 / (1 + rs))

        # ATR-14
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()

        # EMAs
        ema20  = close.ewm(span=20, adjust=False).mean()
        ema50  = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()

        last = -1
        return {
            "rsi_14":  round(float(rsi.iloc[last]),  3),
            "atr_14":  round(float(atr.iloc[last]),  6),
            "ema_20":  round(float(ema20.iloc[last]), 5),
            "ema_50":  round(float(ema50.iloc[last]), 5),
            "ema_200": round(float(ema200.iloc[last]), 5),
        }
