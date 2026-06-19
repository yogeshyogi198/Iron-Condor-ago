"""
Module 1 — Weekly Swing Scanner
=================================
Run:  python swing_scanner.py

Schedule via Task Scheduler: Sunday 8 PM or Monday 7:30 AM IST.

Workflow:
  1. Fetch Nifty 500 constituents (NSE API -> cached locally)
  2. Build instrument-token map from Kite NSE segment
  3. For each stock: fetch daily data -> resample to weekly ->
     compute all 3 confirmations -> qualify / reject
  4. For qualified stocks: compute SL (recent swing low) &
     target (previous swing high)
  5. Send formatted Telegram watchlist
"""

import csv
import io
import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import requests
from kiteconnect import KiteConnect

import telegram_logger
from swing_config import (
    NIFTY_500_URL, NIFTY_500_CACHE,
    HMA_FAST, HMA_SLOW, RSI_PERIOD,
    DAILY_LOOKBACK_DAYS, KITE_REQUEST_DELAY,
    SWING_LOOKBACK_WEEKS, MIN_WEEKLY_BARS,
)
from swing_indicators import (
    hma, rsi,
    all_confirmations, swing_levels, resample_weekly,
)

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
NIFTY_CACHE_FILE = BOT_DIR / NIFTY_500_CACHE

NIFTY50_HARDCODED = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
    "BAJFINANCE", "WIPRO", "ULTRACEMCO", "NESTLEIND", "TECHM",
    "SUNPHARMA", "POWERGRID", "NTPC", "ONGC", "JSWSTEEL",
    "TATAMOTORS", "HCLTECH", "M&M", "TATASTEEL", "ADANIENT",
    "BAJAJFINSV", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HEROMOTOCO", "HINDALCO", "INDUSINDBK", "CIPLA",
    "APOLLOHOSP", "ADANIPORTS", "BPCL", "BRITANNIA", "SBILIFE",
    "HDFCLIFE", "TATACONSUM", "UPL", "VEDL", "SHREECEM",
]

NIFTY_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"


# ── Nifty 500 Symbol List ──────────────────────────────────

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _fetch_with_session(urls: list[tuple[str, str]], max_attempts: int = 2) -> list[str] | None:
    """Try multiple URLs with a shared session. Each tuple is (label, url)."""
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "DNT": "1",
        "Connection": "keep-alive",
    }
    session.headers.update(headers)
    for attempt in range(max_attempts):
        try:
            session.get("https://www.nseindia.com", timeout=10)
        except Exception:
            if attempt == max_attempts - 1:
                return None
            time.sleep(2)
            continue
        for label, url in urls:
            try:
                resp = session.get(url, timeout=15)
                if resp.status_code != 200:
                    continue
                if url.endswith(".csv"):
                    reader = csv.DictReader(io.StringIO(resp.text))
                    symbols = [row["Symbol"].strip() for row in reader if row.get("Symbol")]
                else:
                    data = resp.json()
                    symbols = [item["symbol"] for item in data.get("data", [])]
                if symbols:
                    return symbols
            except Exception:
                continue
        if attempt < max_attempts - 1:
            time.sleep(2)
    return None


def get_nifty500_symbols() -> list[str]:
    cached = []
    if NIFTY_CACHE_FILE.exists():
        try:
            cached = json.loads(NIFTY_CACHE_FILE.read_text())
            age = datetime.now() - datetime.fromtimestamp(
                NIFTY_CACHE_FILE.stat().st_mtime)
            if age.days < 7 and cached:
                return cached
        except Exception:
            pass

    # Method 1: Nifty 500 CSV (most reliable)
    symbols = _fetch_with_session([("nifty500_csv", NIFTY_CSV_URL)])
    if symbols:
        NIFTY_CACHE_FILE.write_text(json.dumps(symbols))
        print(f"  Fetched {len(symbols)} Nifty 500 symbols from NSE CSV")
        return symbols

    # Method 2: Nifty 50 + Next 50 + Midcap 150 + Smallcap 250 = ~500
    composite_urls = [
        ("nifty50_csv", NIFTY50_CSV_URL),
        ("nifty500_json", NIFTY_500_URL),
    ]
    symbols = _fetch_with_session(composite_urls)
    if symbols:
        NIFTY_CACHE_FILE.write_text(json.dumps(symbols))
        print(f"  Fetched {len(symbols)} symbols from NSE composite")
        return symbols

    if cached:
        print("  Using cached Nifty 500 list (NSE fetch failed)")
        return cached

    print("  WARNING: Could not fetch Nifty 500. Falling back to all NSE equities.")
    return []


# ── Kite Helpers ───────────────────────────────────────────

def _build_token_map(kite: KiteConnect) -> dict[str, int]:
    instruments = kite.instruments("NSE")
    result = {}
    for r in instruments:
        itype = r.get("instrument_type", "")
        tsym = r.get("tradingsymbol", "")
        if (itype == "EQ"
                and tsym
                and "-" not in tsym
                and not tsym[0].isdigit()
                and len(tsym) <= 10):
            result[tsym] = int(r["instrument_token"])
    return result


def _fetch_daily(kite: KiteConnect, token: int,
                 days: int = DAILY_LOOKBACK_DAYS) -> list[dict]:
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    try:
        return kite.historical_data(token, from_dt, to_dt, "day")
    except Exception:
        return []


