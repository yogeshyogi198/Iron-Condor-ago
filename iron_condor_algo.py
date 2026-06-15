"""
Iron Condor Algo — NIFTY only | Premium-based strike selection
===============================================================
Strategy:
  - Normal day:  Sell strike ₹20-25 premium, Buy strike ₹4-6 premium
  - 0DTE day:    Same entry, exit when short premium ≈ ₹2 (theta decay)
  - Target:      ₹1000 profit (normal) or short premium ≤ ₹2 (0DTE)
  - Stop Loss:   When loss = total net credit received

Usage:
  python iron_condor_algo.py --login      # One-time login
  python iron_condor_algo.py              # Auto: scan → trade → monitor → exit
"""

import atexit
import csv
import io
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, time as dtime
from typing import Optional

# ── Console formatting (ANSI bold/bright for Windows Terminal / PowerShell) ──
BOLD = "\033[1m"
BRIGHT = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"

def bold(s: str) -> str:
    return f"{BOLD}{s}{RESET}"

def bright(s: str) -> str:
    return f"{BRIGHT}{s}{RESET}"

def green(s: str) -> str:
    return f"{GREEN}{s}{RESET}"

def red(s: str) -> str:
    return f"{RED}{s}{RESET}"

def cyan(s: str) -> str:
    return f"{CYAN}{s}{RESET}"

import requests
from kiteconnect import KiteConnect

import telegram_logger

# ---------------------------------------------------------------------------
# Persisted config
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "kite_config.json")

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Trade log
# ---------------------------------------------------------------------------

TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.csv")
TRADE_LOG_FIELDS = [
    "date", "strategy", "expiry", "entry_time", "exit_time",
    "entry_spot", "exit_spot", "entry_credit", "exit_value",
    "pnl", "charges", "max_profit_target", "stop_loss", "exit_reason",
]

def calc_charges(legs: list, lot_size: int, exchange: str = "NFO") -> float:
    """
    Exact Zerodha charges for options trade.
    Brokerage: ₹20 per order (each leg is one order)
    STT: 0.05% on sell premium value (sell side only)
    Transaction: NSE ₹50.5/crore or BSE ₹37.5/crore on total premium turnover
    SEBI: ₹10/crore on total premium turnover
    GST: 18% on (brokerage + transaction + SEBI)
    Stamp duty: ~0.002% of premium turnover
    """
    orders = len(legs)
    brokerage = orders * 20.0

    total_premium_turnover = 0.0
    sell_premium_value = 0.0

    for leg in legs:
        prem = float(leg.get("premium", 0))
        action = leg.get("action", "")
        value = prem * lot_size
        total_premium_turnover += value
        if action == "SELL":
            sell_premium_value += value

    stt = sell_premium_value * 0.0005  # 0.05% on sell side

    rate_per_crore = 50.5 if exchange == "NFO" else 37.5
    turnover_cr = total_premium_turnover / 1_00_00_000
    transaction_charge = turnover_cr * rate_per_crore

    sebi = turnover_cr * 10.0

    stamp = total_premium_turnover * 0.00002  # 0.002%

    gst = (brokerage + transaction_charge + sebi) * 0.18

    total = brokerage + stt + transaction_charge + sebi + stamp + gst
    return round(total, 2)

def _lock_file_for(strategy: str) -> str:
    return os.path.join(os.path.dirname(__file__), f".bot_lock_{strategy}")

HEARTBEAT_FILE = os.path.join(os.path.dirname(__file__), ".bot_heartbeat.txt")

_last_known_ipv4 = ""
_last_known_ipv6 = ""
_last_ip_check_time: float = 0
_last_whitelist_ok: Optional[bool] = None

_auth_failed = False
_last_test_time: float = 0

def periodic_connection_test(kite: "KiteSession"):
    global _last_test_time
    now = time.time()
    if now - _last_test_time < 900:
        return
    _last_test_time = now
    try:
        instruments = kite.get_option_instruments()
        if not instruments:
            return
        tsym = instruments[0]["tradingsymbol"]
        market_open = is_market_open()
        if market_open:
            qty, price, otype = 9999, 0, "MARKET"
            variety = "regular"
        else:
            qty, price, otype = LOT_SIZE, 0.05, "LIMIT"
            variety = "amo"
        kite.kite.place_order(
            variety, exchange="NFO", tradingsymbol=tsym,
            transaction_type="BUY", quantity=qty, price=price,
            product="NRML", order_type=otype, validity="DAY",
        )
        if not market_open:
            print(f"  Test AMO BUY {tsym} x {qty} @ ₹{price} — visible in Zerodha terminal")
    except Exception as e:
        msg = str(e).lower()
        if "margin" in msg or "funds" in msg or "insufficient" in msg:
            pass
        elif "ip" in msg and ("not allowed" in msg or "whitelist" in msg):
            print(f"  ⚠ IP REJECTED during periodic test: {e}")
        else:
            pass

def reload_token_if_needed(kite: "KiteSession") -> bool:
    """Reload token from config if it changed (user ran --login in another terminal)."""
    global _auth_failed
    cfg = load_config()
    new_token = cfg.get("access_token", "")
    if new_token and new_token != kite.access_token:
        try:
            kite.access_token = new_token
            kite.kite = KiteConnect(api_key=kite.api_key, access_token=new_token, timeout=30)
            kite.kite.profile()
            _auth_failed = False
            print("  ✓ Token renewed from config. Resuming.")
            return True
        except Exception:
            pass
    return False

def get_local_ipv6() -> str:
    """Get device's actual IPv6 from network interface."""
    try:
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("IPv6 Address") and ":" in line:
                    ip = line.split(":")[-1].strip()
                    if not ip.startswith("fe80") and not ip.startswith("::1"):
                        return ip
        else:
            out = subprocess.run(["ip", "-6", "addr", "show"], capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet6") and not line.startswith("inet6 fe80") and "::1" not in line:
                    parts = line.split()
                    if parts:
                        ip = parts[1].split("/")[0]
                        return ip
    except Exception:
        pass
    return ""

def heartbeat():
    global _last_known_ipv4, _last_known_ipv6, _auth_failed
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ip4 = get_public_ip_v4()
    ip6_local = get_local_ipv6()
    changed = []
    if ip4 and ip4 != _last_known_ipv4:
        if _last_known_ipv4:
            changed.append(f"IPv4: {_last_known_ipv4} → {ip4}")
        _last_known_ipv4 = ip4
    if ip6_local and ip6_local != _last_known_ipv6:
        if _last_known_ipv6:
            changed.append(f"IPv6: {_last_known_ipv6} → {ip6_local}")
        _last_known_ipv6 = ip6_local
    if len(changed) == 2:
        msg = f"Both IPs changed:\n{changed[0]}\n{changed[1]}\nUpdate Zerodha whitelist."
        print(f"  ⚠ {msg}")
    ips = "/".join(filter(None, [ip6_local, ip4]))
    now_str += f"  {ips}" if ips else ""
    if _auth_failed:
        now_str += "  [TOKEN EXPIRED - run --login]"
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(now_str)

def _is_pid_running(pid: int) -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        else:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    except Exception:
        return False

def acquire_lock(lock_file: str) -> bool:
    crashed = False
    if os.path.exists(lock_file):
        with open(lock_file) as f:
            pid = f.read().strip()
        try:
            pid_int = int(pid) if pid else None
        except (ValueError, TypeError):
            pid_int = None
        if pid_int and _is_pid_running(pid_int):
            print(f"Another instance (PID {pid_int}) already running. Exiting.")
            return False
        crashed = True
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.remove(lock_file) if os.path.exists(lock_file) else None)
    if crashed:
        print("Iron Condor Bot crashed and will restart.\nCheck .bot_heartbeat.txt for last activity.")
    return True

def init_trade_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG, "w", newline="") as f:
            csv.writer(f).writerow(TRADE_LOG_FIELDS)

def append_trade_log(row: dict):
    try:
        with open(TRADE_LOG, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS).writerow(row)
    except Exception as e:
        print(f"  Trade log write error: {e}")

# ---------------------------------------------------------------------------
# Strategy params (NIFTY only)
# ---------------------------------------------------------------------------

SYMBOL = "NIFTY"
LOT_SIZE = 65

SELL_PREMIUM_MIN = 14.0            # Short strike premium range: ₹14–25
SELL_PREMIUM_MAX = 25.0
BUY_PREMIUM_MIN = 4.0              # Long strike premium range: ₹4–6
BUY_PREMIUM_MAX = 6.0
ZERO_DTE_SELL_TARGET = 2.0         # 0DTE: exit when short premium ≤ ₹2
PROFIT_TARGET_RS = 1000.0        # Book profit at ₹1000
SL_MULTIPLIER = 1.0              # SL when loss = net_credit × 1

NFO_NAME = "NIFTY"               # name column in NFO instrument CSV
NSE_SYMBOL = "NIFTY 50"          # symbol for NSE cash segment
MONTH_ABBR = ["", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# SMA Crossover params
SMA_PERIOD = 60
SMA_TIMEFRAME = "3minute"
SENSEX_NSE_SYMBOL = "SENSEX"     # SENSEX index symbol
SENSEX_EXCHANGE = "BFO"          # BSE F&O exchange for SENSEX options
SENSEX_NAME = "SENSEX"           # name column in BFO instrument CSV
SENSEX_STRIKE_GAP = 100          # SENSEX has 100-point strike intervals

# ---------------------------------------------------------------------------
# Credit Spread params (trend-following with ADX + 200 EMA)
# ---------------------------------------------------------------------------

CS_SELL_PREMIUM_MIN = 25.0
CS_SELL_PREMIUM_MAX = 30.0
CS_BUY_PREMIUM_MIN = 5.0
CS_BUY_PREMIUM_MAX = 10.0
CS_PROFIT_TARGET_RS = 1000.0
CS_SL_MULTIPLIER = 1.0
CS_ZERO_DTE_SELL_TARGET = 2.0
CS_ADX_PERIOD = 14
CS_EMA_PERIOD = 200
CS_TIMEFRAME = "15minute"
CS_ADX_MIN = 25

# ---------------------------------------------------------------------------
# Kite API wrapper
# ---------------------------------------------------------------------------

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE

class KiteSession:
    def __init__(self, static_id: str = ""):
        cfg = load_config()
        self.api_key: str = cfg.get("api_key", "")
        self.api_secret: str = cfg.get("api_secret", "")
        self.access_token: str = cfg.get("access_token", "")
        self.static_id: str = static_id
        self.kite: Optional[KiteConnect] = None
        self._instruments: Optional[list[dict]] = None
        self._instruments_fetched_at: Optional[datetime] = None

    def is_authenticated(self) -> bool:
        return bool(self.api_key and self.access_token)

    def login_step1(self) -> str:
        if not self.api_key:
            print("Enter your API Key (from developers.kite.trade): ", end="")
            self.api_key = input().strip()
            save_config({**load_config(), "api_key": self.api_key})
        print("Enter your API Secret: ", end="")
        self.api_secret = input().strip()
        save_config({**load_config(), "api_secret": self.api_secret})

        kite = KiteConnect(api_key=self.api_key)
        url = kite.login_url()
        print(f"\n1. Open URL in browser:\n{url}")
        print("2. Login & authorize.")
        print("3. Paste the full redirect URL here: ", end="")
        raw = input().strip()
        # Extract request_token from URL if full URL was pasted
        if "request_token=" in raw:
            raw = raw.split("request_token=")[1].split("&")[0]
        return raw

    def login_step2(self, request_token: str):
        kite = KiteConnect(api_key=self.api_key)
        data = kite.generate_session(request_token, api_secret=self.api_secret)
        self.access_token = data["access_token"]
        self.kite = KiteConnect(api_key=self.api_key, access_token=self.access_token,
                                timeout=30)
        save_config({"api_key": self.api_key, "api_secret": self.api_secret,
                      "access_token": self.access_token})
        print("✓ Login successful.")

    def connect(self):
        if not self.is_authenticated():
            raise RuntimeError("Not authenticated. Run --login first.")
        self.kite = KiteConnect(api_key=self.api_key, access_token=self.access_token,
                                timeout=30)

    def ensure_auth(self):
        """Check token is valid; if not, reload from config. Returns True if OK."""
        global _auth_failed
        try:
            self.kite.profile()
            if _auth_failed:
                print("  ✓ Token renewed. Resuming.")
            _auth_failed = False
            return True
        except Exception:
            _auth_failed = True
            if reload_token_if_needed(self):
                return True
            print("  ✗ Token expired. Run: python iron_condor_algo.py --login")
            return False

    def _fetch_instruments(self):
        now = datetime.now()
        if self._instruments and self._instruments_fetched_at:
            if (now - self._instruments_fetched_at).seconds < 3600:
                return
        print("  Downloading instrument list ...")
        resp = requests.get(
            "https://api.kite.trade/instruments",
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {self.api_key}:{self.access_token}",
            },
            timeout=60,
        )
        self._instruments = list(csv.DictReader(io.StringIO(resp.text)))
        self._instruments_fetched_at = now

    def get_option_instruments(self, expiry_date: str = "") -> list[dict]:
        self._fetch_instruments()
        name = NFO_NAME
        return [
            r for r in self._instruments
            if r["exchange"] == "NFO"
            and r["name"] == name
            and r["instrument_type"] in ("CE", "PE")
            and (expiry_date == "" or r["expiry"] == expiry_date)
        ]

    def get_nse_spot(self) -> float:
        sym = f"NSE:{NSE_SYMBOL}"
        return self.kite.ltp(sym)[sym]["last_price"]

    def get_balance(self) -> float:
        """Return available cash balance (equity segment)."""
        try:
            margins = self.kite.margins(segment="equity")
            return float(margins["available"]["cash"])
        except Exception as e:
            print(f"  Could not fetch balance: {e}")
            return 0.0

    def get_quotes(self, tradingsymbols: list[str]) -> dict:
        q = {}
        for i in range(0, len(tradingsymbols), 500):
            keys = [f"NFO:{s}" for s in tradingsymbols[i:i+500]]
            q.update(self.kite.quote(keys))
        return q

    def place_limit(self, tsym: str, ttype: str, qty: int, price: float, exchange: str = "NFO") -> str:
        variety = "amo" if not is_market_open() else "regular"
        return self.kite.place_order(
            variety,
            exchange=exchange, tradingsymbol=tsym, transaction_type=ttype,
            quantity=qty, price=price, product="NRML", order_type="LIMIT",
            validity="DAY", tag=self.static_id,
        )

    def place_market(self, tsym: str, ttype: str, qty: int,
                     price: Optional[float] = None, exchange: str = "NFO") -> str:
        variety = "amo" if not is_market_open() else "regular"
        return self.kite.place_order(
            variety,
            exchange=exchange, tradingsymbol=tsym, transaction_type=ttype,
            quantity=qty, price=0, product="NRML", order_type="MARKET",
            validity="DAY", tag=self.static_id, market_protection=5,
        )

