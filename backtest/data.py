"""
Historical OHLCV loading and bar-by-bar indicator computation for replay.

NO LOOK-AHEAD, AND WHY THE INDICATORS ARE PRECOMPUTED
-----------------------------------------------------
Precomputing a whole indicator series up front looks like cheating. It is
not, and the distinction matters enough to spell out.

Every indicator the strategies use — RSI-14, ATR-14, EMA-20/50/200 — is
causal: pandas `.rolling()` and `.ewm()` at row i read rows <= i and nothing
after. So the value sitting at row i is exactly what a live feed would have
computed having seen only bars 0..i. Computing the column once, vectorized,
is an O(n) way of getting the same numbers an O(n^2) per-bar recomputation
would produce.

That equivalence is not assumed, it is asserted:
tests/backtest/test_no_look_ahead.py compares every precomputed row against
MarketFeed._compute_indicators() run on the truncated frame df[:i+1], which
is the live code path. If someone later adds a non-causal indicator (a
centered rolling window, a `.shift(-1)`, a full-series normalisation) that
test fails.

The rounding below is deliberate and load-bearing: MarketFeed rounds RSI to
3 decimals, ATR to 6 and EMAs to 5 before handing them to an agent. A
strategy comparing `rsi < 30.0` can flip on the 4th decimal, so replaying
with unrounded values would silently diverge from the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from swarm_trading.core.models import Candle, Symbol

# Column names this module works in, matching what MarketFeed receives from
# yfinance. Loaders normalise to these before anything else runs.
OHLCV_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# Indicator names and their rounding, mirroring
# MarketFeed._compute_indicators() exactly — see the module docstring for why
# the rounding is not cosmetic.
INDICATOR_ROUNDING = {
    "rsi_14": 3,
    "atr_14": 6,
    "ema_20": 5,
    "ema_50": 5,
    "ema_200": 5,
}


class OHLCVSourceError(ValueError):
    """Raised when input data cannot be trusted to replay against."""


@dataclass(frozen=True)
class Bar:
    """One historical bar plus the indicator values computable at its close.

    `index` is the bar's position in the series — the replay clock. Nothing
    in the engine may read a Bar with a higher index than the one currently
    being processed.
    """

    index: int
    candle: Candle
    indicators: dict[str, float]

    @property
    def timestamp(self) -> datetime:
        return self.candle.timestamp


def normalise_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Accept the common OHLCV spellings and return the canonical frame.

    Handles yfinance's MultiIndex columns (the same flattening MarketFeed
    does), lowercase headers from CSV exports, and a `timestamp`/`date`
    column instead of a DatetimeIndex.
    """
    out = df.copy()

    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)

    # Case-insensitive rename onto the canonical spellings.
    canonical = {c.lower(): c for c in OHLCV_COLUMNS}
    renamed = {}
    for column in out.columns:
        key = str(column).lower()
        if key in canonical:
            renamed[column] = canonical[key]
    out = out.rename(columns=renamed)

    # A timestamp column is promoted to the index; an existing DatetimeIndex
    # is normalised below.
    if not isinstance(out.index, pd.DatetimeIndex):
        for candidate in ("timestamp", "Timestamp", "date", "Date", "datetime", "Datetime"):
            if candidate in out.columns:
                # utc=True is required, not defensive: a real multi-month
                # intraday export crosses DST, so the column holds mixed
                # UTC offsets and a naive parse raises "Mixed timezones
                # detected". Parsing to UTC first gives one unambiguous
                # instant per row.
                out = out.set_index(pd.to_datetime(out[candidate], utc=True)).drop(columns=[candidate])
                break

    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise OHLCVSourceError(f"missing required OHLCV column(s): {missing}; got {list(out.columns)}")

    if not isinstance(out.index, pd.DatetimeIndex):
        raise OHLCVSourceError("no DatetimeIndex and no timestamp/date column to build one from")

    # Drop the tz once every row is on the same one, leaving naive UTC. The
    # rest of the codebase (Candle.timestamp, ExecutedTrade.opened_at,
    # datetime.utcnow()) is tz-naive, and mixing the two raises on the first
    # comparison.
    if out.index.tz is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)

    out = out[list(OHLCV_COLUMNS)].astype(float)

    # Chronological order is what makes "bar i is the present" meaningful at
    # all. Sorting rather than rejecting, because CSV exports are commonly
    # newest-first.
    if not out.index.is_monotonic_increasing:
        out = out.sort_index()

    if out.index.has_duplicates:
        raise OHLCVSourceError("duplicate timestamps in the input data — cannot order bars unambiguously")

    return out


