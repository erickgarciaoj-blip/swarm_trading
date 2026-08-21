"""
No-look-ahead guarantees.

The engine precomputes indicator columns once, vectorized, instead of
recomputing them per bar. That is only legitimate if every indicator is
causal — row i depending on rows <= i and nothing after. These tests prove
it against the live code path rather than assuming it, so a future
non-causal indicator (a centered window, a `.shift(-1)`, a full-series
normalisation) fails here instead of silently inflating a backtest.
"""

from __future__ import annotations

import pandas as pd
import pytest

from swarm_trading.backtest.data import (
    OHLCVSeries,
    OHLCVSourceError,
    compute_indicator_frame,
    load_dataframe,
    normalise_ohlcv,
    synthetic_ohlcv,
)
from swarm_trading.core.models import Symbol
from swarm_trading.data.feeds.market_feed import MarketFeed

# Enough bars for EMA-200 to have converged well past its seed.
BARS = 400


@pytest.fixture
def series() -> OHLCVSeries:
    return load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(BARS, seed=3))


@pytest.mark.parametrize("bar_index", [250, 300, 349, BARS - 1])
def test_precomputed_indicators_match_the_live_feed_on_truncated_data(bar_index):
    """The decisive test.

    MarketFeed._compute_indicators() is what the RUNTIME calls, and it
    returns the last row of whatever frame it is given. Feeding it df[:i+1]
    — literally all the data that existed at bar i — must reproduce the
    precomputed row at i exactly. If it does not, the replay is seeing the
    future.
    """
    df = synthetic_ohlcv(BARS, seed=3)
    series = load_dataframe(Symbol.XAUUSD, df)

    truncated = df.iloc[: bar_index + 1]
    live = MarketFeed(backend="yfinance")._compute_indicators(truncated)
    replayed = series.indicators_at(bar_index)

    assert replayed == live, f"divergence at bar {bar_index}"


def test_indicators_are_unchanged_by_appending_future_bars():
    """Same claim, approached from the other side: extending the frame with
    later bars must not alter any earlier row."""
    df = synthetic_ohlcv(BARS, seed=5)
    short = compute_indicator_frame(df.iloc[:300])
    long = compute_indicator_frame(df)

    pd.testing.assert_frame_equal(short, long.iloc[:300])


def test_candle_window_never_reaches_past_the_requested_index(series):
    for index in (200, 250, 399):
        window = series.window(index, lookback=100)
        assert window[-1] == series.candle(index)
        assert len(window) == 100
        # Every timestamp in the window is at or before the current bar's.
        assert all(c.timestamp <= series.candle(index).timestamp for c in window)


def test_window_is_truncated_at_the_start_not_padded_with_future_bars(series):
    window = series.window(5, lookback=100)

    assert len(window) == 6  # bars 0..5, not 100
    assert window[-1] == series.candle(5)


def test_window_rejects_an_out_of_range_index(series):
    with pytest.raises(IndexError):
        series.window(BARS, lookback=10)
    with pytest.raises(IndexError):
        series.window(-1, lookback=10)


def test_warmup_indicators_are_absent_rather_than_zero_filled():
    """Before a rolling window has enough history the value is undefined.
    Dropping the key makes an agent's `.get()` return its own default, which
    is what the live feed produces; zero-filling would hand strategies a
    fabricated RSI of 0."""
    series = load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(300, seed=9))

    first_bar = series.indicators_at(0)
    assert "rsi_14" not in first_bar  # needs 14 bars
    assert "atr_14" not in first_bar

    settled = series.indicators_at(250)
    assert set(settled) == {"rsi_14", "atr_14", "ema_20", "ema_50", "ema_200"}


def test_rounding_matches_the_live_feed():
    """MarketFeed rounds before agents see the values, and a threshold
    comparison can flip on the 4th decimal — so the replay must round the
    same way or diverge from the runtime on borderline bars."""
    series = load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(BARS, seed=11))
    indicators = series.indicators_at(300)

    assert indicators["rsi_14"] == round(indicators["rsi_14"], 3)
    assert indicators["atr_14"] == round(indicators["atr_14"], 6)
    for ema in ("ema_20", "ema_50", "ema_200"):
        assert indicators[ema] == round(indicators[ema], 5)


# ─── Loader hygiene ───────────────────────────────────────────────────────


def test_lowercase_columns_and_timestamp_column_are_accepted():
    df = synthetic_ohlcv(50).reset_index(names="timestamp")
    df.columns = [str(c).lower() for c in df.columns]

    series = load_dataframe(Symbol.OIL, df)

    assert len(series) == 50
    assert series.candle(0).symbol == Symbol.OIL


def test_unsorted_input_is_ordered_chronologically():
    """ "Bar i is the present" is meaningless if the rows are not in order.
    CSV exports are commonly newest-first."""
    df = synthetic_ohlcv(50, seed=13)
    series = load_dataframe(Symbol.XAUUSD, df.iloc[::-1])

    timestamps = [series.candle(i).timestamp for i in range(len(series))]
    assert timestamps == sorted(timestamps)


def test_duplicate_timestamps_are_rejected():
    df = synthetic_ohlcv(20)
    with pytest.raises(OHLCVSourceError, match="duplicate"):
        load_dataframe(Symbol.XAUUSD, pd.concat([df, df]))


def test_missing_columns_are_rejected_with_a_useful_message():
    df = synthetic_ohlcv(20).drop(columns=["High"])
    with pytest.raises(OHLCVSourceError, match="High"):
        load_dataframe(Symbol.XAUUSD, df)


def test_multiindex_columns_are_flattened_like_the_live_feed():
    """yfinance returns (field, ticker) columns even for a single symbol."""
    df = synthetic_ohlcv(30)
    df.columns = pd.MultiIndex.from_product([df.columns, ["GC=F"]])

    series = load_dataframe(Symbol.XAUUSD, df)

    assert len(series) == 30


def test_frame_property_returns_a_copy_not_the_internal_state():
    series = load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(30))
    frame = series.frame
    frame.loc[frame.index[0], "Close"] = -1.0

    assert series.candle(0).close != -1.0


def test_csv_round_trip(tmp_path):
    from swarm_trading.backtest.data import load_csv

    path = tmp_path / "bars.csv"
    synthetic_ohlcv(80, seed=17).to_csv(path, index_label="timestamp")

    series = load_csv(Symbol.PLTR, path)

    assert len(series) == 80
    assert series.candle(0).symbol == Symbol.PLTR


def test_empty_input_is_rejected():
    with pytest.raises(OHLCVSourceError):
        load_dataframe(Symbol.XAUUSD, synthetic_ohlcv(10).iloc[0:0])


def test_normalise_does_not_mutate_the_caller_frame():
    df = synthetic_ohlcv(20)
    before = df.copy()
    normalise_ohlcv(df)

    pd.testing.assert_frame_equal(df, before)