# ---------------------------------------------------------------------------
# Strike selection by premium
# ---------------------------------------------------------------------------

def nearest_expiry_today(instruments: list[dict]) -> Optional[str]:
    """Return today's expiry if it exists, else nearest future expiry."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_str = today
    seen = set()
    best = None
    best_diff = 999

    for row in instruments:
        exp = row["expiry"]
        if exp in seen:
            continue
        seen.add(exp)
        if exp == today_str:
            return exp  # 0DTE available
        exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        diff = (exp_date - datetime.now().date()).days
        if 0 <= diff < best_diff:
            best_diff = diff
            best = exp
    return best


def get_premium(quotes: dict, tsym: str) -> Optional[float]:
    key = f"NFO:{tsym}"
    if key not in quotes:
        return None
    return float(quotes[key]["last_price"])


def find_strikes(quotes: dict, calls: list, puts: list,
                 sell_min: float, sell_max: float,
                 buy_min: float, buy_max: float) -> Optional[dict]:
    """
    Among all quoted strikes, pick:
      - Short CE/PE: premium in [sell_min, sell_max], closest to midpoint
      - Long CE/PE:  premium in [buy_min, buy_max], further OTM than short
    """
    def best_in_range(rows, min_p, max_p, must_be_above=None, must_be_below=None):
        best = None
        best_diff = 999
        mid = (min_p + max_p) / 2
        for r in rows:
            strike = float(r["strike"])
            if must_be_above is not None and strike <= must_be_above:
                continue
            if must_be_below is not None and strike >= must_be_below:
                continue
            prem = get_premium(quotes, r["tradingsymbol"])
            if prem is None or prem < min_p or prem > max_p:
                continue
            diff = abs(prem - mid)
            if diff < best_diff:
                best_diff = diff
                best = (r, prem)
        return best

    short_call = best_in_range(calls, sell_min, sell_max)
    short_put = best_in_range(puts, sell_min, sell_max)
    if not short_call or not short_put:
        return None

    sc_row, sc_prem = short_call
    sp_row, sp_prem = short_put
    sc_strike = float(sc_row["strike"])
    sp_strike = float(sp_row["strike"])

    long_call = best_in_range(calls, buy_min, buy_max, must_be_above=sc_strike)
    long_put = best_in_range(puts, buy_min, buy_max, must_be_below=sp_strike)
    if not long_call or not long_put:
        return None

    lc_row, lc_prem = long_call
    lp_row, lp_prem = long_put

    return {
        "short_call": (sc_row, sc_prem),
        "short_put": (sp_row, sp_prem),
        "long_call": (lc_row, lc_prem),
        "long_put": (lp_row, lp_prem),
    }


# ---------------------------------------------------------------------------
# ADX / EMA helpers for trend detection
# ---------------------------------------------------------------------------

def calc_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * multiplier + ema
    return ema


def calc_adx(candles: list[dict], period: int) -> dict | None:
    if len(candles) < period + 2:
        return None
    tr_values, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr_values.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - candles[i-1]["high"]
        dn = candles[i-1]["low"] - l
        plus_dm.append(up if up > dn and up > 0 else 0)
        minus_dm.append(dn if dn > up and dn > 0 else 0)
    if len(tr_values) < period:
        return None
    s = slice(-period, None)
    tr_sum = sum(tr_values[s])
    if tr_sum == 0:
        return {"adx": 0, "plus_di": 0, "minus_di": 0}
    plus_di = 100 * sum(plus_dm[s]) / tr_sum
    minus_di = 100 * sum(minus_dm[s]) / tr_sum
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    return {"adx": dx, "plus_di": plus_di, "minus_di": minus_di}


# ---------------------------------------------------------------------------
# Iron Condor data types
# ---------------------------------------------------------------------------

@dataclass
class IronCondorLeg:
    tradingsymbol: str
    strike: float
    option_type: str
    action: str
    premium: float

@dataclass
class IronCondor:
    spot: float
    expiry: str
    legs: list[IronCondorLeg]
    net_credit: float
    width: float
    lower_breakeven: float
    upper_breakeven: float

    def max_profit(self) -> float:
        return self.net_credit * LOT_SIZE

    def max_loss(self) -> float:
        return (self.width - self.net_credit) * LOT_SIZE

    def to_dict(self) -> dict:
        return {
            "spot": round(self.spot, 2),
            "expiry": self.expiry,
            "legs": [asdict(l) for l in self.legs],
            "net_credit": round(self.net_credit, 2),
            "max_profit": round(self.max_profit(), 2),
            "max_loss": round(self.max_loss(), 2),
            "width": self.width,
            "lower_breakeven": round(self.lower_breakeven, 2),
            "upper_breakeven": round(self.upper_breakeven, 2),
        }


# ---------------------------------------------------------------------------
# Credit Spread data types
# ---------------------------------------------------------------------------

@dataclass
class CreditSpreadLeg:
    tradingsymbol: str
    strike: float
    option_type: str
    action: str
    premium: float


@dataclass
class CreditSpread:
    spot: float
    expiry: str
    spread_type: str
    legs: list[CreditSpreadLeg]
    net_credit: float
    width: float
    lower_breakeven: float
    upper_breakeven: float
    trend: str

    def max_profit(self) -> float:
        return self.net_credit * LOT_SIZE

    def max_loss(self) -> float:
        return (self.width - self.net_credit) * LOT_SIZE

    def to_dict(self) -> dict:
        return {
            "spot": round(self.spot, 2),
            "expiry": self.expiry,
            "spread_type": self.spread_type,
            "legs": [asdict(l) for l in self.legs],
            "net_credit": round(self.net_credit, 2),
            "max_profit": round(self.max_profit(), 2),
            "max_loss": round(self.max_loss(), 2),
            "width": self.width,
            "lower_breakeven": round(self.lower_breakeven, 2),
            "upper_breakeven": round(self.upper_breakeven, 2),
            "trend": self.trend,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class IronCondorManager:
    def __init__(self, kite: KiteSession):
        self.kite = kite
        self.position: Optional[IronCondor] = None
        self.entry_credit: float = 0.0
        self.entry_time: str = ""
        self.entry_spot: float = 0.0
        self._order_ids: dict[str, str] = {}

    def _get_chain(self, expiry: str):
        """Return (calls_list, puts_list, all_tsyms) for expiry."""
        instruments = self.kite.get_option_instruments(expiry)
        calls = sorted(
            [r for r in instruments if r["instrument_type"] == "CE"],
            key=lambda x: float(x["strike"])
        )
        puts = sorted(
            [r for r in instruments if r["instrument_type"] == "PE"],
            key=lambda x: float(x["strike"])
        )
        all_tsyms = [r["tradingsymbol"] for r in instruments]
        return calls, puts, all_tsyms

    def scan(self) -> Optional[IronCondor]:
        """Build iron condor from live data using premium ranges."""
        print(f"Scanning {SYMBOL} (sell ₹{SELL_PREMIUM_MIN}-{SELL_PREMIUM_MAX}, buy ₹{BUY_PREMIUM_MIN}-{BUY_PREMIUM_MAX}) ...")

        spot = self.kite.get_nse_spot()
        print(f"  Spot: {spot:.2f}")

        all_instruments = self.kite.get_option_instruments()
        expiry = nearest_expiry_today(all_instruments)
        if not expiry:
            print("  ✗ No expiry found")
            return None

        calls, puts, tsyms = self._get_chain(expiry)
        print(f"  Expiry: {expiry}  ({len(tsyms)} strikes)")

        quotes = self.kite.get_quotes(tsyms)
        selected = find_strikes(quotes, calls, puts,
                                SELL_PREMIUM_MIN, SELL_PREMIUM_MAX,
                                BUY_PREMIUM_MIN, BUY_PREMIUM_MAX)
        if not selected:
            print("  ✗ No strikes match premium targets")
            return None

        sc_row, sc_prem = selected["short_call"]
        sp_row, sp_prem = selected["short_put"]
        lc_row, lc_prem = selected["long_call"]
        lp_row, lp_prem = selected["long_put"]

        legs = [
            IronCondorLeg(sp_row["tradingsymbol"], float(sp_row["strike"]), "PE", "SELL", sp_prem),
            IronCondorLeg(lp_row["tradingsymbol"], float(lp_row["strike"]), "PE", "BUY", lp_prem),
            IronCondorLeg(sc_row["tradingsymbol"], float(sc_row["strike"]), "CE", "SELL", sc_prem),
            IronCondorLeg(lc_row["tradingsymbol"], float(lc_row["strike"]), "CE", "BUY", lc_prem),
        ]

        net_credit = (sp_prem + sc_prem) - (lp_prem + lc_prem)
        width = float(lc_row["strike"]) - float(sc_row["strike"])

        return IronCondor(
            spot=spot, expiry=expiry, legs=legs,
            net_credit=net_credit, width=width,
            lower_breakeven=float(sp_row["strike"]) - net_credit,
            upper_breakeven=float(sc_row["strike"]) + net_credit,
        )


    def _place_legs(self, legs: list[IronCondorLeg], phase_name: str,
                    order_ids: dict) -> bool:
        """Place orders for a list of legs. Returns True if all placed."""
        for leg in legs:
            qty = LOT_SIZE
            if is_market_open():
                print(f"  {leg.action} {leg.tradingsymbol} x {qty} (MARKET)")
                try:
                    oid = self.kite.place_market(leg.tradingsymbol, leg.action, qty, leg.premium)
                    order_ids[leg.tradingsymbol] = oid
                    print(f"    ✓ {oid}")
                except Exception as e:
                    print(f"    ✗ {e}")
                    return False
            else:
                print(f"  {leg.action} {leg.tradingsymbol} @ ₹{leg.premium:.2f} x {qty} (AMO LIMIT)")
                try:
                    oid = self.kite.place_limit(leg.tradingsymbol, leg.action, qty, leg.premium)
                    order_ids[leg.tradingsymbol] = oid
                    print(f"    ✓ {oid}")
                except Exception as e:
                    print(f"    ✗ {e}")
                    return False
        return True

    def _check_fills(self, legs: list[IronCondorLeg]) -> bool:
        """Check if all given legs are filled."""
        expected = {leg.tradingsymbol for leg in legs}
        try:
            for o in self.kite.kite.orders():
                tsym = o.get("tradingsymbol", "")
                if tsym in expected and o.get("status") == "COMPLETE" and o.get("filled_quantity", 0) >= LOT_SIZE:
                    expected.discard(tsym)
        except Exception:
            return False
        return len(expected) == 0

    def _square_off_legs(self, legs: list[IronCondorLeg], order_ids: dict[str, str] = {}):
        """Square off filled legs using market orders."""
        filled = set()
        try:
            for o in self.kite.kite.orders():
                tsym = o.get("tradingsymbol", "")
                if tsym in {l.tradingsymbol for l in legs} and o.get("status") == "COMPLETE" and o.get("filled_quantity", 0) >= LOT_SIZE:
                    filled.add(tsym)
        except Exception:
            pass
        for leg in legs:
            if leg.tradingsymbol in filled:
                reverse = "BUY" if leg.action == "SELL" else "SELL"
                print(f"  Squaring off {reverse} {leg.tradingsymbol} x {LOT_SIZE}")
                try:
                    self.kite.place_market(leg.tradingsymbol, reverse, LOT_SIZE)
                except Exception as e:
                    print(f"    ✗ {e}")
            else:
                oid = order_ids.get(leg.tradingsymbol)
                if oid:
                    try:
                        self.kite.kite.cancel_order("regular", oid)
                    except Exception:
                        try:
                            self.kite.kite.cancel_order("amo", oid)
                        except Exception:
                            pass

    def enter(self, ic: IronCondor) -> bool:
        """Place 4-leg orders — BUY (protection) first, then SELL (premium)."""
        is_0dte = ic.expiry == datetime.now().strftime("%Y-%m-%d")
        target_str = "short premium ≤ ₹2" if is_0dte else f"₹{PROFIT_TARGET_RS:.0f} profit"

        print(f"\nENTER — Credit ₹{ic.net_credit:.2f}  Target: {target_str}")

        order_ids: dict[str, str] = {}
        buy_legs = [l for l in ic.legs if l.action == "BUY"]
        sell_legs = [l for l in ic.legs if l.action == "SELL"]

        if not self._place_legs(buy_legs, "BUY", order_ids):
            for tsym, oid in order_ids.items():
                try:
                    self.kite.kite.cancel_order("regular", oid)
                except Exception:
                    try:
                        self.kite.kite.cancel_order("amo", oid)
                    except Exception:
                        pass
            return False

        if is_market_open():
            time.sleep(3)
            if not self._check_fills(buy_legs):
                self._square_off_legs(buy_legs, order_ids)
                return False

        if not self._place_legs(sell_legs, "SELL", order_ids):
            self._square_off_legs(buy_legs, order_ids)
            return False

        self.position = ic
        self.entry_credit = ic.net_credit
        self.entry_time = datetime.now().isoformat()
        self.entry_spot = ic.spot
        self._order_ids = order_ids
        # Notify: strategy + legs punched
        legs_dict = [asdict(l) for l in ic.legs]
        telegram_logger.strategy_entry_alert("IRON CONDOR", legs_dict)
        return True

    def verify_fills(self) -> bool:
        """Check all 4 legs filled; if partial, square off and abort."""
        if not self.position:
            return False
        ic = self.position
        expected = {leg.tradingsymbol for leg in ic.legs}
        try:
            orders = self.kite.kite.orders()
        except Exception as e:
            print(f"  Could not fetch orders: {e}")
            return False
        filled = set()
        for o in orders:
            tsym = o.get("tradingsymbol", "")
            status = o.get("status", "")
            filled_qty = o.get("filled_quantity", 0)
            if tsym in expected and status == "COMPLETE" and filled_qty >= LOT_SIZE:
                filled.add(tsym)
        missing = expected - filled
        if not missing:
            telegram_logger.trade_alert(SYMBOL, "ENTER", ic.net_credit, LOT_SIZE)
            return True
        print("Partial fill — squaring off")
        for leg in ic.legs:
            if leg.tradingsymbol in filled:
                reverse = "BUY" if leg.action == "SELL" else "SELL"
                print(f"  {reverse} {leg.tradingsymbol} x {LOT_SIZE} (MARKET)")
                try:
                    self.kite.place_market(leg.tradingsymbol, reverse, LOT_SIZE)
                except Exception as e:
                    print(f"    ✗ {e}")
            else:
                oid = self._order_ids.get(leg.tradingsymbol)
                if oid:
                    try:
                        self.kite.kite.cancel_order("regular", oid)
                        print(f"  Cancelled unfilled {leg.tradingsymbol}")
                    except Exception:
                        try:
                            self.kite.kite.cancel_order("amo", oid)
                            print(f"  Cancelled unfilled AMO {leg.tradingsymbol}")
                        except Exception:
                            pass
        self.position = None
        self.entry_credit = 0.0
        c = load_config()
        c.pop("position", None)
        save_config(c)
        print("Partial position squared off. Exiting.")
        return False

    def monitor(self, exit_now: bool = False) -> str:
        if not self.position:
            return "NO_POSITION"

        ic = self.position
        quotes = self.kite.get_quotes([leg.tradingsymbol for leg in ic.legs])

        current = 0.0
        short_prems = []
        for leg in ic.legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is not None:
                current += prem if leg.action == "SELL" else -prem
                if leg.action == "SELL":
                    short_prems.append(prem)

        pnl_per = self.entry_credit - current
        pnl = pnl_per * LOT_SIZE
        spot = self.kite.get_nse_spot()
        avg_short_prem = sum(short_prems) / len(short_prems) if short_prems else 0
        now = datetime.now()
        is_0dte = ic.expiry == now.strftime("%Y-%m-%d")

        pnl_str = f"P&L ₹{pnl:+.0f}"

        if exit_now:
            return "EXIT_REQUESTED"

        # Time exit: close at 3:15 PM regardless
        if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
            return "EXIT_TIME"

        # Normal day: profit target
        if not is_0dte and pnl >= PROFIT_TARGET_RS:
            return "EXIT_PROFIT"

        # 0DTE: exit when short premium ≤ ₹2
        if is_0dte and avg_short_prem <= ZERO_DTE_SELL_TARGET:
            return "EXIT_PROFIT"

        # Stop loss (same for both)
        if pnl <= -(self.entry_credit * LOT_SIZE * SL_MULTIPLIER):
            return "EXIT_LOSS"
        return "HOLD"

    def exit(self, reason: str):
        if not self.position:
            return
        print(f"\nEXIT ({reason})")
        exit_spot = self.kite.get_nse_spot()
        quotes = self.kite.get_quotes([leg.tradingsymbol for leg in self.position.legs])
        current = 0.0
        for leg in self.position.legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is not None:
                current += prem if leg.action == "SELL" else -prem
            reverse = "BUY" if leg.action == "SELL" else "SELL"
            try:
                oid = self.kite.place_market(leg.tradingsymbol, reverse, LOT_SIZE)
            except Exception as e:
                print(f"  ✗ {leg.tradingsymbol}: {e}")
        pnl = (self.entry_credit - current) * LOT_SIZE
        charges = calc_charges([asdict(l) for l in self.position.legs], LOT_SIZE)
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": "IC",
            "expiry": self.position.expiry,
            "entry_time": self.entry_time,
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(self.entry_spot, 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(self.entry_credit, 2),
            "exit_value": round(current, 2),
            "pnl": round(pnl, 2),
            "charges": charges,
            "max_profit_target": PROFIT_TARGET_RS,
            "stop_loss": round(self.entry_credit * LOT_SIZE, 2),
            "exit_reason": reason,
        })
        telegram_logger.pnl_alert(pnl, trade_id="IC_" + self.position.expiry)
        telegram_logger.trade_alert(SYMBOL, reason, abs(current), LOT_SIZE)
        print("Position closed.")
        self.position = None
        self.entry_credit = 0.0

    def resume_from_positions(self) -> Optional[IronCondor]:
        """Scan live Zerodha positions and build IronCondor if 4 NIFTY legs exist."""
        try:
            positions = self.kite.kite.positions()["day"]
        except Exception as e:
            print(f"  Could not fetch positions: {e}")
            return None
        nifty_opts = [
            p for p in positions
            if p.get("exchange") == "NFO" and "NIFTY" in p.get("tradingsymbol", "")
        ]
        if len(nifty_opts) != 4:
            print(f"  Expected 4 NIFTY option legs, found {len(nifty_opts)}")
            return None
        sell_qty = max(p.get("quantity", 0) for p in nifty_opts)
        legs = []
        for p in nifty_opts:
            tsym = p["tradingsymbol"]
            qty = p.get("quantity", 0)
            strike = float(p.get("strike_price", 0))
            otype = p.get("instrument_type", "")
            action = "SELL" if abs(qty) == sell_qty else "BUY"
            legs.append(IronCondorLeg(tsym, strike, otype, action, 0))
        expiries = {p.get("expiry_date", "")[:10] for p in nifty_opts}
        expiry = list(expiries)[0]
        # Estimate net credit from current premiums
        quotes = self.kite.get_quotes([l.tradingsymbol for l in legs])
        net = 0.0
        for leg in legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is None:
                return None
            leg.premium = prem
            net += prem if leg.action == "SELL" else -prem
        spot = self.kite.get_nse_spot()
        return IronCondor(spot=spot, expiry=expiry, legs=legs,
                          net_credit=net, width=0,
                          lower_breakeven=0, upper_breakeven=0)


# ---------------------------------------------------------------------------
# Credit Spread Manager (trend-following: ADX + 200 EMA)
# ---------------------------------------------------------------------------

class CreditSpreadManager:
    def __init__(self, kite: KiteSession):
        self.kite = kite
        self.position: CreditSpread | None = None
        self.entry_credit: float = 0.0
        self.entry_time: str = ""
        self.entry_spot: float = 0.0
        self._order_ids: dict[str, str] = {}

    def _get_nifty_token(self) -> int | None:
        for exchange in ("NSE", "BSE"):
            try:
                for row in self.kite.kite.instruments(exchange):
                    tsym = row.get("tradingsymbol", "")
                    if tsym in ("NIFTY 50", "NIFTY"):
                        return int(row["instrument_token"])
                    if "NIFTY" in tsym and row.get("instrument_type", "") == "":
                        return int(row["instrument_token"])
            except Exception:
                continue
        return None

    def _get_candles(self, token: int, lookback_hours: int = 300) -> list:
        to_dt = datetime.now()
        try:
            return self.kite.kite.historical_data(token, to_dt - timedelta(hours=lookback_hours), to_dt, CS_TIMEFRAME)
        except Exception as e:
            print(f"  Historical data error: {e}")
            return []

    def _detect_trend(self) -> dict | None:
        token = self._get_nifty_token()
        if not token:
            print("  Could not find NIFTY index token")
            return None
        candles = self._get_candles(token)
        if len(candles) < max(CS_ADX_PERIOD + 2, CS_EMA_PERIOD + 1):
            print(f"  Insufficient candle data ({len(candles)})")
            return None
        closes = [c["close"] for c in candles]
        price = closes[-1]
        ema = calc_ema(closes, CS_EMA_PERIOD)
        adx = calc_adx(candles, CS_ADX_PERIOD)
        if ema is None or adx is None:
            return None
        above = price > ema
        return {
            "adx": round(adx["adx"], 1), "plus_di": round(adx["plus_di"], 1),
            "minus_di": round(adx["minus_di"], 1), "ema_200": round(ema, 2),
            "price": price, "above_ema": above,
            "trending": adx["adx"] >= CS_ADX_MIN,
            "bullish": above and adx["plus_di"] > adx["minus_di"],
            "bearish": not above and adx["minus_di"] > adx["plus_di"],
            "ranging": adx["adx"] < 20,
        }

    def _get_chain(self, expiry: str):
        instruments = self.kite.get_option_instruments(expiry)
        calls = sorted([r for r in instruments if r["instrument_type"] == "CE"], key=lambda x: float(x["strike"]))
        puts = sorted([r for r in instruments if r["instrument_type"] == "PE"], key=lambda x: float(x["strike"]))
        return calls, puts, [r["tradingsymbol"] for r in instruments]

    def scan(self) -> CreditSpread | None:
        trend = self._detect_trend()
        if not trend:
            return None
        dir_str = "BULLISH" if trend["bullish"] else "BEARISH" if trend["bearish"] else "RANGING"
        if trend["ranging"]:
            return None
        if not trend["trending"]:
            return None
        if not trend["bullish"] and not trend["bearish"]:
            return None
        spread_type = "BULL_PUT" if trend["bullish"] else "BEAR_CALL"
        spot = self.kite.get_nse_spot()
        expiry = nearest_expiry_today(self.kite.get_option_instruments())
        if not expiry:
            return None
        calls, puts, tsyms = self._get_chain(expiry)
        quotes = self.kite.get_quotes(tsyms)
        if spread_type == "BULL_PUT":
            sel = find_strikes(quotes, [], puts, CS_SELL_PREMIUM_MIN, CS_SELL_PREMIUM_MAX, CS_BUY_PREMIUM_MIN, CS_BUY_PREMIUM_MAX)
            if not sel:
                print("  No qualifying Bull Put strikes")
                return None
            sp, lp = sel["short_put"], sel["long_put"]
            legs = [CreditSpreadLeg(sp[0]["tradingsymbol"], float(sp[0]["strike"]), "PE", "SELL", sp[1]),
                    CreditSpreadLeg(lp[0]["tradingsymbol"], float(lp[0]["strike"]), "PE", "BUY", lp[1])]
            net = sp[1] - lp[1]
            w = float(sp[0]["strike"]) - float(lp[0]["strike"])
            lower_be = float(sp[0]["strike"]) - net
            upper_be = float("inf")
        else:
            sel = find_strikes(quotes, calls, [], CS_SELL_PREMIUM_MIN, CS_SELL_PREMIUM_MAX, CS_BUY_PREMIUM_MIN, CS_BUY_PREMIUM_MAX)
            if not sel:
                print("  No qualifying Bear Call strikes")
                return None
            sc, lc = sel["short_call"], sel["long_call"]
            legs = [CreditSpreadLeg(sc[0]["tradingsymbol"], float(sc[0]["strike"]), "CE", "SELL", sc[1]),
                    CreditSpreadLeg(lc[0]["tradingsymbol"], float(lc[0]["strike"]), "CE", "BUY", lc[1])]
            net = sc[1] - lc[1]
            w = float(lc[0]["strike"]) - float(sc[0]["strike"])
            lower_be = float("-inf")
            upper_be = float(sc[0]["strike"]) + net
        return CreditSpread(spot=spot, expiry=expiry, spread_type=spread_type, legs=legs,
                            net_credit=net, width=w, lower_breakeven=lower_be, upper_breakeven=upper_be,
                            trend=f"ADX:{trend['adx']} EMA200:{trend['ema_200']:.0f}")

    def enter(self, cs: CreditSpread) -> bool:
        is_0dte = cs.expiry == datetime.now().strftime("%Y-%m-%d")
        target_str = "short prem ≤ ₹2" if is_0dte else f"₹{CS_PROFIT_TARGET_RS} profit"
        print(f"\nENTER {cs.spread_type} — Credit ₹{cs.net_credit:.2f}  Target: {target_str}")
        order_ids: dict = {}
        buy_legs = [l for l in cs.legs if l.action == "BUY"]
        sell_legs = [l for l in cs.legs if l.action == "SELL"]
        for leg in buy_legs:
            try:
                oid = self.kite.place_market(leg.tradingsymbol, leg.action, LOT_SIZE, leg.premium)
                order_ids[leg.tradingsymbol] = oid
            except Exception as e:
                print(f"  ✗ BUY fail: {e}")
                return False
        if is_market_open():
            time.sleep(3)
            expected = {l.tradingsymbol for l in buy_legs}
            try:
                for o in self.kite.kite.orders():
                    if o["tradingsymbol"] in expected and o["status"] == "COMPLETE" and o["filled_quantity"] >= LOT_SIZE:
                        expected.discard(o["tradingsymbol"])
            except Exception:
                pass
            if expected:
                print("  Protection not filled. Aborting.")
                self._square_off(buy_legs, order_ids)
                return False

        print("Phase 2 — Premium (SELL)...")
        for leg in sell_legs:
            try:
                oid = self.kite.place_market(leg.tradingsymbol, leg.action, LOT_SIZE, leg.premium)
                order_ids[leg.tradingsymbol] = oid
            except Exception as e:
                print(f"  ✗ SELL fail: {e}")
                self._square_off(buy_legs, order_ids)
                return False
        self.position = cs
        self.entry_credit = cs.net_credit
        self.entry_time = datetime.now().isoformat()
        self.entry_spot = cs.spot
        self._order_ids = order_ids
        legs_dict = [asdict(l) for l in cs.legs]
        telegram_logger.strategy_entry_alert(f"CS {cs.spread_type}", legs_dict)
        return True

    def _square_off(self, legs: list[CreditSpreadLeg], order_ids: dict = {}):
        filled = set()
        try:
            for o in self.kite.kite.orders():
                if o["tradingsymbol"] in {l.tradingsymbol for l in legs} and o["status"] == "COMPLETE" and o["filled_quantity"] >= LOT_SIZE:
                    filled.add(o["tradingsymbol"])
        except Exception:
            pass
        for leg in legs:
            if leg.tradingsymbol in filled:
                rev = "BUY" if leg.action == "SELL" else "SELL"
                try:
                    self.kite.place_market(leg.tradingsymbol, rev, LOT_SIZE)
                except Exception:
                    pass
            elif leg.tradingsymbol in order_ids:
                try:
                    self.kite.kite.cancel_order("regular", order_ids[leg.tradingsymbol])
                except Exception:
                    try:
                        self.kite.kite.cancel_order("amo", order_ids[leg.tradingsymbol])
                    except Exception:
                        pass

    def monitor(self, exit_now: bool = False) -> str:
        if not self.position:
            return "NO_POSITION"
        cs = self.position
        quotes = self.kite.get_quotes([l.tradingsymbol for l in cs.legs])
        current, short_prems = 0.0, []
        for leg in cs.legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is not None:
                current += prem if leg.action == "SELL" else -prem
                if leg.action == "SELL":
                    short_prems.append(prem)
        pnl = (self.entry_credit - current) * LOT_SIZE
        avg_sp = sum(short_prems) / len(short_prems) if short_prems else 0
        spot = self.kite.get_nse_spot()
        is_0dte = cs.expiry == datetime.now().strftime("%Y-%m-%d")
        print(f"\n{'='*50}\nMONITOR {cs.spread_type} {SYMBOL}\n{'='*50}")
        print(f"Spot: {spot:.0f}  Credit: ₹{self.entry_credit:.1f}  Current: ₹{current:.1f}")
        print(f"Short Prem: ₹{avg_sp:.1f}  P&L: ₹{pnl:.0f}")
        if exit_now:
            return "EXIT_REQUESTED"
        if datetime.now().hour > 15 or (datetime.now().hour == 15 and datetime.now().minute >= 15):
            return "EXIT_TIME"
        if not is_0dte and pnl >= CS_PROFIT_TARGET_RS:
            return "EXIT_PROFIT"
        if is_0dte and avg_sp <= CS_ZERO_DTE_SELL_TARGET:
            return "EXIT_PROFIT"
        if pnl <= -(self.entry_credit * LOT_SIZE * CS_SL_MULTIPLIER):
            return "EXIT_LOSS"
        return "HOLD"

    def exit(self, reason: str):
        if not self.position:
            return
        print(f"\n{'='*50}\nEXIT ({reason})\n{'='*50}")
        exit_spot = self.kite.get_nse_spot()
        quotes = self.kite.get_quotes([l.tradingsymbol for l in self.position.legs])
        current = 0.0
        for leg in self.position.legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is not None:
                current += prem if leg.action == "SELL" else -prem
            rev = "BUY" if leg.action == "SELL" else "SELL"
            try:
                oid = self.kite.place_market(leg.tradingsymbol, rev, LOT_SIZE)
            except Exception as e:
                print(f"  ✗ {leg.tradingsymbol}: {e}")
        pnl = (self.entry_credit - current) * LOT_SIZE
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"CS_{self.position.spread_type}",
            "expiry": self.position.expiry,
            "entry_time": self.entry_time,
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(self.entry_spot, 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(self.entry_credit, 2),
            "exit_value": round(current, 2),
            "pnl": round(pnl, 2),
            "max_profit_target": CS_PROFIT_TARGET_RS,
            "stop_loss": round(self.entry_credit * LOT_SIZE, 2),
            "exit_reason": reason,
        })
        telegram_logger.pnl_alert(pnl, trade_id="CS_" + self.position.expiry)
        telegram_logger.trade_alert(SYMBOL, reason, abs(current), LOT_SIZE)
        print("Position closed.")
        self.position = None
        self.entry_credit = 0.0

    def resume_from_positions(self) -> CreditSpread | None:
        try:
            positions = self.kite.kite.positions()["day"]
        except Exception:
            return None
        nifty = [p for p in positions if p.get("exchange") == "NFO" and "NIFTY" in p.get("tradingsymbol", "")]
        if len(nifty) != 2:
            return None
        legs = []
        for p in nifty:
            tsym = p["tradingsymbol"]
            qty = p.get("quantity", 0)
            legs.append(CreditSpreadLeg(tsym, float(p.get("strike_price", 0)),
                                        p.get("instrument_type", ""),
                                        "SELL" if abs(qty) == LOT_SIZE else "BUY", 0))
        exp = list({p.get("expiry_date", "")[:10] for p in nifty})[0]
        quotes = self.kite.get_quotes([l.tradingsymbol for l in legs])
        net = 0.0
        for leg in legs:
            prem = get_premium(quotes, leg.tradingsymbol)
            if prem is None:
                return None
            leg.premium = prem
            net += prem if leg.action == "SELL" else -prem
        spread_type = "BULL_PUT" if any(l.option_type == "PE" for l in legs) else "BEAR_CALL"
        return CreditSpread(spot=self.kite.get_nse_spot(), expiry=exp, spread_type=spread_type,
                            legs=legs, net_credit=net, width=0, lower_breakeven=0, upper_breakeven=0, trend="resumed")


# ---------------------------------------------------------------------------
# SMA Crossover Strategy (SENSEX, 3min, SMA60)
# ---------------------------------------------------------------------------

class SmaCrossover:
    def __init__(self, kite: KiteSession, lots: int = 1):
        self.kite = kite
        self.lots = lots
        self.trades_today = 0
        self.session1_done = False
        self.session2_done = False
        self.trades = []  # list of trade dicts: {side, entry_ts, entry_price, sl, target_level, qty, tsym, strike, oid, entry_prem, entry_sl, expiry}

    def _get_index_token(self) -> Optional[int]:
        """Find SENSEX index instrument token for historical data."""
        for exchange in ("BSE", "NSE"):
            try:
                for row in self.kite.kite.instruments(exchange):
                    tsym = row.get("tradingsymbol", "")
                    exch = row.get("exchange", "")
                    if tsym == "SENSEX" and exch == exchange:
                        return int(row["instrument_token"])
                    if "SENSEX" in tsym and row.get("instrument_type", "") == "":
                        return int(row["instrument_token"])
            except Exception:
                continue
        return None

    def _get_sensex_spot(self) -> Optional[float]:
        for sym in ("BSE:SENSEX", f"NSE:{SENSEX_NSE_SYMBOL}"):
            try:
                return self.kite.kite.ltp(sym)[sym]["last_price"]
            except Exception:
                continue
        return None

    def _get_max_loss(self) -> float:
        """Estimate max loss for 1 lot SENSEX option (lot=15 or 20)."""
        # Default to 20 for safety, adjust if needed
        return 20000  # ~Rs 100 x 200pts

    def _get_3min_candles(self, token: int, lookback_hours: int = 48) -> list:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(hours=lookback_hours)
        try:
            return self.kite.kite.historical_data(token, from_dt, to_dt, "3minute")
        except Exception as e:
            print(f"  Hist data error: {e}")
            return []

    def _calc_sma(self, candles: list, period: int) -> Optional[float]:
        if len(candles) < period:
            return None
        closes = [c["close"] for c in candles[-period:]]
        return sum(closes) / period

    def _get_sensex_instruments(self, expiry: str) -> list[dict]:
        self.kite._fetch_instruments()
        return [
            r for r in self.kite._instruments
            if r["exchange"] == SENSEX_EXCHANGE
            and r["name"] == SENSEX_NAME
            and r["instrument_type"] in ("CE", "PE")
            and r["expiry"] == expiry
        ]

    def _get_nearest_expiry(self, instruments: list[dict]) -> Optional[str]:
        today = datetime.now().strftime("%Y-%m-%d")
        seen = set()
        best = None
        best_diff = 999
        for row in instruments:
            exp = row["expiry"]
            if exp in seen:
                continue
            seen.add(exp)
            if exp == today:
                return exp
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            diff = (exp_date - datetime.now().date()).days
            if 0 <= diff < best_diff:
                best_diff = diff
                best = exp
        return best

    def _get_atm_option(self, spot: float, is_ce: bool) -> Optional[dict]:
        atm = round(spot / SENSEX_STRIKE_GAP) * SENSEX_STRIKE_GAP
        self.kite._fetch_instruments()
        s_exps = set()
        for r in self.kite._instruments:
            if r["exchange"] == SENSEX_EXCHANGE and r["name"] == SENSEX_NAME:
                s_exps.add(r["expiry"])
        if not s_exps:
            print("  No SENSEX expiry found")
            return None
        # Find nearest expiry
        today = datetime.now().strftime("%Y-%m-%d")
        best_exp = min(s_exps, key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d") - datetime.now()).days) if e >= today else 999)
        opts = self._get_sensex_instruments(best_exp)
        otype = "CE" if is_ce else "PE"
        best = None
        best_diff = 999999
        for r in opts:
            if r["instrument_type"] == otype:
                d = abs(float(r["strike"]) - atm)
                if d < best_diff:
                    best_diff = d
                    qty = int(r.get("lot_size", 15))
                    best = {"tsym": r["tradingsymbol"], "strike": float(r["strike"]),
                            "qty": qty, "expiry": best_exp}
        return best

    def _is_trade_active(self, trade: dict = None) -> bool:
        if trade is not None:
            return self._trade_has_position(trade)
        return any(self._trade_has_position(t) for t in self.trades)

    def _trade_has_position(self, trade: dict) -> bool:
        try:
            positions = self.kite.kite.positions()["day"]
            for p in positions:
                if p.get("tradingsymbol") == trade["tsym"] and p.get("quantity", 0) != 0:
                    return True
        except Exception:
            pass
        return False

    def _in_session1(self, now: datetime) -> bool:
        return (now.hour == 9 and now.minute >= 30) or (now.hour == 10) or (now.hour == 11 and now.minute <= 30)

    def _in_session2(self, now: datetime) -> bool:
        return (now.hour == 13) or (now.hour == 14 and now.minute <= 30)

    def _manage_trades(self):
        spot = self._get_sensex_spot()
        if spot is None:
            return
        to_remove = []
        for trade in self.trades:
            side = trade["side"]
            entry_price = trade["entry_price"]
            sl = trade["sl"]
            target_level = trade["target_level"]
            points = abs(spot - entry_price)
            is_buy = (side == "CE")
            if (is_buy and spot <= sl) or (not is_buy and spot >= sl):
                print(f"  SL hit {trade['tsym']} @ {spot:.2f}, closing")
                self._exit_trade(trade)
                to_remove.append(trade)
            else:
                risk = abs(entry_price - trade["entry_sl"])
                rr = points / risk if risk > 0 else 0
                if rr >= target_level + 1:
                    new_sl = entry_price + (target_level + 1) * risk if is_buy else entry_price - (target_level + 1) * risk
                    if (is_buy and new_sl > sl) or (not is_buy and new_sl < sl):
                        trade["sl"] = new_sl
                        trade["target_level"] = target_level + 1
                        print(f"  Trail {trade['tsym']} SL to {new_sl:.2f} (RR {target_level+1}:1)")
        for t in to_remove:
            if t in self.trades:
                self.trades.remove(t)

    def _exit_trade(self, trade: dict):
        tsym = trade["tsym"]
        qty = trade["qty"]
        exit_spot = self._get_sensex_spot() or 0
        try:
            ltp = self.kite.kite.ltp(f"{SENSEX_EXCHANGE}:{tsym}")
            exit_prem = ltp.get(f"{SENSEX_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            exit_prem = 0
        try:
            self.kite.place_market(tsym, "BUY" if trade["side"] == "PE" else "SELL", qty, exchange=SENSEX_EXCHANGE)
            print(f"  Closed {tsym}")
        except Exception as e:
            print(f"  Close error: {e}")
        entry_prem = trade.get("entry_prem", 0)
        pnl = (entry_prem - exit_prem) * qty if trade["side"] == "CE" else (exit_prem - entry_prem) * qty
        charges = calc_charges([{"action": "BUY", "premium": entry_prem}], qty, exchange=SENSEX_EXCHANGE)
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"SMA_{trade['side']}",
            "expiry": trade.get("expiry", ""),
            "entry_time": trade.get("entry_ts", ""),
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(trade.get("entry_price", 0), 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(entry_prem, 2),
            "exit_value": round(exit_prem, 2),
            "pnl": round(pnl, 2),
            "charges": charges,
            "max_profit_target": 0,
            "stop_loss": 0,
            "exit_reason": "SL_HIT" if trade.get("sl") else "CLOSED",
        })
        if trade in self.trades:
            self.trades.remove(trade)

    def _wait_for_crossover(self, session_name: str, timeout_minutes: int = 120) -> bool:
        """Monitor for SMA crossover during session window. Returns True if trade entered."""
        token = self._get_index_token()
        if not token:
            print("  Could not find SENSEX index token")
            return False

        print(f"  [{session_name}] Watching crossover...")
        start = datetime.now()
        last_cross = None

        while True:
            now = datetime.now()
            elapsed = (now - start).total_seconds() / 60
            if elapsed > timeout_minutes:
                return False

            if session_name == "S1" and not self._in_session1(now):
                return False
            if session_name == "S2" and not self._in_session2(now):
                return False

            candles = self._get_3min_candles(token)
            if len(candles) < SMA_PERIOD:
                time.sleep(5)
                continue

            sma = self._calc_sma(candles, SMA_PERIOD)
            if sma is None:
                time.sleep(5)
                continue

            current_price = candles[-1]["close"]
            prev_price = candles[-2]["close"] if len(candles) > 1 else current_price
            prev_sma = self._calc_sma(candles[:-1], SMA_PERIOD) if len(candles) > SMA_PERIOD else sma

            if prev_sma is None:
                time.sleep(5)
                continue

            # Detect crossover
            price_above = current_price > sma
            prev_above = prev_price > prev_sma
            spot = self._get_sensex_spot()
            if spot is None:
                time.sleep(5)
                continue

            if price_above and not prev_above:
                if last_cross != "CE":
                    opt = self._get_atm_option(spot, is_ce=True)
                    if opt and self._enter_trade(opt, "CE", candles[-1]):
                        return True
                last_cross = "CE"
            elif not price_above and prev_above:
                if last_cross != "PE":
                    opt = self._get_atm_option(spot, is_ce=False)
                    if opt and self._enter_trade(opt, "PE", candles[-1]):
                        return True
                last_cross = "PE"

            monitor_ip_status(self.kite)
            time.sleep(5)

    def _enter_trade(self, opt: dict, side: str, entry_candle: dict) -> bool:
        side_str = "BUY"
        entry_price = self._get_sensex_spot()
        if entry_price is None:
            return False

        tsym = opt["tsym"]
        qty = opt["qty"] * self.lots

        # SL at entry candle low (CE) or high (PE)
        if side == "CE":
            sl = entry_candle["low"]
            entry_sl = sl
        else:
            sl = entry_candle["high"]
            entry_sl = sl

        risk = abs(entry_price - sl)
        target_price = entry_price + risk if side == "CE" else entry_price - risk

        entry_prem = 0
        try:
            ltp = self.kite.kite.ltp(f"{SENSEX_EXCHANGE}:{tsym}")
            entry_prem = ltp.get(f"{SENSEX_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            pass

        try:
            if is_market_open():
                oid = self.kite.place_market(tsym, side_str, qty, exchange=SENSEX_EXCHANGE)
            else:
                oid = self.kite.place_limit(tsym, side_str, qty, entry_prem or 100, exchange=SENSEX_EXCHANGE)
        except Exception as e:
            print(f"  ✗ {tsym}: {e}")
            return False

        trade = {
            "side": side,
            "entry_price": entry_price,
            "entry_sl": entry_sl,
            "sl": sl,
            "target_level": 1.0,
            "qty": qty,
            "tsym": tsym,
            "strike": opt["strike"],
            "expiry": opt["expiry"],
            "entry_ts": datetime.now().isoformat(),
            "entry_prem": entry_prem,
        }
        self.trades.append(trade)
        self.trades_today += 1
        telegram_logger.strategy_entry_alert("SMA CROSSOVER", [{
            "action": "BUY",
            "tradingsymbol": tsym,
            "strike": opt["strike"],
            "option_type": side,
            "premium": entry_prem,
        }])
        return True

    def run(self):
        """Main SMA crossover loop — supports multiple concurrent trades."""
        print(f"SMA Crossover — {SMA_TIMEFRAME} SMA{SMA_PERIOD}")

        # Check for carryover positions at startup
        try:
            positions = self.kite.kite.positions()["day"]
            for p in positions:
                tsym = p.get("tradingsymbol", "")
                qty = p.get("quantity", 0)
                if qty and "SENSEX" in tsym and p.get("exchange") == SENSEX_EXCHANGE:
                    otype = "CE" if qty > 0 else "PE"
                    trade = {
                        "side": otype,
                        "entry_price": self._get_sensex_spot() or 0,
                        "entry_sl": 0,
                        "sl": 0,
                        "target_level": 1.0,
                        "qty": abs(qty),
                        "tsym": tsym,
                        "strike": float(p.get("strike_price", 0)),
                        "expiry": (p.get("expiry_date", "") or "")[:10],
                        "entry_ts": datetime.now().isoformat(),
                        "entry_prem": 0,
                    }
                    self.trades.append(trade)
                    self.trades_today += 1
                    print(f"  Carryover trade detected: {tsym} x {abs(qty)}")
        except Exception:
            pass

        while True:
            monitor_ip_status(self.kite)
            periodic_connection_test(self.kite)
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            active_count = sum(1 for t in self.trades if self._trade_has_position(t))
            session1_can_enter = self.trades_today < 1 and active_count < 1
            session2_can_enter = self.trades_today < 2 and active_count < 2

            # If past Session 1 window (11:30), mark it done
            if not self.session1_done and (now.hour > 11 or (now.hour == 11 and now.minute > 30)):
                self.session1_done = True

            # Session 1: 9:30-11:30 (only if no carryover trade active)
            if session1_can_enter and not self.session1_done and self._in_session1(now):
                if self._wait_for_crossover("S1"):
                    self.session1_done = True

            # Session 2: 1:00-2:30
            if session2_can_enter and not self.session2_done and self._in_session2(now) and self.session1_done:
                if self._wait_for_crossover("S2"):
                    self.session2_done = True

            # Manage all active trades — trail SL for all
            if self.trades:
                self._manage_trades()

            # Cleanup dead trades (position closed externally)
            self.trades = [t for t in self.trades if self._trade_has_position(t)]

            # If any trade still active, fast loop
            if self.trades:
                time.sleep(5)
                continue

            # If past Session 2 and nothing active, wait for next day
            if not self.trades and (now.hour > 14 or (now.hour == 14 and now.minute > 30)):
                next_day = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
                wait = (next_day - now).total_seconds()
                time.sleep(min(wait, 3600))
                self.trades_today = 0
                self.session1_done = False
                self.session2_done = False

            if self.trades_today >= 2 and not self.trades:
                next_day = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
                wait = (next_day - now).total_seconds()
                time.sleep(min(wait, 3600))
                self.trades_today = 0
                self.session1_done = False
                self.session2_done = False

            time.sleep(10)


# ---------------------------------------------------------------------------
# SMA Crossover Strategy — BANK NIFTY (2-hour resampled from 60-min candles)
# ---------------------------------------------------------------------------

class SmaCrossoverBNF:
    """BANK NIFTY SMA crossover: resample two 60-min → 2hr candle, SMA(60) on 2hr."""

    BNF_EXCHANGE = "NFO"
    BNF_NAME = "BANKNIFTY"
    BNF_STRIKE_GAP = 100
    BNF_RAW_TIMEFRAME = "60minute"
    BNF_PERIOD = 60
    BNF_DEFAULT_LOT = 15
    BNF_INDEX_SYMBOLS = ["NSE:NIFTY BANK", "NSE:BANKNIFTY"]

    def __init__(self, kite: KiteSession, lots: int = 1):
        self.kite = kite
        self.lots = lots
        self.trades_today = 0
        self.trades = []  # list of trade dicts

    def _get_index_token(self) -> Optional[int]:
        """Find BANK NIFTY index instrument token for historical data."""
        for exchange in ("NSE", "BSE"):
            try:
                for row in self.kite.kite.instruments(exchange):
                    tsym = row.get("tradingsymbol", "")
                    exch = row.get("exchange", "")
                    name = row.get("name", "")
                    if name == "NIFTY BANK" and exch == exchange:
                        return int(row["instrument_token"])
                    if tsym in ("NIFTY BANK", "BANKNIFTY") and exch == exchange:
                        return int(row["instrument_token"])
            except Exception:
                continue
        return None

    def _get_bnf_spot(self) -> Optional[float]:
        for sym in self.BNF_INDEX_SYMBOLS:
            try:
                return self.kite.kite.ltp(sym)[sym]["last_price"]
            except Exception:
                continue
        return None

    def _get_60min_candles(self, token: int, lookback_days: int = 14) -> list:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=lookback_days)
        try:
            return self.kite.kite.historical_data(token, from_dt, to_dt, self.BNF_RAW_TIMEFRAME)
        except Exception as e:
            print(f"  Hist data error: {e}")
            return []

    def _resample_2hr(self, candles: list) -> list:
        """Pair consecutive 60-min candles into 2-hour OHLC candles."""
        result = []
        for i in range(0, len(candles) - 1, 2):
            c1, c2 = candles[i], candles[i + 1]
            result.append({
                "date": c2["date"] if "date" in c2 else c2.get("time", c2.get("timestamp", "")),
                "open": c1["open"],
                "high": max(c1["high"], c2["high"]),
                "low": min(c1["low"], c2["low"]),
                "close": c2["close"],
            })
        return result

    def _calc_sma(self, candles: list, period: int) -> Optional[float]:
        if len(candles) < period:
            return None
        closes = [c["close"] for c in candles[-period:]]
        return sum(closes) / period

    def _get_bnf_instruments(self, expiry: str) -> list[dict]:
        self.kite._fetch_instruments()
        return [
            r for r in self.kite._instruments
            if r["exchange"] == self.BNF_EXCHANGE
            and r["name"] == self.BNF_NAME
            and r["instrument_type"] in ("CE", "PE")
            and r["expiry"] == expiry
        ]

    def _get_nearest_expiry(self, instruments: list[dict]) -> Optional[str]:
        today = datetime.now().strftime("%Y-%m-%d")
        seen = set()
        best = None
        best_diff = 999
        for row in instruments:
            exp = row["expiry"]
            if exp in seen:
                continue
            seen.add(exp)
            if exp == today:
                return exp
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            diff = (exp_date - datetime.now().date()).days
            if 0 <= diff < best_diff:
                best_diff = diff
                best = exp
        return best

    def _get_atm_option(self, spot: float, is_ce: bool) -> Optional[dict]:
        atm = round(spot / self.BNF_STRIKE_GAP) * self.BNF_STRIKE_GAP
        self.kite._fetch_instruments()
        s_exps = set()
        for r in self.kite._instruments:
            if r["exchange"] == self.BNF_EXCHANGE and r["name"] == self.BNF_NAME:
                s_exps.add(r["expiry"])
        if not s_exps:
            print("  No BANKNIFTY expiry found")
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        best_exp = min(s_exps, key=lambda e: abs((datetime.strptime(e, "%Y-%m-%d") - datetime.now()).days) if e >= today else 999)
        opts = self._get_bnf_instruments(best_exp)
        otype = "CE" if is_ce else "PE"
        best = None
        best_diff = 999999
        for r in opts:
            if r["instrument_type"] == otype:
                d = abs(float(r["strike"]) - atm)
                if d < best_diff:
                    best_diff = d
                    qty = int(r.get("lot_size", self.BNF_DEFAULT_LOT))
                    best = {"tsym": r["tradingsymbol"], "strike": float(r["strike"]),
                            "qty": qty, "expiry": best_exp}
        return best

    def _trade_has_position(self, trade: dict) -> bool:
        try:
            positions = self.kite.kite.positions()["day"]
            for p in positions:
                if p.get("tradingsymbol") == trade["tsym"] and p.get("quantity", 0) != 0:
                    return True
        except Exception:
            pass
        return False

    def _is_trade_active(self, trade: dict = None) -> bool:
        if trade is not None:
            return self._trade_has_position(trade)
        return any(self._trade_has_position(t) for t in self.trades)

    def _in_session(self, now: datetime) -> bool:
        # Check during market hours when 1hr candles are forming
        return now.hour >= 9 and (now.hour < 15 or (now.hour == 15 and now.minute <= 30))

    def _manage_trades(self):
        spot = self._get_bnf_spot()
        if spot is None:
            return
        to_remove = []
        for trade in self.trades:
            side = trade["side"]
            entry_price = trade["entry_price"]
            sl = trade["sl"]
            target_level = trade["target_level"]
            points = abs(spot - entry_price)
            is_buy = (side == "CE")
            if (is_buy and spot <= sl) or (not is_buy and spot >= sl):
                print(f"  SL hit {trade['tsym']} @ {spot:.2f}, closing")
                self._exit_trade(trade)
                to_remove.append(trade)
            else:
                risk = abs(entry_price - trade["entry_sl"])
                rr = points / risk if risk > 0 else 0
                if rr >= target_level + 1:
                    new_sl = entry_price + (target_level + 1) * risk if is_buy else entry_price - (target_level + 1) * risk
                    if (is_buy and new_sl > sl) or (not is_buy and new_sl < sl):
                        trade["sl"] = new_sl
                        trade["target_level"] = target_level + 1
                        print(f"  Trail {trade['tsym']} SL to {new_sl:.2f} (RR {target_level+1}:1)")
        for t in to_remove:
            if t in self.trades:
                self.trades.remove(t)

    def _exit_trade(self, trade: dict):
        tsym = trade["tsym"]
        qty = trade["qty"]
        exit_spot = self._get_bnf_spot() or 0
        try:
            ltp = self.kite.kite.ltp(f"{self.BNF_EXCHANGE}:{tsym}")
            exit_prem = ltp.get(f"{self.BNF_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            exit_prem = 0
        try:
            self.kite.place_market(tsym, "BUY" if trade["side"] == "PE" else "SELL", qty, exchange=self.BNF_EXCHANGE)
            print(f"  Closed {tsym}")
        except Exception as e:
            print(f"  Close error: {e}")
        entry_prem = trade.get("entry_prem", 0)
        pnl = (entry_prem - exit_prem) * qty if trade["side"] == "CE" else (exit_prem - entry_prem) * qty
        charges = calc_charges([{"action": "BUY", "premium": entry_prem}], qty, exchange=self.BNF_EXCHANGE)
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"BNF_SMA_{trade['side']}",
            "expiry": trade.get("expiry", ""),
            "entry_time": trade.get("entry_ts", ""),
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(trade.get("entry_price", 0), 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(entry_prem, 2),
            "exit_value": round(exit_prem, 2),
            "pnl": round(pnl, 2),
            "charges": charges,
            "max_profit_target": 0,
            "stop_loss": 0,
            "exit_reason": "SL_HIT" if trade.get("sl") else "CLOSED",
        })
        exit_reason = "SL_HIT" if trade.get("sl") else "CLOSED"
        telegram_logger.strategy_exit_alert(f"BNF SMA {trade['side']}", exit_reason, pnl)
        if trade in self.trades:
            self.trades.remove(trade)

    def _wait_for_crossover(self, timeout_minutes: int = 150) -> bool:
        """Monitor 2hr resampled SMA crossover (check every new 1hr candle)."""
        token = self._get_index_token()
        if not token:
            print("  Could not find BANK NIFTY index token")
            return False

        print(f"  Watching 2hr SMA{self.BNF_PERIOD} crossover...")
        start = datetime.now()
        last_cross = None
        last_candle_count = 0

        while True:
            now = datetime.now()
            elapsed = (now - start).total_seconds() / 60
            if elapsed > timeout_minutes:
                return False

            if not is_market_open():
                time.sleep(60)
                continue

            raw = self._get_60min_candles(token)
            if len(raw) < self.BNF_PERIOD * 2:
                time.sleep(30)
                continue

            # Only act when a new 1hr candle has closed
            if len(raw) == last_candle_count:
                time.sleep(30)
                continue
            last_candle_count = len(raw)

            two_hr = self._resample_2hr(raw)
            if len(two_hr) < self.BNF_PERIOD:
                time.sleep(30)
                continue

            sma = self._calc_sma(two_hr, self.BNF_PERIOD)
            if sma is None:
                time.sleep(30)
                continue

            current_candle = two_hr[-1]
            prev_candle = two_hr[-2] if len(two_hr) > 1 else current_candle
            prev_sma = self._calc_sma(two_hr[:-1], self.BNF_PERIOD) if len(two_hr) > self.BNF_PERIOD else sma

            if prev_sma is None:
                time.sleep(30)
                continue

            price_above = current_candle["close"] > sma
            prev_above = prev_candle["close"] > prev_sma
            spot = self._get_bnf_spot()
            if spot is None:
                time.sleep(30)
                continue

            if price_above and not prev_above:
                if last_cross != "CE":
                    opt = self._get_atm_option(spot, is_ce=True)
                    if opt and self._enter_trade(opt, "CE", current_candle):
                        return True
                last_cross = "CE"
            elif not price_above and prev_above:
                if last_cross != "PE":
                    opt = self._get_atm_option(spot, is_ce=False)
                    if opt and self._enter_trade(opt, "PE", current_candle):
                        return True
                last_cross = "PE"

            monitor_ip_status(self.kite)
            time.sleep(30)

    def _enter_trade(self, opt: dict, side: str, entry_candle: dict) -> bool:
        side_str = "BUY"
        entry_price = self._get_bnf_spot()
        if entry_price is None:
            return False

        tsym = opt["tsym"]
        qty = opt["qty"] * self.lots

        if side == "CE":
            sl = entry_candle["low"]
            entry_sl = sl
        else:
            sl = entry_candle["high"]
            entry_sl = sl

        risk = abs(entry_price - sl)
        target_price = entry_price + risk if side == "CE" else entry_price - risk

        entry_prem = 0
        try:
            ltp = self.kite.kite.ltp(f"{self.BNF_EXCHANGE}:{tsym}")
            entry_prem = ltp.get(f"{self.BNF_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            pass

        try:
            if is_market_open():
                oid = self.kite.place_market(tsym, side_str, qty, exchange=self.BNF_EXCHANGE)
            else:
                oid = self.kite.place_limit(tsym, side_str, qty, entry_prem or 100, exchange=self.BNF_EXCHANGE)
        except Exception as e:
            print(f"  ✗ {tsym}: {e}")
            return False

        trade = {
            "side": side,
            "entry_price": entry_price,
            "entry_sl": entry_sl,
            "sl": sl,
            "target_level": 1.0,
            "qty": qty,
            "tsym": tsym,
            "strike": opt["strike"],
            "expiry": opt["expiry"],
            "entry_ts": datetime.now().isoformat(),
            "entry_prem": entry_prem,
        }
        self.trades.append(trade)
        self.trades_today += 1
        telegram_logger.strategy_entry_alert("BNF SMA CROSSOVER", [{
            "action": "BUY",
            "tradingsymbol": tsym,
            "strike": opt["strike"],
            "option_type": side,
            "premium": entry_prem,
        }])
        return True

    def _next_market_open(self, now: datetime) -> datetime:
        n = now + timedelta(days=1)
        if now.weekday() >= 4:
            n += timedelta(days=(7 - now.weekday()))
        return n.replace(hour=9, minute=15, second=0, microsecond=0)

    def _load_carryover_positions(self):
        try:
            positions = self.kite.kite.positions()["day"]
            for p in positions:
                tsym = p.get("tradingsymbol", "")
                qty = p.get("quantity", 0)
                if qty and "BANKNIFTY" in tsym and p.get("exchange") == self.BNF_EXCHANGE:
                    otype = "CE" if qty > 0 else "PE"
                    trade = {
                        "side": otype,
                        "entry_price": self._get_bnf_spot() or 0,
                        "entry_sl": 0,
                        "sl": 0,
                        "target_level": 1.0,
                        "qty": abs(qty),
                        "tsym": tsym,
                        "strike": float(p.get("strike_price", 0)),
                        "expiry": (p.get("expiry_date", "") or "")[:10],
                        "entry_ts": datetime.now().isoformat(),
                        "entry_prem": 0,
                    }
                    self.trades.append(trade)
                    self.trades_today += 1
                    print(f"  Carryover trade detected: {tsym} x {abs(qty)}")
        except Exception:
            pass

    def _check_crossover(self, last_candle_count: int):
        """Non-blocking crossover check. Returns (new_candle_count, side or None)."""
        token = self._get_index_token()
        if not token:
            return last_candle_count, None

        raw = self._get_60min_candles(token)
        if len(raw) < self.BNF_PERIOD * 2:
            return last_candle_count, None

        if len(raw) == last_candle_count:
            return last_candle_count, None

        two_hr = self._resample_2hr(raw)
        if len(two_hr) < self.BNF_PERIOD:
            return last_candle_count, None

        sma = self._calc_sma(two_hr, self.BNF_PERIOD)
        if sma is None:
            return last_candle_count, None

        prev_sma = self._calc_sma(two_hr[:-1], self.BNF_PERIOD) if len(two_hr) > self.BNF_PERIOD else sma
        if prev_sma is None:
            return last_candle_count, None

        current_candle = two_hr[-1]
        prev_candle = two_hr[-2] if len(two_hr) > 1 else current_candle

        price_above = current_candle["close"] > sma
        prev_above = prev_candle["close"] > prev_sma

        side = None
        if price_above and not prev_above:
            side = "CE"
        elif not price_above and prev_above:
            side = "PE"

        return len(raw), side

    def run(self):
        """Main SMA crossover loop — multiple concurrent trades."""
        print(f"BNF SMA Crossover — 2hr resampled SMA{self.BNF_PERIOD}")

        self._load_carryover_positions()

        last_candle_count = 0
        last_cross = None

        while True:
            monitor_ip_status(self.kite)
            periodic_connection_test(self.kite)
            now = datetime.now()

            active_count = sum(1 for t in self.trades if self._trade_has_position(t))

            # Single crossover check per loop (non-blocking)
            if active_count < 2 and self._in_session(now):
                cross_signal, side = self._check_crossover(last_candle_count)
                if isinstance(cross_signal, int):
                    last_candle_count = cross_signal
                if side:
                    spot = self._get_bnf_spot()
                    if spot:
                        opt = self._get_atm_option(spot, is_ce=(side == "CE"))
                        if opt:
                            candle = {"low": spot - 50, "high": spot + 50}  # fallback
                            if self._enter_trade(opt, side, candle):
                                last_cross = side

            # Manage all active trades
            if self.trades:
                self._manage_trades()

            self.trades = [t for t in self.trades if self._trade_has_position(t)]

            if self.trades:
                time.sleep(10)
                continue

            # Market closed — sleep until next market open
            if not is_market_open():
                next_open = self._next_market_open(now)
                wait = (next_open - now).total_seconds()
                time.sleep(min(wait, 3600))

            time.sleep(10)

# ---------------------------------------------------------------------------
# NIFTY 1H SMA60 Options Strategy — Multi-Leg Condor with Adjustment
# ---------------------------------------------------------------------------

N1H_EXCHANGE = "NFO"
N1H_NAME = "NIFTY"
N1H_SPOT_SYMBOL = "NSE:NIFTY 50"
N1H_TIMEFRAME = "60minute"
N1H_PERIOD = 60
N1H_STRIKE_GAP = 50
N1H_LOT_DEFAULT = 25

# Strike offsets from ATM
N1H_BUY1_OFFSET = 250
N1H_SELL1_OFFSET = 450
N1H_SELL2_OFFSET = 650
N1H_BUY2_OFFSET = 700

# Adjustment: roll buy1 by this many points when locking
N1H_LOCK_ROLL = 100  # e.g., ATM+250 → ATM+350 (bullish) / ATM-250 → ATM-350 (bearish)


class NiftySMAOptions:
    """Nifty 1H SMA60 crossover → 4-leg options structure with adjustment lock."""

    def __init__(self, kite: KiteSession):
        self.kite = kite
        self.position: Optional[dict] = None  # position state
        self.entry_time: str = ""
        self.entry_spot: float = 0
        self._order_ids: dict[str, str] = {}

    # ── helpers ──────────────────────────────────────────────

    def _get_index_token(self) -> Optional[int]:
        for exchange in ("NSE", "BSE"):
            try:
                for row in self.kite.kite.instruments(exchange):
                    tsym = row.get("tradingsymbol", "")
                    if tsym in ("NIFTY 50", "NIFTY"):
                        return int(row["instrument_token"])
                    if "NIFTY" in tsym and row.get("instrument_type", "") == "":
                        return int(row["instrument_token"])
            except Exception:
                continue
        return None

    def _get_60min_candles(self, token: int, lookback_days: int = 10) -> list:
        to_dt = datetime.now()
        try:
            return self.kite.kite.historical_data(
                token, to_dt - timedelta(days=lookback_days), to_dt, N1H_TIMEFRAME
            )
        except Exception as e:
            print(f"  Hist data error: {e}")
            return []

    def _calc_sma(self, candles: list, period: int) -> Optional[float]:
        if len(candles) < period:
            return None
        return sum(c["close"] for c in candles[-period:]) / period

    def _round_strike(self, spot: float, offset: int, side: str) -> int:
        """Round spot ± offset to nearest valid NIFTY strike (multiple of 50)."""
        raw = (spot - offset) if side == "PE" else (spot + offset)
        return int(round(raw / N1H_STRIKE_GAP) * N1H_STRIKE_GAP)

    def _get_option_tsym(self, strike: int, otype: str, expiry: str) -> Optional[str]:
        self.kite._fetch_instruments()
        for r in self.kite._instruments:
            if (r["exchange"] == N1H_EXCHANGE and r["name"] == N1H_NAME
                    and r["expiry"] == expiry and r["instrument_type"] == otype
                    and abs(float(r["strike"]) - strike) < 1):
                return r["tradingsymbol"]
        return None

    def _get_option_premium(self, tsym: str) -> float:
        try:
            q = self.kite.kite.ltp(f"{N1H_EXCHANGE}:{tsym}")
            return q.get(f"{N1H_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            return 0

    def _get_atm(self) -> int:
        spot = self.kite.get_nse_spot() or 0
        return int(round(spot / N1H_STRIKE_GAP) * N1H_STRIKE_GAP)

    def _side_from_crossover(self) -> Optional[str]:
        """Check 1H SMA60 crossover. Returns 'CE' (bullish) or 'PE' (bearish) or None."""
        token = self._get_index_token()
        if not token:
            return None
        candles = self._get_60min_candles(token)
        if len(candles) < N1H_PERIOD + 1:
            return None
        sma = self._calc_sma(candles, N1H_PERIOD)
        prev_sma = self._calc_sma(candles[:-1], N1H_PERIOD)
        if sma is None or prev_sma is None:
            return None
        cc = candles[-1]["close"]
        pc = candles[-2]["close"]
        above = cc > sma
        prev_above = pc > prev_sma
        if above and not prev_above:
            return "CE"  # bullish
        if not above and prev_above:
            return "PE"  # bearish
        return None

    # ── build / enter ────────────────────────────────────────

    def _build_legs(self, side: str, expiry: str) -> Optional[list[dict]]:
        """Build 4-leg structure for given direction."""
        atm = self._get_atm()
        if side == "CE":
            strikes = [
                self._round_strike(atm, N1H_BUY1_OFFSET, "CE"),
                self._round_strike(atm, N1H_SELL1_OFFSET, "CE"),
                self._round_strike(atm, N1H_SELL2_OFFSET, "CE"),
                self._round_strike(atm, N1H_BUY2_OFFSET, "CE"),
            ]
            actions = ["BUY", "SELL", "SELL", "BUY"]
            otype = "CE"
        else:
            strikes = [
                self._round_strike(atm, N1H_BUY1_OFFSET, "PE"),
                self._round_strike(atm, N1H_SELL1_OFFSET, "PE"),
                self._round_strike(atm, N1H_SELL2_OFFSET, "PE"),
                self._round_strike(atm, N1H_BUY2_OFFSET, "PE"),
            ]
            actions = ["BUY", "SELL", "SELL", "BUY"]
            otype = "PE"

        legs = []
        for strike, action in zip(strikes, actions):
            tsym = self._get_option_tsym(strike, otype, expiry)
            if not tsym:
                print(f"  {red('✗ Strike')} {strike} {otype} not found for {expiry}")
                return None
            prem = self._get_option_premium(tsym)
            legs.append({
                "tsym": tsym, "strike": strike, "option_type": otype,
                "action": action, "premium": prem, "exchange": N1H_EXCHANGE,
                "expiry": expiry,
            })
        return legs

    def _calc_max_loss(self, legs: list[dict]) -> float:
        net = sum(-l["premium"] if l["action"] == "BUY" else l["premium"] for l in legs)
        qty = self._get_qty(legs[0]["tsym"])
        return max(0, net * qty)

    def _get_qty(self, tsym: str) -> int:
        """Read lot size for a given trading symbol."""
        self.kite._fetch_instruments()
        for r in self.kite._instruments:
            if r["tradingsymbol"] == tsym:
                return int(r.get("lot_size", N1H_LOT_DEFAULT))
        return N1H_LOT_DEFAULT

    def _enter(self, legs: list[dict]) -> bool:
        """Place all legs. BUY first, then SELL if market open."""
        qty = self._get_qty(legs[0]["tsym"])
        print(f"\n{bold(green('ENTER'))} — {legs[0]['option_type']} ({qty} qty)")
        buy_legs = [l for l in legs if l["action"] == "BUY"]
        sell_legs = [l for l in legs if l["action"] == "SELL"]
        oids: dict[str, str] = {}

        for leg in buy_legs:
            try:
                oid = self.kite.place_market(leg["tsym"], "BUY", qty, exchange=N1H_EXCHANGE)
                oids[leg["tsym"]] = oid
                print(f"  {green('BUY')} {leg['tsym']} @ mkt")
            except Exception as e:
                print(f"  {red('✗ BUY')} {leg['tsym']}: {e}")
                self._cancel_oids(oids)
                return False

        for leg in sell_legs:
            try:
                oid = self.kite.place_market(leg["tsym"], "SELL", qty, exchange=N1H_EXCHANGE)
                oids[leg["tsym"]] = oid
                print(f"  {red('SELL')} {leg['tsym']} @ mkt")
            except Exception as e:
                print(f"  {red('✗ SELL')} {leg['tsym']}: {e}")
                self._cancel_oids(oids)
                return False

        self.position = {
            "legs": legs, "side": legs[0]["option_type"],
            "entry_spot": self.entry_spot, "entry_time": self.entry_time,
            "entry_atm": int(round(self.entry_spot / N1H_STRIKE_GAP) * N1H_STRIKE_GAP),
            "max_loss": self._calc_max_loss(legs),
            "qty": qty,
            "locked": False, "lock_level": 0,
        }
        self._order_ids = oids
        telegram_logger.strategy_entry_alert("N1H OPTIONS", [{
            "action": l["action"], "tradingsymbol": l["tsym"],
            "strike": l["strike"], "option_type": l["option_type"],
            "premium": l["premium"],
        } for l in legs])
        return True

    def _cancel_oids(self, oids: dict):
        for tsym, oid in oids.items():
            try:
                self.kite.kite.cancel_order("regular", oid)
            except Exception:
                try:
                    self.kite.kite.cancel_order("amo", oid)
                except Exception:
                    pass

    # ── monitor ──────────────────────────────────────────────

    def _current_pnl(self) -> float:
        if not self.position:
            return 0
        qty = self.position["qty"]
        current = 0.0
        for leg in self.position["legs"]:
            prem = self._get_option_premium(leg["tsym"])
            if leg["action"] == "SELL":
                current += leg["premium"] - prem
            else:
                current += prem - leg["premium"]
        return current * qty

    def _adjust(self):
        """Lock profit by rolling the primary buy leg (offset 250)."""
        side = self.position["side"]
        legs = self.position["legs"]
        atm = self.position["entry_atm"]
        qty = self.position["qty"]

        # Identify the primary buy leg (buy1 at offset 250 from ATM)
        buy1 = next((l for l in legs if l["action"] == "BUY"
                     and abs(l["strike"] - atm) == N1H_BUY1_OFFSET), None)
        if not buy1:
            print(f"  {red('Could not identify buy1 leg for adjustment')}")
            return

        # Close the primary buy leg
        try:
            self.kite.place_market(buy1["tsym"],
                                   "SELL" if buy1["action"] == "BUY" else "BUY",
                                   qty, exchange=N1H_EXCHANGE)
            print(f"  {green('Close')} {buy1['tsym']}")
        except Exception as e:
            print(f"  {red('Close error')}: {e}")

        # Remove closed leg from tracking
        legs.remove(buy1)

        # Open new buy leg (closer to ATM by lock_roll)
        new_offset = N1H_BUY1_OFFSET - N1H_LOCK_ROLL
        new_strike = (atm + new_offset) if side == "CE" else (atm - new_offset)
        new_strike = int(round(new_strike / N1H_STRIKE_GAP) * N1H_STRIKE_GAP)
        expiry = buy1.get("expiry", "")
        new_tsym = self._get_option_tsym(new_strike, side, expiry)
        if new_tsym:
            try:
                self.kite.place_market(new_tsym, "BUY", qty, exchange=N1H_EXCHANGE)
                print(f"  {green('BUY')} {new_tsym} (adjustment)")
                legs.append({
                    "tsym": new_tsym, "strike": new_strike, "option_type": side,
                    "action": "BUY", "premium": 0, "exchange": N1H_EXCHANGE,
                    "expiry": expiry,
                })
            except Exception as e:
                print(f"  {red('Adjust error')}: {e}")

        self.position["locked"] = True
        self.position["lock_level"] = 1
        print(f"  {bold(green('✅ Profit locked — zero loss achieved'))}")

    # ── run ──────────────────────────────────────────────────

    def run(self):
        """Main loop: watch for crossover → enter → monitor → adjust → exit."""
        print(f"{bold(cyan('NIFTY 1H SMA60 Options Strategy'))}")
        last_candle_count = 0

        while True:
            monitor_ip_status(self.kite)
            periodic_connection_test(self.kite)
            now = datetime.now()

            # ── ENTRY PHASE ──
            if self.position is None:
                if not is_market_open():
                    next_open = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
                    if now.weekday() >= 4:
                        next_open += timedelta(days=(7 - now.weekday()))
                    time.sleep(min((next_open - now).total_seconds(), 3600))
                    continue

                # Check for crossover (only when new 1H candle closes)
                raw = self._get_60min_candles(self._get_index_token() or 0)
                if len(raw) == last_candle_count or len(raw) < N1H_PERIOD + 1:
                    time.sleep(15)
                    continue
                last_candle_count = len(raw)

                side = self._side_from_crossover()
                if side:
                    print(f"  {bold(cyan('Crossover detected'))}: {side}")
                    self.entry_spot = self.kite.get_nse_spot() or 0
                    self.entry_time = now.isoformat()
                    expiry = nearest_expiry_today(self.kite.get_option_instruments())
                    if not expiry:
                        print("  No expiry found")
                        time.sleep(30)
                        continue
                    legs = self._build_legs(side, expiry)
                    if legs and self._enter(legs):
                        print(f"  {bold(green('Position entered'))}: {side}")
                    else:
                        print(f"  {red('Entry failed')}")
                time.sleep(30)
                continue

            # ── MONITOR / ADJUST / EXIT PHASE ──
            legs = self.position["legs"]
            pnl = self._current_pnl()
            max_loss = self.position["max_loss"]
            side = self.position["side"]

            # Market closed — sleep until next open, carry position forward
            if not is_market_open():
                next_open = (now + timedelta(days=1)).replace(hour=9, minute=15, second=0, microsecond=0)
                if now.weekday() >= 4:
                    next_open += timedelta(days=(7 - now.weekday()))
                wait = min((next_open - now).total_seconds(), 3600)
                print(f"  {bold('Market closed — carryover')}, sleep {wait:.0f}s")
                time.sleep(wait)
                continue

            # Adjustment: when profit equals max loss
            if not self.position["locked"] and pnl >= max_loss:
                self._adjust()
                # After lock, continue monitoring

            # Trail after lock: 1:1, 1:2
            if self.position.get("locked"):
                lock_base = max_loss  # already recovered
                extra = pnl - lock_base
                if extra >= lock_base * 2:  # 1:2 RR
                    self._exit("TARGET_1_2", pnl)
                    continue
                elif extra >= lock_base:  # 1:1 RR — partial trail
                    if self.position.get("lock_level", 0) < 2:
                        self.position["lock_level"] = 2
                        print(f"  {bright('1:1 RR achieved — trailing')}")

            # Hard stop: if P&L drops below -max_loss (unlikely after lock)
            if pnl <= -max_loss:
                self._exit("STOP_LOSS", pnl)
                continue

            time.sleep(10)

    def _exit(self, reason: str, pnl: float):
        if not self.position:
            return
        legs = self.position["legs"]
        qty = self.position["qty"]
        print(f"\n{bold(red('EXIT'))} ({reason}) — P&L ₹{pnl:+.2f}")
        exit_spot = self.kite.get_nse_spot() or 0
        current = 0.0
        for leg in legs:
            prem = self._get_option_premium(leg["tsym"])
            if leg["action"] == "SELL":
                current += leg["premium"] - prem
            else:
                current += prem - leg["premium"]
            reverse = "BUY" if leg["action"] == "SELL" else "SELL"
            try:
                self.kite.place_market(leg["tsym"], reverse, qty, exchange=N1H_EXCHANGE)
                print(f"  {red('Close')} {leg['tsym']}")
            except Exception as e:
                print(f"  {red('✗')} {leg['tsym']}: {e}")
        charges = calc_charges(
            [{"action": l["action"], "premium": l["premium"]} for l in legs],
            qty, exchange=N1H_EXCHANGE,
        )
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"N1H_{self.position['side']}",
            "expiry": next((l.get("expiry", "") for l in legs), ""),
            "entry_time": self.position.get("entry_time", ""),
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(self.position.get("entry_spot", 0), 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(sum(l["premium"] for l in legs if l["action"] == "SELL") -
                                   sum(l["premium"] for l in legs if l["action"] == "BUY"), 2),
            "exit_value": round(current, 2),
            "pnl": round(pnl, 2),
            "charges": charges,
            "max_profit_target": 0,
            "stop_loss": round(self.position.get("max_loss", 0), 2),
            "exit_reason": reason,
        })
        telegram_logger.strategy_exit_alert("N1H OPTIONS", reason, pnl)
        self.position = None
        print("Done.")


# ---------------------------------------------------------------------------
# Credit Spread runner
# ---------------------------------------------------------------------------

def _run_credit_spread(kite: KiteSession, manager: CreditSpreadManager, args):
    cfg = load_config()

    if args.resume:
        print("Scanning live Zerodha positions for credit spread...")
        cs = manager.resume_from_positions()
        if not cs:
            return
        manager.position = cs
        manager.entry_credit = cs.net_credit
        print(f"Resumed {cs.spread_type}, credit ₹{cs.net_credit:.2f}")

    if cfg.get("cs_position") and not args.resume:
        p = cfg["cs_position"]
        legs = [CreditSpreadLeg(**l) for l in p["legs"]]
        cs = CreditSpread(spot=0, expiry=p["expiry"], spread_type=p["spread_type"],
                          legs=legs, net_credit=p["entry_credit"], width=0,
                          lower_breakeven=0, upper_breakeven=0, trend="resumed")
        manager.position = cs
        manager.entry_credit = p["entry_credit"]

    if not args.resume and manager.position is None:
        if is_market_open():
            now = datetime.now()
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now < target:
                print(f"Wait until 10:00 ({((target-now).total_seconds()):.0f}s)...")
                while datetime.now() < target:
                    time.sleep(30)

        while manager.position is None:
            if not is_market_open():
                print("Market closed.")
                return

            c = load_config()
            if c.get("cs_position") and not args.resume:
                break

            cs = manager.scan()
            while not cs:
                if not is_market_open():
                    print("Market closed.")
                    return
                trend = manager._detect_trend()
                if trend and (trend["ranging"] or not trend["trending"]):
                    print("Ranging — fallback to IC...")
                    ic_manager = IronCondorManager(kite)
                    ic = ic_manager.scan()
                    if ic:
                        balance = kite.get_balance()
                        required = ic.max_loss()
                        if balance < required:
                            print(f"  Margin ₹{balance:,.0f} < ₹{required:,.0f}. Retry...")
                            time.sleep(60)
                            cs = manager.scan()
                            continue
                        pos_entry = {"expiry": ic.expiry, "entry_credit": ic.net_credit,
                                     "legs": [asdict(l) for l in ic.legs]}
                        save_config({**load_config(), "position": pos_entry})
                        ok = ic_manager.enter(ic)
                        if not ok:
                            print("IC fail, retry CS...")
                            time.sleep(60)
                            cs = manager.scan()
                            continue
                        if not is_market_open():
                            print("AMO, waiting market open...")
                            while not is_market_open():
                                time.sleep(60)
                            time.sleep(30)
                            if not ic_manager.verify_fills():
                                c = load_config()
                                c.pop("position", None)
                                save_config(c)
                                print("IC fills fail, retry CS...")
                                time.sleep(60)
                                cs = manager.scan()
                                continue
                        else:
                            time.sleep(5)
                            if not ic_manager.verify_fills():
                                c = load_config()
                                c.pop("position", None)
                                save_config(c)
                                print("IC fills fail, retry CS...")
                                time.sleep(60)
                                cs = manager.scan()
                                continue
                        target_str = "short premium ≤ ₹2" if ic.expiry == datetime.now().strftime("%Y-%m-%d") else f"₹{PROFIT_TARGET_RS} profit"
                        print(f"IC monitor ({target_str}, SL @ credit)")
                        try:
                            while True:
                                monitor_ip_status(kite)
                                if not kite.ensure_auth():
                                    time.sleep(60)
                                    continue
                                time.sleep(60)
                                action = ic_manager.monitor()
                                if action in ("EXIT_PROFIT", "EXIT_LOSS", "EXIT_TIME"):
                                    ic_manager.exit(action)
                                    c = load_config()
                                    c.pop("position", None)
                                    save_config(c)
                                    return
                        except Exception as e:
                            telegram_logger.error_alert("IC (CS fallback) Strategy", str(e))
                            raise
                    else:
                        print("No IC either, retry CS...")
                else:
                    print("No setup, retry 60s...")
                time.sleep(60)
                cs = manager.scan()

            # Found a valid CS setup — try entry
            is_0dte = cs.expiry == datetime.now().strftime("%Y-%m-%d")
            print(json.dumps(cs.to_dict(), indent=2))
            balance = kite.get_balance()
            required = cs.max_loss()
            print(f"\nBalance: ₹{balance:,.0f}  Required: ₹{required:,.0f}")
            if balance < required:
                print(f"  Insufficient margin. Need ₹{required:,.0f}")
                print("  Waiting 60s then retrying scan...")
                time.sleep(60)
                continue
            pos_entry = {"expiry": cs.expiry, "entry_credit": cs.net_credit,
                         "spread_type": cs.spread_type, "legs": [asdict(l) for l in cs.legs]}
            save_config({**load_config(), "cs_position": pos_entry})
            ok = manager.enter(cs)
            if not ok:
                print("  Entry failed. Cleaning up and retrying in 60s...")
                c = load_config()
                c.pop("cs_position", None)
                save_config(c)
                time.sleep(60)
                continue
            if not is_market_open():
                print("AMO placed. Waiting for market open...")
                while not is_market_open():
                    time.sleep(60)
                time.sleep(30)
            else:
                time.sleep(5)
            # Verify fills
            expected = {l.tradingsymbol for l in cs.legs}
            try:
                for o in kite.kite.orders():
                    if o["tradingsymbol"] in expected and o["status"] == "COMPLETE" and o["filled_quantity"] >= LOT_SIZE:
                        expected.discard(o["tradingsymbol"])
            except Exception:
                pass
            if expected:
                print("Not all legs filled. Squaring off and retrying...")
                manager.exit("PARTIAL_FILL")
                c = load_config()
                c.pop("cs_position", None)
                save_config(c)
                manager.position = None
                manager.entry_credit = 0.0
                time.sleep(60)
                continue
            print("All legs filled ✓")
            break

    target_str = "short prem ≤ ₹2" if manager.position.expiry == datetime.now().strftime("%Y-%m-%d") else f"₹{CS_PROFIT_TARGET_RS} profit"
    print(f"Monitoring every 10s. Target: {target_str}, SL at credit.")
    try:
        while True:
            monitor_ip_status(kite)
            if not kite.ensure_auth():
                time.sleep(10)
                continue
            time.sleep(10)
            periodic_connection_test(kite)
            action = manager.monitor()
            if action in ("EXIT_PROFIT", "EXIT_LOSS", "EXIT_TIME", "EXIT_REQUESTED"):
                manager.exit(action)
                c = load_config()
                c.pop("cs_position", None)
                save_config(c)
                print("Done.")
                return
    except Exception as e:
        telegram_logger.error_alert("Credit Spread Strategy", str(e))
        raise


# ---------------------------------------------------------------------------
# Manual Trade Manager — detect single-leg positions, trail SL
# ---------------------------------------------------------------------------

MT_CONFIG_KEY = "manual_trade"

class ManualTradeManager:
    def __init__(self, kite: KiteSession):
        self.kite = kite
        self.trade: dict | None = None

    def detect_position(self) -> dict | None:
        """Scan live Zerodha positions for single-leg manual trades."""
        try:
            positions = self.kite.kite.positions()["day"]
        except Exception as e:
            print(f"  Positions fetch error: {e}")
            return None

        # Exclude IC/CS multi-leg positions
        nifty_option_tsyms = set()
        for p in positions:
            tsym = p.get("tradingsymbol", "")
            if "NIFTY" in tsym and abs(p.get("quantity", 0)) > 0:
                nifty_option_tsyms.add(tsym)

        # Find the single leg (not part of any 2-leg or 4-leg strategy)
        for p in positions:
            tsym = p.get("tradingsymbol", "")
            qty = p.get("quantity", 0)
            if qty == 0:
                continue
            exchange = p.get("exchange", "")
            if exchange not in ("NFO", "BFO"):
                continue
            strike = float(p.get("strike_price", 0))
            otype = p.get("instrument_type", "")
            if otype not in ("CE", "PE"):
                continue
            lot = abs(qty)
            side = "BUY" if qty > 0 else "SELL"
            expiry = (p.get("expiry_date", "") or "")[:10]
            return {
                "tsym": tsym,
                "strike": strike,
                "option_type": otype,
                "side": side,
                "qty": lot,
                "exchange": exchange,
                "expiry": expiry,
                "entry_qty": qty,
            }
        return None

    def init_from_config(self) -> bool:
        """Load saved trade + SL from config."""
        cfg = load_config()
        saved = cfg.get(MT_CONFIG_KEY)
        if not saved:
            return False
        self.trade = saved
        print(f"  Resumed manual trade: {self.trade['tsym']} | SL @ {self.trade['sl']}")
        return True

    def save_to_config(self):
        cfg = {**load_config(), MT_CONFIG_KEY: self.trade}
        save_config(cfg)

    def clear_config(self):
        cfg = load_config()
        cfg.pop(MT_CONFIG_KEY, None)
        save_config(cfg)

    def start(self, sl_points: float | None = None):
        """Start monitoring a detected manual trade. User provides SL in points."""
        pos = self.detect_position()
        if not pos:
            print("  No manual trade detected in Zerodha positions.")
            return False

        tsym = pos["tsym"]
        # Get current premium
        exchange = pos["exchange"]
        try:
            ltp = self.kite.kite.ltp(f"{exchange}:{tsym}")
            prem = ltp.get(f"{exchange}:{tsym}", {}).get("last_price", 0)
        except Exception:
            prem = 0

        side = pos["side"]
        otype = pos["option_type"]

        if sl_points is None:
            print(f"\n  Detected: {side} {tsym} x {pos['qty']} @ ₹{prem:.2f}")
            print("  Enter SL in points (e.g. 50 for ₹50): ", end="")
            try:
                sl_points = float(input().strip())
            except (ValueError, EOFError):
                print("  Invalid, defaulting to 50 points")
                sl_points = 50.0

        entry_price = prem
        is_buy = (side == "BUY")
        sl_price = entry_price - sl_points if is_buy else entry_price + sl_points

        self.trade = {
            "tsym": tsym,
            "strike": pos["strike"],
            "option_type": otype,
            "side": side,
            "qty": pos["qty"],
            "exchange": exchange,
            "expiry": pos["expiry"],
            "entry_price": entry_price,
            "entry_sl": sl_price,
            "sl": sl_price,
            "target_level": 1.0,
            "entry_ts": datetime.now().isoformat(),
        }
        self.save_to_config()
        print(f"  Monitoring {side} {tsym} | Entry ₹{entry_price:.2f} | SL ₹{sl_price:.2f}")
        return True

    def monitor(self):
        if not self.trade:
            return "NO_TRADE"
        tsym = self.trade["tsym"]
        exchange = self.trade["exchange"]
        try:
            ltp = self.kite.kite.ltp(f"{exchange}:{tsym}")
            current_prem = ltp.get(f"{exchange}:{tsym}", {}).get("last_price", 0)
        except Exception:
            current_prem = 0

        entry = self.trade["entry_price"]
        sl = self.trade["sl"]
        side = self.trade["side"]
        is_buy = (side == "BUY")

        # SL check
        if is_buy and current_prem <= sl:
            print(f"  SL hit @ ₹{current_prem:.2f}")
            self.exit("SL_HIT")
            return "EXIT_SL"
        elif not is_buy and current_prem >= sl:
            print(f"  SL hit @ ₹{current_prem:.2f}")
            self.exit("SL_HIT")
            return "EXIT_SL"

        # Trail SL
        points = abs(current_prem - entry)
        risk = abs(entry - self.trade["entry_sl"])
        target_level = self.trade["target_level"]
        rr = points / risk if risk > 0 else 0

        if rr >= target_level + 1:
            if is_buy:
                new_sl = entry + (target_level + 1) * risk
            else:
                new_sl = entry - (target_level + 1) * risk
            if (is_buy and new_sl > sl) or (not is_buy and new_sl < sl):
                self.trade["sl"] = new_sl
                self.trade["target_level"] = target_level + 1
                self.save_to_config()
                print(f"  Trail SL to ₹{new_sl:.2f} (RR {target_level+1}:1)")
        return "HOLD"

    def exit(self, reason: str):
        if not self.trade:
            return
        tsym = self.trade["tsym"]
        qty = self.trade["qty"]
        exchange = self.trade["exchange"]
        side = self.trade["side"]

        reverse = "SELL" if side == "BUY" else "BUY"
        try:
            self.kite.place_market(tsym, reverse, qty, exchange=exchange)
            print(f"  Closed {tsym}")
        except Exception as e:
            print(f"  Close error: {e}")

        try:
            ltp = self.kite.kite.ltp(f"{exchange}:{tsym}")
            exit_prem = ltp.get(f"{exchange}:{tsym}", {}).get("last_price", 0)
        except Exception:
            exit_prem = 0

        entry_prem = self.trade["entry_price"]
        pnl = (exit_prem - entry_prem) * qty if side == "BUY" else (entry_prem - exit_prem) * qty
        charges = calc_charges([{"action": side, "premium": entry_prem}], qty, exchange=self.trade.get("exchange", "NFO"))
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"MT_{self.trade['option_type']}",
            "expiry": self.trade.get("expiry", ""),
            "entry_time": self.trade.get("entry_ts", ""),
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(entry_prem, 2),
            "exit_spot": round(exit_prem, 2),
            "entry_credit": round(entry_prem if side == "SELL" else 0, 2),
            "exit_value": round(exit_prem, 2),
            "pnl": round(pnl, 2),
            "charges": charges,
            "max_profit_target": 0,
            "stop_loss": round(self.trade.get("entry_sl", 0), 2),
            "exit_reason": reason,
        })
        self.clear_config()
        self.trade = None


def _run_manual_trade(kite: KiteSession, args):
    manager = ManualTradeManager(kite)

    # Try resume from config
    if args.resume and manager.init_from_config():
        pass
    elif manager.init_from_config():
        print("  Saved manual trade found. Resuming...")
    else:
        ok = manager.start()
        if not ok:
            return

    print("  Monitoring every 60s. Trailing SL active.")
    try:
        while True:
            monitor_ip_status(kite)
            if not kite.ensure_auth():
                time.sleep(60)
                continue
            time.sleep(10)
            action = manager.monitor()
            if action in ("EXIT_SL",):
                print("Done.")
                return
    except Exception as e:
        telegram_logger.error_alert("Manual Trade", str(e))
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _fetch_ip(family) -> str:
    import socket as _socket
    _orig = _socket.getaddrinfo
    def _patch(h, p, f=0, t=0, pr=0, fl=0):
        return _orig(h, p, family, t, pr, fl)
    _socket.getaddrinfo = _patch
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        return ""
    finally:
        _socket.getaddrinfo = _orig

def get_public_ip_v6() -> str:
    return _fetch_ip(socket.AF_INET6)

def get_public_ip_v4() -> str:
    return _fetch_ip(socket.AF_INET)

_ip_check_interval = 120  # seconds between whitelist checks

def check_ip_whitelist(kite: "KiteSession") -> Optional[bool]:
    """Check if current IP is whitelisted by making a lightweight API call.
    Returns True if OK, False if rejected, None on other errors."""
    global _last_ip_check_time, _last_whitelist_ok
    now = time.time()
    if now - _last_ip_check_time < _ip_check_interval and _last_whitelist_ok is not None:
        return _last_whitelist_ok
    _last_ip_check_time = now
    if not kite or not kite.kite:
        return None
    try:
        kite.kite.profile()
        _last_whitelist_ok = True
        return True
    except Exception as e:
        msg = str(e).lower()
        if "ip" in msg and ("not allowed" in msg or "whitelist" in msg):
            v6 = get_local_ipv6()
            v4 = get_public_ip_v4()
            warn = f"IP NOT WHITELISTED! Update Zerodha Developer console.\n  IPv4: {v4 or 'N/A'}\n  IPv6: {v6 or 'N/A'}"
            print(f"  ⚠ {warn}")
            _last_whitelist_ok = False
            return False
        return None

def monitor_ip_status(kite: "KiteSession"):
    """Periodic IP whitelist check. Prints status changes."""
    global _last_known_ipv4, _last_known_ipv6
    v4 = get_public_ip_v4()
    v6 = get_local_ipv6()
    changed = []
    if v4 and v4 != _last_known_ipv4:
        changed.append(f"IPv4: {_last_known_ipv4 or 'N/A'} → {v4}")
        _last_known_ipv4 = v4
    if v6 and v6 != _last_known_ipv6:
        changed.append(f"IPv6: {_last_known_ipv6 or 'N/A'} → {v6}")
        _last_known_ipv6 = v6
    if changed:
        print(f"  ⚠ IP changed:\n" + "\n".join(changed))
    result = check_ip_whitelist(kite)
    if result is False:
        print("  ✗ IP is NOT whitelisted. Fix immediately to avoid trade failures.")
    elif result is True:
        pass  # silent when OK
    heartbeat()

def main():
    telegram_logger.enable_crash_alerts()
    import argparse
    parser = argparse.ArgumentParser(description="Auto Trading Bot — NIFTY / SENSEX")
    parser.add_argument("--login", action="store_true", help="Login to Zerodha")
    parser.add_argument("--resume", action="store_true", help="Scan live Zerodha positions and monitor")
    parser.add_argument("--test-ip", action="store_true", help="Test if whitelisted IP works via dummy order")
    parser.add_argument("--strategy", choices=["ic", "sma", "cs", "mt", "bnf", "n1h"], default="ic",
                        help="Strategy: ic (Iron Condor), cs (Credit Spread), sma (SMA Crossover), bnf (Bank Nifty SMA), n1h (Nifty 1H SMA Options)")
    parser.add_argument("--lots", type=int, default=1, help="Lot multiplier (e.g. 2 = 2x lot quantity)")
    args = parser.parse_args()
    init_trade_log()

    global LOT_SIZE
    LOT_SIZE = 65 * args.lots

    get_public_ip_v4()
    get_local_ipv6()

    kite = KiteSession()

    if args.login:
        request_token = kite.login_step1()
        try:
            kite.login_step2(request_token)
            cfg = load_config()
            print(f"\nYour API Key (Static ID): {cfg.get('api_key', '')}")
            print("Enter this in Zerodha Developer App → Settings → Static ID")
        except Exception as e:
            print(f"Login failed: {e}")
        return

    if not acquire_lock(_lock_file_for(args.strategy)):
        return

    # Normal run — no prompts, just execute
    if not kite.is_authenticated():
        print("Not logged in. Run with --login first.")
        return

    cfg = load_config()
    kite.static_id = cfg.get("api_key", "")
    kite.connect()

    try:
        kite.get_option_instruments()
    except Exception as e:
        msg = str(e).lower()
        if "ip" in msg and ("not allowed" in msg or "whitelist" in msg):
            print(f"IP not whitelisted: {e}")

    if args.test_ip:
        try:
            instruments = kite.get_option_instruments()
            if not instruments:
                print("IP check: no instruments")
                return
            tsym = instruments[0]["tradingsymbol"]
            kite.kite.place_order(
                "regular", exchange="NFO", tradingsymbol=tsym,
                transaction_type="BUY", quantity=9999, price=0,
                product="NRML", order_type="MARKET", validity="DAY",
            )
        except Exception as e:
            msg = str(e).lower()
            if "margin" in msg or "funds" in msg or "insufficient" in msg:
                print("  IP: whitelisted")
            else:
                print(f"  IP: {e}")
        return

    # Strategy dispatch
    if args.strategy == "mt":
        try:
            _run_manual_trade(kite, args)
        except Exception as e:
            telegram_logger.error_alert("Manual Trade Strategy", str(e))
            raise
        return

    if args.strategy == "bnf":
        bnf = SmaCrossoverBNF(kite, lots=args.lots)
        try:
            bnf.run()
        except Exception as e:
            telegram_logger.error_alert("BNF SMA Strategy", str(e))
            raise
        return

    if args.strategy == "n1h":
        n1h = NiftySMAOptions(kite)
        try:
            n1h.run()
        except Exception as e:
            telegram_logger.error_alert("N1H Options Strategy", str(e))
            raise
        return

    if args.strategy == "sma":
        sma = SmaCrossover(kite, lots=args.lots)
        try:
            sma.run()
        except Exception as e:
            telegram_logger.error_alert("SMA Strategy", str(e))
            raise
        return

    if args.strategy == "cs":
        manager = CreditSpreadManager(kite)
        try:
            _run_credit_spread(kite, manager, args)
        except Exception as e:
            telegram_logger.error_alert("Credit Spread Strategy", str(e))
            raise
        return

    manager = IronCondorManager(kite)

    # Handle --resume: scan live Zerodha positions
    if args.resume:
        print("Scanning live Zerodha positions...")
        ic = manager.resume_from_positions()
        if not ic:
            return
        manager.position = ic
        manager.entry_credit = ic.net_credit
        print(f"Resumed {len(ic.legs)} legs, estimated credit ₹{ic.net_credit:.2f}")

    # Check if already in a position (from bot config)
    if cfg.get("position") and not args.resume:
        existing_pos = cfg["position"]
        print("Existing position found. Resuming...")
        legs = [IronCondorLeg(**l) for l in existing_pos["legs"]]
        ic = IronCondor(spot=0, expiry=existing_pos["expiry"], legs=legs,
                        net_credit=existing_pos["entry_credit"], width=0,
                        lower_breakeven=0, upper_breakeven=0)
        manager.position = ic
        manager.entry_credit = existing_pos["entry_credit"]
        # Verify fills (handles crash mid-entry)
        if not manager.verify_fills():
            # Partial fill cleaned up — remove stale config and try fresh entry
            c = load_config()
            c.pop("position", None)
            save_config(c)
            manager.position = None

    # Fresh entry (no existing/resumed position) — retry until success or market close
    if not args.resume and manager.position is None:
        if is_market_open():
            now = datetime.now()
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now < target:
                print(f"Waiting until 10:00 AM ({((target-now).total_seconds()):.0f}s)...")
                while datetime.now() < target:
                    time.sleep(30)

        while manager.position is None:
            c = load_config()
            if c.get("position") and not args.resume:
                break

            ic = manager.scan()
            while not ic:
                print("Retry 60s...")
                time.sleep(60)
                ic = manager.scan()

            is_0dte = ic.expiry == datetime.now().strftime("%Y-%m-%d")

            balance = kite.get_balance()
            required = ic.max_loss()
            if balance < required:
                print(f"  Insufficient margin ₹{balance:,.0f} < ₹{required:,.0f}. Retry 60s...")
                time.sleep(60)
                continue

            pos_entry = {
                "expiry": ic.expiry, "entry_credit": ic.net_credit,
                "legs": [asdict(l) for l in ic.legs],
            }
            save_config({**load_config(), "position": pos_entry})

            ok = manager.enter(ic)
            if not ok:
                print("  Entry failed. Cleanup retry 60s...")
                c = load_config()
                c.pop("position", None)
                save_config(c)
                manager.position = None
                time.sleep(60)
                continue

            if not is_market_open():
                print("  AMO placed, waiting market open...")
                while not is_market_open():
                    time.sleep(60)
                time.sleep(30)
                if not manager.verify_fills():
                    print("  Fills failed. Cleanup retry...")
                    c = load_config()
                    c.pop("position", None)
                    save_config(c)
                    manager.position = None
                    manager.entry_credit = 0.0
                    time.sleep(60)
                    continue
            else:
                time.sleep(5)
                if not manager.verify_fills():
                    print("  Fills failed. Cleanup retry...")
                    c = load_config()
                    c.pop("position", None)
                    save_config(c)
                    manager.position = None
                    manager.entry_credit = 0.0
                    time.sleep(60)
                    continue

            break

    target_str = "short premium ≤ ₹2" if ic.expiry == datetime.now().strftime("%Y-%m-%d") else f"₹{PROFIT_TARGET_RS} profit"
    print(f"Monitoring ({target_str}, SL @ credit)")
    try:
        while True:
            monitor_ip_status(kite)
            if not kite.ensure_auth():
                time.sleep(60)
                continue
            time.sleep(10)
            periodic_connection_test(kite)
            action = manager.monitor()
            if action in ("EXIT_PROFIT", "EXIT_LOSS", "EXIT_TIME"):
                manager.exit(action)
                c = load_config()
                c.pop("position", None)
                save_config(c)
                print("Done.")
                return
    except Exception as e:
        telegram_logger.error_alert("IC Strategy", str(e))
        raise


if __name__ == "__main__":
    main()
    try:
        input("Press Enter to close this window...")
    except Exception:
        pass