def compute_indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Causal indicator columns, one row per bar.

    Formulas are a line-for-line mirror of MarketFeed._compute_indicators().
    They are duplicated here rather than imported because that method returns
    only the LAST bar's scalars, and a replay needs the whole series; the
    equivalence is pinned by test_no_look_ahead.py rather than by sharing
    code that does not fit.
    """
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

    out = pd.DataFrame(
        {
            "rsi_14": rsi,
            "atr_14": atr,
            "ema_20": close.ewm(span=20, adjust=False).mean(),
            "ema_50": close.ewm(span=50, adjust=False).mean(),
            "ema_200": close.ewm(span=200, adjust=False).mean(),
        },
        index=df.index,
    )
    for name, places in INDICATOR_ROUNDING.items():
        out[name] = out[name].round(places)
    return out


class OHLCVSeries:
    """An immutable, indexable bar series for one symbol.

    Deliberately not a generator: a replay needs random access to the
    trailing window (MarketState carries the last N candles), and a
    generator would either buffer internally or force the engine to. What it
    does NOT offer is any way to reach past the current index — the engine
    slices with `window(i)`, which is bounded above by i.
    """

    def __init__(self, symbol: Symbol, df: pd.DataFrame, timeframe: str = "1m"):
        frame = normalise_ohlcv(df)
        if frame.empty:
            raise OHLCVSourceError(f"no rows for {symbol.value}")

        self.symbol = symbol
        self.timeframe = timeframe
        self._frame = frame
        self._indicators = compute_indicator_frame(frame)

        # .to_numpy() rather than .itertuples(): pandas-stubs types tuple
        # fields as a broad union that mypy will not accept into float(),
        # and a float64 array is both correctly typed and faster to build
        # a few hundred thousand Candles from.
        values = frame.to_numpy(dtype=float)
        self._candles: list[Candle] = [
            Candle(
                symbol=symbol,
                timestamp=pd.Timestamp(ts).to_pydatetime(),
                open=float(row[0]),
                high=float(row[1]),
                low=float(row[2]),
                close=float(row[3]),
                volume=float(row[4]),
                timeframe=timeframe,
            )
            for ts, row in zip(frame.index, values, strict=True)
        ]

    def __len__(self) -> int:
        return len(self._candles)

    @property
    def frame(self) -> pd.DataFrame:
        """Read-only view of the normalised OHLCV, for tests and reporting."""
        return self._frame.copy()

    def candle(self, index: int) -> Candle:
        return self._candles[index]

    def window(self, index: int, lookback: int) -> list[Candle]:
        """Candles [index-lookback+1 .. index] inclusive — never beyond index.

        The upper bound is the whole no-look-ahead guarantee on the candle
        side; `index + 1` appears nowhere in this class.
        """
        if index < 0 or index >= len(self._candles):
            raise IndexError(f"bar index {index} out of range for {len(self._candles)} bars")
        start = max(0, index - lookback + 1)
        return self._candles[start : index + 1]

    def indicators_at(self, index: int) -> dict[str, float]:
        """Indicator values as of this bar's close.

        NaN entries (the warm-up period before a rolling window has enough
        history) are dropped rather than zero-filled, so an agent sees the
        key as absent — exactly what MarketState.indicators.get() expects,
        and what the live feed produces during warm-up.
        """
        row = self._indicators.iloc[index]
        return {str(name): float(value) for name, value in row.items() if pd.notna(value)}

    def bar(self, index: int) -> Bar:
        return Bar(index=index, candle=self.candle(index), indicators=self.indicators_at(index))


def load_csv(symbol: Symbol, path: str | Path, timeframe: str = "1m") -> OHLCVSeries:
    """Load one symbol's history from a CSV file."""
    return OHLCVSeries(symbol, pd.read_csv(path), timeframe=timeframe)


def load_dataframe(symbol: Symbol, df: pd.DataFrame, timeframe: str = "1m") -> OHLCVSeries:
    """Load one symbol's history from an in-memory DataFrame.

    The seam for any future provider: fetch into a DataFrame, hand it here.
    Nothing in this package imports yfinance, Polygon or any other feed, so
    swapping the source touches only the caller.
    """
    return OHLCVSeries(symbol, df, timeframe=timeframe)


def synthetic_ohlcv(
    n: int = 600,
    seed: int = 7,
    start: float = 100.0,
    vol: float = 0.0015,
    freq: str = "1min",
) -> pd.DataFrame:
    """A deterministic geometric random walk, for smoke tests and examples.

    Ships with the package rather than living in the test tree so there is
    exactly ONE canonical import path for it. A module under tests/ that
    other test modules import is reachable both as `tests.backtest.x` and
    `swarm_trading.tests.backtest.x` (this repo imports itself as
    `swarm_trading.*`), which mypy rejects as the same file under two module
    names.

    Depends only on (n, seed, start, vol), so a caller asserting
    reproducibility of the ENGINE has a reproducible INPUT to assert it on.
    Not market data and not a model of any instrument — a price path with
    the right shape to exercise the replay loop.
    """
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, vol, n)
    close = start * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": rng.integers(100, 1000, n).astype(float),
        },
        index=pd.date_range("2026-01-01", periods=n, freq=freq),
    )
