"""
Historical feature-frame fetcher shared by RL training (train.py) and
RLAgent's periodic retraining. Mirrors MarketFeed._compute_indicators, but
keeps the full rolling series (not just the latest bar) since the training
env needs one row per historical bar.
"""

from __future__ import annotations

import pandas as pd

from swarm_trading.core.models import Symbol
from swarm_trading.data.feeds.market_feed import YF_SYMBOL_MAP


def fetch_feature_frame(symbol: Symbol, period: str = "60d", interval: str = "1h") -> pd.DataFrame:
    import yfinance as yf

    ticker = YF_SYMBOL_MAP.get(symbol.value, symbol.value)
    df: pd.DataFrame = yf.download(ticker, period=period, interval=interval, progress=False)
    if df.empty:
        raise ValueError(f"No historical data for {ticker}")

    close, high, low = df["Close"], df["High"], df["Low"]

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()

    out = (
        pd.DataFrame(
            {
                "close": close,
                "rsi_14": rsi,
                "atr_14": atr,
                "ema_20": ema20,
                "ema_50": ema50,
                "ema_200": ema200,
            }
        )
        .dropna()
        .reset_index(drop=True)
    )

    if out.empty:
        raise ValueError(f"Not enough history for {ticker} to compute indicators")
    return out
