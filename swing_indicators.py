"""
Swing Trading — Indicator Calculations
========================================
Pure functions: HMA, MACD, RSI + confirmation checkers.
All operate on pandas Series / DataFrames.
"""

import numpy as np
import pandas as pd


# ── Weighted Moving Average ────────────────────────────────

def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    def _wma(x):
        return np.dot(x, weights) / weights.sum()
    return series.rolling(window=period).apply(_wma, raw=True).astype(float)


# ── Hull Moving Average ────────────────────────────────────

def hma(series: pd.Series, period: int) -> pd.Series:
    half = int(period / 2)
    sqrt_period = int(np.sqrt(period))
    wma_half = wma(series, half)
    wma_full = wma(series, period)
    raw = 2 * wma_half - wma_full
    return wma(raw, sqrt_period)


# ── MACD (fast, slow, signal) ──────────────────────────────

def macd(close: pd.Series, fast: int = 3, slow: int = 21, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


# ── RSI (Wilder smoothing) ─────────────────────────────────

def rsi(close: pd.Series, period: int = 9) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


# ── Confirmation 1: HMA Setup ─────────────────────────────

def check_hma_setup(df: pd.DataFrame) -> bool:
    if len(df) < HMA_SLOW + 5:
        return False
    hma_fast = hma(df["close"], HMA_FAST)
    hma_slow = hma(df["close"], HMA_SLOW)
    curr_close = df["close"].iloc[-1]
    prev_close = df["close"].iloc[-2]
    curr_hma_fast = hma_fast.iloc[-1]
    prev_hma_fast = hma_fast.iloc[-2]
    curr_hma_slow = hma_slow.iloc[-1]
    if pd.isna(curr_hma_fast) or pd.isna(curr_hma_slow):
        return False
    crossed = prev_close <= prev_hma_fast and curr_close > curr_hma_fast
    in_zone = curr_hma_slow < curr_close < curr_hma_fast
    return crossed and in_zone


# ── Confirmation 2: MACD Setup ────────────────────────────

def check_macd_setup(macd_line: pd.Series, signal_line: pd.Series,
                     histogram: pd.Series) -> bool:
    if len(histogram) < MACD_MIN_HISTOGRAM_BARS + 5:
        return False
    recent = histogram.iloc[-(MACD_MIN_HISTOGRAM_BARS + 5):]
    neg_run = 0
    for val in recent:
        if val < 0:
            neg_run += 1
            if neg_run >= MACD_MIN_HISTOGRAM_BARS:
                break
        else:
            neg_run = 0
    else:
        return False
    crossover = (macd_line.iloc[-2] <= signal_line.iloc[-2] and
                 macd_line.iloc[-1] > signal_line.iloc[-1])
    first_green = histogram.iloc[-1] > 0 and histogram.iloc[-2] <= 0
    return crossover and first_green


# ── Confirmation 3: RSI Setup ─────────────────────────────

def check_rsi_setup(rsi_vals: pd.Series) -> bool:
    if len(rsi_vals) < RSI_WMA_PERIOD + 5:
        return False
    rsi_sma = rsi_vals.rolling(window=RSI_SMA_PERIOD).mean()
    rsi_wma = wma(rsi_vals, RSI_WMA_PERIOD)
    curr_rsi = rsi_vals.iloc[-1]
    curr_sma = rsi_sma.iloc[-1]
    curr_wma = rsi_wma.iloc[-1]
    if pd.isna(curr_sma) or pd.isna(curr_wma):
        return False
    prev_rsi = rsi_vals.iloc[-2]
    prev_sma = rsi_sma.iloc[-2]
    prev_wma = rsi_wma.iloc[-2]
    if pd.isna(prev_sma) or pd.isna(prev_wma):
        return curr_rsi > curr_wma and curr_sma > curr_wma
    above_wma = curr_rsi > curr_wma and curr_sma > curr_wma
    crossed = ((prev_rsi <= prev_wma and curr_rsi > curr_wma) or
               (prev_sma <= prev_wma and curr_sma > curr_wma))
    return above_wma or crossed


# ── All 3 confirmations ───────────────────────────────────

def all_confirmations(df: pd.DataFrame) -> bool:
    if len(df) < max(HMA_SLOW, RSI_WMA_PERIOD) + 10:
        return False
    rsi_vals = rsi(df["close"], RSI_PERIOD)
    macd_line, signal_line, histogram = macd(
        df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    c1 = check_hma_setup(df)
    c2 = check_macd_setup(macd_line, signal_line, histogram)
    c3 = check_rsi_setup(rsi_vals)
    return c1 and c2 and c3


# ── Swing Low / High ──────────────────────────────────────

def swing_levels(df: pd.DataFrame, lookback: int = 30) -> tuple:
    recent = df.tail(lookback)
    swing_low = recent["low"].min()
    swing_high = recent["high"].max()
    return round(swing_low, 2), round(swing_high, 2)


# ── Resample daily → weekly ───────────────────────────────

def resample_weekly(daily: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(daily)
    dates = pd.to_datetime([c["date"] for c in daily])
    df["date"] = pd.to_datetime(dates.strftime("%Y-%m-%d"))
    df = df.set_index("date").sort_index()
    weekly = df.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return weekly


# ── Import constants (kept at bottom to avoid circular refs) ──
from swing_config import (
    HMA_FAST, HMA_SLOW,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL, MACD_MIN_HISTOGRAM_BARS,
    RSI_PERIOD, RSI_SMA_PERIOD, RSI_WMA_PERIOD,
)