def _get_current_price(kite: KiteConnect, symbol: str) -> float | None:
    try:
        key = f"NSE:{symbol}"
        q = kite.ltp(key)
        return float(q[key]["last_price"])
    except Exception:
        return None


# ── Main Scanner ───────────────────────────────────────────

def main():
    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if not api_key or not access_token:
        print("Not authenticated. Run iron_condor_algo.py --login first.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=30)
    print("Swing Scanner — Weekly Scan")
    print(f"{'='*50}")

    # Build token map FIRST (single instruments() call)
    print("  Building token map ...")
    token_map = _build_token_map(kite)
    print(f"  Found {len(token_map)} NSE equity tokens")

    # Determine universe with fallback priority
    symbols = []
    source_name = ""

    nifty500 = get_nifty500_symbols()
    nifty500 = [s for s in nifty500 if s and not s[0].isdigit()]
    if nifty500:
        symbols = nifty500
        source_name = "Nifty 500"
    else:
        # Fallback 1: token_map keys (clean EQ symbols)
        all_equities = sorted(token_map.keys())
        if len(all_equities) >= 50:
            symbols = all_equities
            source_name = "all NSE equities"
        else:
            # Fallback 2: hardcoded Nifty 50
            symbols = NIFTY50_HARDCODED
            source_name = "hardcoded Nifty 50"

    print(f"  Universe source: {source_name} — {len(symbols)} stocks")

    telegram_logger.send_telegram(
        f"📊 SWING SCAN started — {len(symbols)} stocks, "
        f"{len(token_map)} tokens available", level="INFO")

    qualified = []
    errors = 0
    skipped = 0
    last_tg_progress = 0

    for i, symbol in enumerate(symbols, 1):
        token = token_map.get(symbol)
        if token is None:
            skipped += 1
            continue
        print(f"  [{i}/{len(symbols)}] {symbol} ... ", end="", flush=True)
        daily = _fetch_daily(kite, token)
        if len(daily) < 100:
            print("skipped (insufficient data)")
            skipped += 1
            time.sleep(KITE_REQUEST_DELAY)
            continue
        weekly = resample_weekly(daily)
        if len(weekly) < MIN_WEEKLY_BARS:
            print(f"skipped (<{MIN_WEEKLY_BARS} weeks)")
            skipped += 1
            time.sleep(KITE_REQUEST_DELAY)
            continue
        try:
            confirmed = all_confirmations(weekly, daily_data=daily)
        except Exception:
            print("error")
            errors += 1
            time.sleep(KITE_REQUEST_DELAY)
            continue
        if not confirmed:
            print("no")
            time.sleep(KITE_REQUEST_DELAY)
            continue
        price = _get_current_price(kite, symbol)
        if price is None:
            print("no price")
            time.sleep(KITE_REQUEST_DELAY)
            continue
        close_series = weekly["close"]
        curr_hma_fast = hma(close_series, HMA_FAST).iloc[-1]
        curr_hma_slow = hma(close_series, HMA_SLOW).iloc[-1]
        rsi_val = rsi(close_series, RSI_PERIOD).iloc[-1]
        sl, target = swing_levels(weekly, SWING_LOOKBACK_WEEKS)
        qualified.append({
            "symbol": symbol,
            "price": round(price, 2),
            "hma_fast": round(curr_hma_fast, 2),
            "hma_slow": round(curr_hma_slow, 2),
            "rsi": round(rsi_val, 2),
            "stop_loss": round(sl, 2),
            "target": round(target, 2),
        })
        print("✓ QUALIFIED")
        time.sleep(KITE_REQUEST_DELAY)

        if i % 100 == 0:
            print(f"  Progress: {i}/{len(symbols)} — {len(qualified)} qualified")
            if i - last_tg_progress >= 200:
                telegram_logger.send_telegram(
                    f"📊 SWING SCAN progress: {i}/{len(symbols)} stocks, "
                    f"{len(qualified)} qualified", level="INFO")
                last_tg_progress = i

    print(f"\n{'='*50}")
    print(f"Scan complete: {len(qualified)} qualified, "
          f"{skipped} skipped, {errors} errors")

    if qualified:
        lines = ["📊 WEEKLY SWING SCAN RESULTS",
                 f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                 f"Qualified: {len(qualified)} / {len(symbols)} stocks",
                 ""]
        for i, q in enumerate(qualified, 1):
            lines.extend([
                f"{i}. {q['symbol']}",
                f"   LTP: ₹{q['price']}",
                f"   30HMA: {q['hma_fast']} | 44HMA: {q['hma_slow']}",
                f"   RSI(9): {q['rsi']}",
                f"   SL: {q['stop_loss']}  Target: {q['target']}",
                "",
            ])
        telegram_logger.send_telegram("\n".join(lines), level="INFO")
        print(f"  ✓ Alert sent ({len(qualified)} stocks)")
    else:
        telegram_logger.send_telegram(
            "📊 WEEKLY SWING SCAN\nNo stocks qualified this week.",
            level="INFO")
        print("  No qualified stocks")

    return qualified


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        tb = traceback.format_exc()
        print(f"FATAL: {tb}")
        telegram_logger.error_alert("SwingScanner", tb)
