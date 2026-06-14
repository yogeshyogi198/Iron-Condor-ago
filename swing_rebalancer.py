"""
Module 2 — Daily Rebalancing Monitor
======================================
Run:  python swing_rebalancer.py

Schedule via Task Scheduler every 30-60 min during market hours (9:15-15:30 IST).

Workflow:
  1. Verify market is open
  2. Fetch equity holdings from Zerodha
  3. For each holding, compute day change percentage
  4. If >= +3% → send SELL alert (book partial profits)
  5. If <= -3% → send BUY alert (reinvest at discount)
  6. Dedup: once alerted today for a symbol, skip it
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from kiteconnect import KiteConnect

import telegram_logger
from swing_config import (
    MARKET_OPEN, MARKET_CLOSE, REBALANCE_THRESHOLD,
)

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
ALERTS_TODAY_FILE = BOT_DIR / ".swing_alerts_today.json"


# ── Helpers ────────────────────────────────────────────────

def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def is_market_open() -> bool:
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _load_alerts_today() -> set:
    if ALERTS_TODAY_FILE.exists():
        try:
            data = json.loads(ALERTS_TODAY_FILE.read_text())
            date = data.get("date", "")
            if date == datetime.now().strftime("%Y-%m-%d"):
                return set(data.get("symbols", []))
        except Exception:
            pass
    return set()


def _save_alerts_today(symbols: set):
    ALERTS_TODAY_FILE.write_text(json.dumps({
        "date": datetime.now().strftime("%Y-%m-%d"),
        "symbols": list(symbols),
    }))


# ── Main Monitor ───────────────────────────────────────────

def main():
    if not is_market_open():
        print("Market closed. Run during 9:15 AM – 3:30 PM IST.")
        return

    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if not api_key or not access_token:
        print("Not authenticated. Run iron_condor_algo.py --login first.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=30)
    already_alerted = _load_alerts_today()
    print(f"Swing Rebalancer — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    try:
        holdings = kite.holdings()
    except Exception as e:
        print(f"  Could not fetch holdings: {e}")
        telegram_logger.error_alert("SwingRebalancer", str(e))
        return

    if not holdings:
        print("  No equity holdings found.")
        return

    print(f"  Holdings: {len(holdings)} stocks")

    # Build quote keys for all symbols
    sym_to_holding = {}
    for h in holdings:
        tsym = h.get("tradingsymbol", "")
        qty = h.get("quantity", 0)
        if qty <= 0:
            continue
        sym_to_holding[f"NSE:{tsym}"] = h

    if not sym_to_holding:
        print("  No active holdings with quantity > 0.")
        return

    quotes = {}
    for i in range(0, len(sym_to_holding), 500):
        batch = list(sym_to_holding.keys())[i:i + 500]
        try:
            quotes.update(kite.quote(batch))
        except Exception as e:
            print(f"  Quote fetch error: {e}")

    alerts = []
    for key, holding in sym_to_holding.items():
        symbol = holding["tradingsymbol"]
        qty = holding.get("quantity", 0)
        avg_price = float(holding.get("average_price", 0))
        quote = quotes.get(key, {})
        ltp = float(quote.get("last_price", 0))
        prev_close = float(quote.get("ohlc", {}).get("close", 0))

        if not ltp or not prev_close:
            continue

        day_change_pct = (ltp - prev_close) / prev_close
        day_change_str = f"{day_change_pct * 100:+.2f}%"

        if symbol in already_alerted:
            print(f"  {symbol}: {day_change_str} (already alerted today)")
            continue

        if day_change_pct >= REBALANCE_THRESHOLD:
            pnl = (ltp - avg_price) * qty
            msg = (
                f"🔔 STOCK {symbol} is UP {day_change_str}\n"
                f"   LTP: ₹{ltp:.2f} | Qty: {qty}\n"
                f"   Avg Cost: ₹{avg_price:.2f} | P&L: ₹{pnl:+,.0f}\n"
                f"   Consider selling 10% of position ({max(1, qty//10)} shares)"
            )
            alerts.append(msg)
            already_alerted.add(symbol)
            print(f"  ⬆ {symbol}: {day_change_str} — SELL alert")

        elif day_change_pct <= -REBALANCE_THRESHOLD:
            pnl = (ltp - avg_price) * qty
            msg = (
                f"🔔 STOCK {symbol} is DOWN {day_change_str}\n"
                f"   LTP: ₹{ltp:.2f} | Qty: {qty}\n"
                f"   Avg Cost: ₹{avg_price:.2f} | P&L: ₹{pnl:+,.0f}\n"
                f"   Consider reinvesting profits here"
            )
            alerts.append(msg)
            already_alerted.add(symbol)
            print(f"  ⬇ {symbol}: {day_change_str} — BUY alert")

        else:
            print(f"  {symbol}: {day_change_str}")

    if alerts:
        for alert in alerts:
            telegram_logger.send_telegram(alert, level="WARNING")
            time.sleep(0.5)
        print(f"\n  ✓ {len(alerts)} alert(s) sent")
    else:
        print("\n  No stocks crossed 3% threshold.")

    _save_alerts_today(already_alerted)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        telegram_logger.error_alert("SwingRebalancer", traceback.format_exc())
        raise
