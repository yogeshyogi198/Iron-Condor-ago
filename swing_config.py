"""
Swing Trading Strategy — Configuration
========================================
All tunable parameters in one place.
"""

from datetime import time

# ── Stock Universe ──────────────────────────────────────────
# Nifty 500 index constituents (fetched dynamically, cached locally)
NIFTY_500_INDEX = "NIFTY%20500"
NIFTY_500_URL = f"https://www.nseindia.com/api/equity-stockIndices?index={NIFTY_500_INDEX}"
NIFTY_500_CACHE = "nifty500_cache.json"

# ── Confirmation 1: Hull Moving Average ────────────────────
HMA_FAST = 30
HMA_SLOW = 44

# ── Confirmation 2: MACD ───────────────────────────────────
MACD_FAST = 3
MACD_SLOW = 21
MACD_SIGNAL = 9
MACD_MIN_HISTOGRAM_BARS = 8

# ── Confirmation 3: Custom RSI ─────────────────────────────
RSI_PERIOD = 9
RSI_SMA_PERIOD = 3
RSI_WMA_PERIOD = 21

# ── Swing Levels ───────────────────────────────────────────
SWING_LOOKBACK_WEEKS = 30

# ── Portfolio & Rebalancing ────────────────────────────────
MAX_POSITIONS = 20
ALLOCATION_PCT = 0.05
REBALANCE_THRESHOLD = 0.03

# ── Market Hours (IST) ─────────────────────────────────────
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# ── Scanner Schedule (day-of-week: Sunday=6, Monday=0) ─────
SCANNER_DAYS = [6, 0]

# ── Historical Data ────────────────────────────────────────
DAILY_LOOKBACK_DAYS = 300
KITE_REQUEST_DELAY = 0.35
