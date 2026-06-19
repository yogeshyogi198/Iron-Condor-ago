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


# ── Heikin Ashi ────────────────────────────────────────────

def compute_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha["HA_Close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    ha_open = np.empty(len(df))
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i - 1] + ha["HA_Close"].iloc[i - 1]) / 2.0
    ha["HA_Open"] = ha_open

    ha["HA_High"] = np.maximum(
        df["high"].values,
        np.maximum(ha["HA_Open"].values, ha["HA_Close"].values),
    )
    ha["HA_Low"] = np.minimum(
        df["low"].values,
        np.minimum(ha["HA_Open"].values, ha["HA_Close"].values),
    )
    return ha


# ── Entry confirmation: Setup A, Setup B + overall filters ──

def all_confirmations(
    weekly_df: pd.DataFrame,
    daily_data: list | None = None,
    market_cap: float = float("inf"),
) -> bool:
    """(Setup A OR Setup B) AND all overall filters must pass."""
    if len(weekly_df) < MIN_WEEKLY_BARS:
        return False

    ha = compute_heikin_ashi(weekly_df)
    ha_close = ha["HA_Close"]
    ha_open = ha["HA_Open"]

    # ── MACD(3,21,9) — used in Setup A + overall filters ──
    macd_line_a, signal_line_a, hist_a = macd(
        weekly_df["close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    )

    # ── MACD(3,11,9) — used in Setup B ──
    macd_line_b, signal_line_b, _ = macd(
        weekly_df["close"], MACD_B_FAST, MACD_B_SLOW, MACD_B_SIGNAL,
    )

    # ── Shared HMA / rolling calculations ──
    hma_ha_30 = hma(ha_close, 30)
    hma_ha_44 = hma(ha_close, 44)
    min_12_ha = ha_close.rolling(12).min()
    hma_min_12 = hma(min_12_ha, 30)

    # ═══════════════════════════════════════════════════════
    # Setup A — all must be true
    # ═══════════════════════════════════════════════════════
    setup_a = (
        macd_line_a.iloc[-1] >= signal_line_a.iloc[-1]
        and ha_close.iloc[-2] <= hma_ha_30.iloc[-2]
        and ha_close.iloc[-1] > hma_ha_30.iloc[-1]
        and ha_close.iloc[-2] < hma_min_12.iloc[-2]
    )

    # ═══════════════════════════════════════════════════════
    # Setup B — all must be true
    # ═══════════════════════════════════════════════════════
    max_7_hist = hist_a.rolling(7).max()

    setup_b = (
        ha_close.iloc[-1] >= hma_ha_44.iloc[-1]
        and ha_close.iloc[-2] < hma_min_12.iloc[-2]
        and macd_line_b.iloc[-2] <= signal_line_b.iloc[-2]
        and macd_line_b.iloc[-1] > signal_line_b.iloc[-1]
        and max_7_hist.iloc[-2] < 0
    )

    if not (setup_a or setup_b):
        return False

    # ═══════════════════════════════════════════════════════
    # Overall filters — always required
    # ═══════════════════════════════════════════════════════

    rsi_vals = rsi(weekly_df["close"], RSI_PERIOD)

    # 1. Weekly RSI(9) >= 40
    if rsi_vals.iloc[-1] < 40:
        return False

    # 2. Weekly WMA(RSI(9), 11) < Weekly RSI(9)
    if wma(rsi_vals, 11).iloc[-1] >= rsi_vals.iloc[-1]:
        return False

    # 3. Daily Close > 50
    if daily_data and len(daily_data) and daily_data[-1]["close"] <= 50:
        return False

    # 4. Volume(1 day ago) > 50,000
    if daily_data and len(daily_data) > 1 and daily_data[-2]["volume"] <= 50000:
        return False

    # 5. Market Cap > 1000
    if market_cap <= 1000:
        return False

    # 6. Weekly MACD Histogram(21,3,9) > 0
    if hist_a.iloc[-1] <= 0:
        return False

    # 7. Weekly HA-Close > HA-Open (and 1 week ago also)
    if not (
        ha_close.iloc[-1] > ha_open.iloc[-1]
        and ha_close.iloc[-2] > ha_open.iloc[-2]
    ):
        return False

    # 8. Daily Close > Daily Open
    if daily_data and len(daily_data) and daily_data[-1]["close"] <= daily_data[-1]["open"]:
        return False

    # 9. Weekly Min(10, MACD Histogram(21,3,9)) < -20
    if hist_a.rolling(10).min().iloc[-1] >= -20:
        return False

    # 10. Weekly Volume > SMA(Weekly Close, 7)
    if weekly_df["volume"].iloc[-1] <= weekly_df["close"].rolling(7).mean().iloc[-1]:
        return False

    return True


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
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    MACD_B_FAST, MACD_B_SLOW, MACD_B_SIGNAL,
    RSI_PERIOD,
    MIN_WEEKLY_BARS,
)
