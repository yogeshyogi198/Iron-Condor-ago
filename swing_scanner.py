"""
NH Mix Rules Scanner (Chartink replica) - Zerodha Kite Connect
Matches the verified chartink scan (screenshot 21 Jun 2026).

SETUP
-----
pip install kiteconnect pandas numpy

Environment variables required:
    KITE_API_KEY      - your Kite Connect app's API key
    KITE_ACCESS_TOKEN - today's access token

Access token must be regenerated daily:

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key="your_api_key")
    print(kite.login_url())          # open this, log in, copy request_token from redirect URL
    data = kite.generate_session("request_token_here", api_secret="your_api_secret")
    print(data["access_token"])      # set this as KITE_ACCESS_TOKEN

SCAN LOGIC (verified against your chartink screenshot)
--------------------------------------------------------
Stock passes ALL of:

  Group A - passes ANY 1 of:
    (a) Weekly Macd Line(21,3,9) >= Weekly Macd Signal(21,3,9)
    (b) ALL of:
          Weekly Macd Line(11,3,9) > Weekly Macd Signal(11,3,9)
          1 week ago Macd Line(11,3,9) <= 1 week ago Macd Signal(11,3,9)
          1 week ago Max(7, Daily Macd Histogram(21,3,9)) < 0

  + Weekly Rsi(9) >= 40
  + Weekly Wma(Daily Rsi(9), 11) < Weekly Rsi(9)
  + Daily Close > 50
  + 1 day ago Volume > 50000
  + Market Cap > 1000
  + Weekly Macd Histogram(21,3,9) > 0
  + Weekly HA-Close > Weekly HA-Open
  + 1 week ago HA-Close > 1 week ago HA-Open
  + Daily Close > Daily Open
  + Weekly Min(10, Daily Macd Histogram(21,3,9)) < -20
  + Weekly Volume > Weekly Sma(Daily Close, 7)

IMPORTANT: "Daily Macd Histogram(21,3,9)" and "Daily Rsi(9)" inside the
Weekly Min/Max/Wma wrappers are computed on DAILY closes (not weekly), then
sampled at each week's last trading day before the weekly rolling function
is applied. This is different from the plain "Weekly Macd..."/"Weekly Rsi..."
conditions elsewhere, which are computed directly on weekly close prices.
Mixing these up changes results significantly (daily EMA(21) ~1 month vs
weekly EMA(21) ~5 months) - this script keeps them as two separate series.

LIMITATIONS
-----------
1. Market Cap is NOT available from Kite's API. Approximated using an
   optional market_cap.csv (columns: symbol,market_cap_cr). Missing file =
   filter is SKIPPED (logged as a warning, not silently ignored).
2. Kite has no native 'week' interval. Weekly candles are built by
   resampling daily data, week ending Friday (W-FRI).
3. Scanning the full NSE EQ universe (~2000 symbols) takes ~15-20 minutes
   due to Kite's historical-data rate limit. Run as a scheduled job after
   market close, not as a real-time scanner.
"""

import json
import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from kiteconnect import KiteConnect

import telegram_logger

# ---------------- CONFIG ----------------
BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"

MARKET_CAP_CSV = "market_cap.csv"
MIN_MARKET_CAP_CR = 1000
MIN_DAILY_CLOSE = 50
MIN_VOLUME_1D_AGO = 50000

LOOKBACK_DAYS = 365 * 4
RATE_LIMIT_SLEEP = 0.35
KITE_TIMEOUT = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("swing_scanner")


# ---------------- INDICATOR HELPERS ----------------
def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def macd_line_signal_hist(series: pd.Series, a: int, b: int, c: int):
    macd_l = ema(series, a) - ema(series, b)
    signal = ema(macd_l, c)
    histogram = macd_l - signal
    return macd_l, signal, histogram


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha = pd.DataFrame(index=df.index)
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = [(df["open"].iloc[0] + df["close"].iloc[0]) / 2]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + ha["ha_close"].iloc[i - 1]) / 2)
    ha["ha_open"] = ha_open
    ha["ha_high"] = pd.concat([df["high"], ha["ha_open"], ha["ha_close"]], axis=1).max(axis=1)
    ha["ha_low"] = pd.concat([df["low"], ha["ha_open"], ha["ha_close"]], axis=1).min(axis=1)
    return ha


# ---------------- DATA FETCH ----------------
def get_kite() -> KiteConnect:
    cfg = json.loads(CONFIG_FILE.read_text())
    api_key = cfg["api_key"]
    access_token = cfg["access_token"]
    kite = KiteConnect(api_key=api_key, timeout=KITE_TIMEOUT)
    kite.set_access_token(access_token)
    return kite


def get_universe(kite: KiteConnect) -> pd.DataFrame:
    instruments = kite.instruments("NSE")
    df = pd.DataFrame(instruments)
    df = df[(df["segment"] == "NSE") & (df["instrument_type"] == "EQ")]
    return df[["instrument_token", "tradingsymbol"]].reset_index(drop=True)


