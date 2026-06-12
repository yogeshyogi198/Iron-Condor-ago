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
    "pnl", "max_profit_target", "stop_loss", "exit_reason",
]

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
        if pid and _is_pid_running(int(pid)):
            print(f"Another instance (PID {pid}) already running. Exiting.")
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
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE

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
    def __init__(self, kite: KiteSession):
        self.kite = kite
        self.trades_today = 0
        self.session1_done = False
        self.session2_done = False
        self.trade = None  # {side, entry_ts, entry_price, sl, target_level, qty, tsym, strike, oid}

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

    def _is_trade_active(self) -> bool:
        """Check if trade still has open positions."""
        if not self.trade:
            return False
        try:
            positions = self.kite.kite.positions()["day"]
            for p in positions:
                if p.get("tradingsymbol") == self.trade["tsym"] and p.get("quantity", 0) != 0:
                    return True
        except Exception:
            pass
        return False

    def _in_session1(self, now: datetime) -> bool:
        return (now.hour == 9 and now.minute >= 30) or (now.hour == 10) or (now.hour == 11 and now.minute <= 30)

    def _in_session2(self, now: datetime) -> bool:
        return (now.hour == 13) or (now.hour == 14 and now.minute <= 30)

    def _manage_trade(self):
        if not self.trade:
            return
        spot = self._get_sensex_spot()
        if spot is None:
            return
        side = self.trade["side"]
        entry_price = self.trade["entry_price"]
        sl = self.trade["sl"]
        target_level = self.trade["target_level"]
        points = abs(spot - entry_price)

        is_buy = (side == "CE")
        if is_buy and spot <= sl:
            print(f"  SL hit @ {spot:.2f}, closing")
            self._exit_trade()
        elif not is_buy and spot >= sl:
            print(f"  SL hit @ {spot:.2f}, closing")
            self._exit_trade()
        else:
            risk = abs(entry_price - self.trade["entry_sl"])
            rr = points / risk if risk > 0 else 0
            if rr >= target_level + 1:
                new_sl = entry_price + (target_level + 1) * risk if is_buy else entry_price - (target_level + 1) * risk
                if (is_buy and new_sl > self.trade["sl"]) or (not is_buy and new_sl < self.trade["sl"]):
                    self.trade["sl"] = new_sl
                    self.trade["target_level"] = target_level + 1
                    print(f"  Trail SL to {new_sl:.2f} (RR {target_level+1}:1)")

    def _exit_trade(self):
        if not self.trade:
            return
        tsym = self.trade["tsym"]
        qty = self.trade["qty"]
        exit_spot = self._get_sensex_spot() or 0
        try:
            ltp = self.kite.kite.ltp(f"{SENSEX_EXCHANGE}:{tsym}")
            exit_prem = ltp.get(f"{SENSEX_EXCHANGE}:{tsym}", {}).get("last_price", 0)
        except Exception:
            exit_prem = 0
        try:
            self.kite.place_market(tsym, "BUY" if self.trade["side"] == "PE" else "SELL", qty, exchange=SENSEX_EXCHANGE)
            print(f"  Closed {tsym}")
        except Exception as e:
            print(f"  Close error: {e}")
        entry_prem = self.trade.get("entry_prem", 0)
        pnl = (entry_prem - exit_prem) * qty if self.trade["side"] == "CE" else (exit_prem - entry_prem) * qty
        append_trade_log({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": f"SMA_{self.trade['side']}",
            "expiry": self.trade.get("expiry", ""),
            "entry_time": self.trade.get("entry_ts", ""),
            "exit_time": datetime.now().isoformat(),
            "entry_spot": round(self.trade.get("entry_price", 0), 2),
            "exit_spot": round(exit_spot, 2),
            "entry_credit": round(entry_prem, 2),
            "exit_value": round(exit_prem, 2),
            "pnl": round(pnl, 2),
            "max_profit_target": 0,
            "stop_loss": 0,
            "exit_reason": "SL_HIT" if self.trade.get("sl") else "CLOSED",
        })
        self.trade = None

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
                time.sleep(10)
                continue

            sma = self._calc_sma(candles, SMA_PERIOD)
            if sma is None:
                time.sleep(10)
                continue

            current_price = candles[-1]["close"]
            prev_price = candles[-2]["close"] if len(candles) > 1 else current_price
            prev_sma = self._calc_sma(candles[:-1], SMA_PERIOD) if len(candles) > SMA_PERIOD else sma

            if prev_sma is None:
                time.sleep(10)
                continue

            # Detect crossover
            price_above = current_price > sma
            prev_above = prev_price > prev_sma
            spot = self._get_sensex_spot()
            if spot is None:
                time.sleep(10)
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
            time.sleep(10)

    def _enter_trade(self, opt: dict, side: str, entry_candle: dict) -> bool:
        side_str = "BUY"
        entry_price = self._get_sensex_spot()
        if entry_price is None:
            return False

        tsym = opt["tsym"]
        qty = opt["qty"]

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

        self.trade = {
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
        self.trades_today += 1
        return True

    def run(self):
        """Main SMA crossover loop."""
        print(f"SMA Crossover — {SMA_TIMEFRAME} SMA{SMA_PERIOD}")

        while True:
            monitor_ip_status(self.kite)
            periodic_connection_test(self.kite)
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # If past Session 1 window (11:30), mark it done so Session 2 isn't locked out
            if not self.session1_done and (now.hour > 11 or (now.hour == 11 and now.minute > 30)):
                self.session1_done = True

            # Session 1: 9:30-11:30
            if not self.session1_done and self._in_session1(now) and self.trades_today < 1:
                if self._wait_for_crossover("S1"):
                    self.session1_done = True

            if not self.session2_done and self._in_session2(now) and self.session1_done and self.trades_today < 2:
                if self._wait_for_crossover("S2"):
                    self.session2_done = True

            # Manage existing trade
            if self.trade:
                self._manage_trade()

            if not self._is_trade_active() and self.trade:
                self.trade = None

            # If past Session 2 window and nothing to do, wait for next day
            if self.session1_done and not self.trade and (now.hour > 14 or (now.hour == 14 and now.minute > 30)):
                next_day = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
                wait = (next_day - now).total_seconds()
                time.sleep(min(wait, 3600))
                self.trades_today = 0
                self.session1_done = False
                self.session2_done = False

            if self.trades_today >= 2:
                next_day = (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
                wait = (next_day - now).total_seconds()
                time.sleep(min(wait, 3600))
                self.trades_today = 0
                self.session1_done = False
                self.session2_done = False

            time.sleep(60)


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
    print(f"Monitoring every 60s. Target: {target_str}, SL at credit.")
    while True:
        monitor_ip_status(kite)
        if not kite.ensure_auth():
            time.sleep(60)
            continue
        time.sleep(60)
        periodic_connection_test(kite)
        action = manager.monitor()
        if action in ("EXIT_PROFIT", "EXIT_LOSS", "EXIT_TIME", "EXIT_REQUESTED"):
            manager.exit(action)
            c = load_config()
            c.pop("cs_position", None)
            save_config(c)
            print("Done.")
            return


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
    parser.add_argument("--strategy", choices=["ic", "sma", "cs"], default="ic",
                        help="Strategy: ic (Iron Condor), cs (Credit Spread), or sma (SMA Crossover)")
    args = parser.parse_args()
    init_trade_log()

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
    if args.strategy == "sma":
        sma = SmaCrossover(kite)
        sma.run()
        return

    if args.strategy == "cs":
        manager = CreditSpreadManager(kite)
        _run_credit_spread(kite, manager, args)
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
    while True:
        monitor_ip_status(kite)
        if not kite.ensure_auth():
            time.sleep(60)
            continue
        time.sleep(60)
        periodic_connection_test(kite)
        action = manager.monitor()
        if action in ("EXIT_PROFIT", "EXIT_LOSS", "EXIT_TIME"):
            manager.exit(action)
            c = load_config()
            c.pop("position", None)
            save_config(c)
            print("Done.")
            return


if __name__ == "__main__":
    main()
    try:
        input("Press Enter to close this window...")
    except Exception:
        pass