def fetch_daily(kite: KiteConnect, token: int, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    try:
        data = kite.historical_data(token, from_date, to_date, interval="day")
    except Exception:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    return df


def to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return daily_df.resample("W-FRI").agg(agg).dropna()


def load_market_cap_map():
    if not os.path.exists(MARKET_CAP_CSV):
        log.warning("market_cap.csv not found - Market Cap filter will be SKIPPED.")
        return None
    mc = pd.read_csv(MARKET_CAP_CSV)
    return dict(zip(mc["symbol"], mc["market_cap_cr"]))


# ---------------- SCAN LOGIC ----------------
def build_indicators(daily_df: pd.DataFrame, weekly_df: pd.DataFrame) -> pd.DataFrame:
    w = weekly_df.copy()
    w = pd.concat([w, heikin_ashi(w)], axis=1)

    # --- Weekly-native MACD (computed on weekly close) ---
    w["macd_line_21_3"], w["macd_signal_21_3_9"], w["macd_hist_21_3_9"] = macd_line_signal_hist(w["close"], 21, 3, 9)
    w["macd_line_11_3"], w["macd_signal_11_3_9"], _ = macd_line_signal_hist(w["close"], 11, 3, 9)

    # --- Weekly-native RSI ---
    w["rsi9"] = rsi(w["close"], 9)

    # --- Daily-computed RSI(9), sampled at each week's last trading day ---
    daily_rsi9 = rsi(daily_df["close"], 9)
    daily_rsi9_w = daily_rsi9.resample("W-FRI").last().reindex(w.index)
    w["wma_dailyrsi9_11"] = wma(daily_rsi9_w, 11)

    # --- Daily-computed MACD Histogram(21,3,9), sampled at each week's last trading day ---
    _, _, daily_hist = macd_line_signal_hist(daily_df["close"], 21, 3, 9)
    daily_hist_w = daily_hist.resample("W-FRI").last().reindex(w.index)
    w["max7_daily_hist"] = daily_hist_w.rolling(7).max().shift(1)   # "1 week ago Max(7, ...)"
    w["min10_daily_hist"] = daily_hist_w.rolling(10).min()          # "Weekly Min(10, ...)" (current)

    # --- Weekly Sma(Daily Close,7) == Sma(weekly close,7) since weekly close IS the
    #     last daily close of the week ---
    w["sma_close_7"] = w["close"].rolling(7).mean()

    return w


def evaluate(symbol: str, weekly: pd.DataFrame, daily: pd.DataFrame, mc_map) -> bool:
    if len(weekly) < 50 or len(daily) < 5:
        return False

    cur, prev = weekly.iloc[-1], weekly.iloc[-2]

    cond1 = cur["macd_line_21_3"] >= cur["macd_signal_21_3_9"]

    cond2 = (
        cur["macd_line_11_3"] > cur["macd_signal_11_3_9"]
        and prev["macd_line_11_3"] <= prev["macd_signal_11_3_9"]
        and prev["max7_daily_hist"] < 0
    )

    if not (cond1 or cond2):
        return False

    if not (cur["rsi9"] >= 40):
        return False
    if not (cur["wma_dailyrsi9_11"] < cur["rsi9"]):
        return False

    d_today, d_yday = daily.iloc[-1], daily.iloc[-2]
    if not (d_today["close"] > MIN_DAILY_CLOSE):
        return False
    if not (d_yday["volume"] > MIN_VOLUME_1D_AGO):
        return False

    if mc_map is not None:
        mcap = mc_map.get(symbol)
        if mcap is None or mcap <= MIN_MARKET_CAP_CR:
            return False

    if not (cur["macd_hist_21_3_9"] > 0):
        return False
    if not (cur["ha_close"] > cur["ha_open"]):
        return False
    if not (prev["ha_close"] > prev["ha_open"]):
        return False
    if not (d_today["close"] > d_today["open"]):
        return False
    if not (cur["min10_daily_hist"] < -20):
        return False
    if not (cur["volume"] > cur["sma_close_7"]):
        return False

    return True


# ---------------- MAIN ----------------
def main():
    if not CONFIG_FILE.exists():
        raise SystemExit("kite_config.json not found. Run iron_condor_algo.py --login first.")

    kite = get_kite()
    universe = get_universe(kite)
    mc_map = load_market_cap_map()

    qualified = []
    total = len(universe)
    log.info(f"Scanning {total} NSE equities...")

    for i, row in universe.iterrows():
        symbol, token = row["tradingsymbol"], row["instrument_token"]
        try:
            daily = fetch_daily(kite, token)
            time.sleep(RATE_LIMIT_SLEEP)
            if daily.empty or len(daily) < 250:
                continue

            weekly_raw = to_weekly(daily)
            weekly = build_indicators(daily, weekly_raw)

            if evaluate(symbol, weekly, daily, mc_map):
                qualified.append(symbol)

        except Exception:
            continue

        if (i + 1) % 500 == 0:
            log.info(f"Progress: {i + 1}/{total}")

    log.info(f"Scan complete. {len(qualified)} stocks matched.")
    for s in qualified:
        print(s)

    if qualified:
        lines = [
            "📊 NH MIX SCAN RESULTS",
            f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"Qualified: {len(qualified)} stocks",
            "",
        ]
        for s in qualified:
            lines.append(f"• {s}")
        telegram_logger.send_telegram("\n".join(lines), level="INFO")
    else:
        telegram_logger.send_telegram(
            "📊 NH MIX SCAN\nNo stocks qualified.", level="INFO")

    return qualified


if __name__ == "__main__":
    main()
