import json
import logging
import os
import ssl
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = Path(__file__).parent / "kite_config.json"
_last_send: float = 0
_MIN_INTERVAL = 10

_ICONS = {
    "INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴",
    "CRITICAL": "💀", "TRADE": "📈", "PROFIT": "✅", "LOSS": "❌",
}


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _get_telegram_creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") or _load_config().get("telegram_bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "") or _load_config().get("telegram_chat_id", "")
    return token, chat_id


def send_telegram(message: str, level: str = "INFO") -> bool:
    global _last_send
    token, chat_id = _get_telegram_creds()
    if not token or not chat_id:
        return False
    now = time.time()
    if now - _last_send < _MIN_INTERVAL:
        return False
    _last_send = now
    icon = _ICONS.get(level, "📌")
    time_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    text = f"{icon} *{level}* | `{time_str}`\n```\n{message}\n```"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    ctx = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("  ⚠ Telegram: Invalid bot token (401 Unauthorized)")
        return False
    except Exception:
        return False


# ── Trade helpers ──────────────────────────────────────────

def trade_alert(symbol: str, action: str, price: float, qty: int):
    send_telegram(f"{symbol} | {action.upper()} {qty} @ ₹{price:.2f}", level="TRADE")


def pnl_alert(pnl: float, trade_id: str = ""):
    level = "PROFIT" if pnl >= 0 else "LOSS"
    msg = f"P&L: ₹{pnl:+.2f}"
    if trade_id:
        msg = f"[{trade_id}] {msg}"
    send_telegram(msg, level=level)


def strategy_entry_alert(strategy: str, legs: list):
    lines = [f"🚀 *{strategy} ENTERED*"]
    for leg in legs:
        act = leg.get("action", "")
        strike = leg.get("strike", 0)
        otype = leg.get("option_type", "")
        prem = leg.get("premium", 0)
        tsym = leg.get("tradingsymbol", "")
        lines.append(f"  {act} {tsym} @ ₹{prem:.2f}")
    net = sum(l.get("premium", 0) for l in legs if l.get("action") == "SELL") - \
          sum(l.get("premium", 0) for l in legs if l.get("action") == "BUY")
    lines.append(f"  Net Credit: ₹{net:.2f}")
    send_telegram("\n".join(lines), level="TRADE")


def strategy_exit_alert(strategy: str, reason: str, pnl: float):
    icon = "✅" if pnl >= 0 else "❌"
    send_telegram(f"{icon} *{strategy} EXIT* ({reason})\nP&L: ₹{pnl:+.2f}",
                  level="PROFIT" if pnl >= 0 else "LOSS")


def error_alert(context: str, error_msg: str):
    send_telegram(f"🔴 *ERROR* [{context}]\n{error_msg}", level="ERROR")


# ── Logging handler ───────────────────────────────────────

class TelegramHandler(logging.Handler):
    def __init__(self, min_level=logging.WARNING):
        super().__init__(min_level)

    def emit(self, record: logging.LogRecord):
        level = record.levelname
        msg = self.format(record)
        send_telegram(msg, level=level)


def setup_logger(name: str = "AlgoBot", min_telegram_level=logging.WARNING):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    ))

    tg = TelegramHandler(min_level=min_telegram_level)
    tg.setFormatter(logging.Formatter("%(name)s | %(message)s"))

    logger.addHandler(console)
    logger.addHandler(tg)
    return logger


# ── Crash alerts ──────────────────────────────────────────

def _handle_exception(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    send_telegram(f"💀 BOT CRASHED:\n{tb_str}", level="CRITICAL")
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def enable_crash_alerts():
    sys.excepthook = _handle_exception
    send_telegram("🤖 AlgoBot started successfully!", level="INFO")


def send_daily_summary(data: dict):
    """Send a formatted daily summary to Telegram.
    
    data = {
        "date": "2026-06-12",
        "indices": {
            "nifty": {"open": 23500, "high": 23650, "low": 23480, "close": 23622, "change_pct": 0.52},
            "sensex": {...},
            "banknifty": {...}
        },
        "option_oi": {
            "nifty": {"expiry": "2026-06-16", "spot": 23622, "atm": {...}, "near": [...], "ce": {...}, "pe": {...}, "pcr": 1.15},
            "banknifty": {...},
            "sensex": {...}
        },
        "trades": {"total": 1, "pnl": -3198.0, "charges": 198.5, "net": -3396.5}
    }
    """
    lines = [f"📊 DAILY SUMMARY | {data.get('date', 'N/A')}"]
    lines.append("")

    for label, key in [("NIFTY", "nifty"), ("SENSEX", "sensex"), ("BANK NIFTY", "banknifty")]:
        idx = data.get("indices", {}).get(key)
        if idx:
            o = idx.get("open", 0)
            h = idx.get("high", 0)
            l = idx.get("low", 0)
            c = idx.get("close", 0)
            chg = idx.get("change", 0)
            chg_pct = idx.get("change_pct", 0)
            rng = h - l
            arrow = "🔺" if chg >= 0 else "🔻"
            lines.append(f"{arrow} {label}")
            lines.append(f"  Open: {o:,.2f}  High: {h:,.2f}  Low: {l:,.2f}  Close: {c:,.2f}")
            lines.append(f"  Day Range: {rng:,.2f}  |  Gain: {chg:+,.2f}  ({chg_pct:+.2f}%)")

    all_oi = data.get("option_oi", {})
    for label, key in [("NIFTY", "nifty"), ("BANK NIFTY", "banknifty"), ("SENSEX", "sensex")]:
        oi = all_oi.get(key)
        if oi and oi.get("near"):
            expiry = oi.get("expiry", "N/A")
            spot = oi.get("spot", 0)
            lines.append("")
            lines.append(f"OPTION OI — {label} ({expiry} @ {spot:,.0f})")
            atm = oi.get("atm", {})
            if atm:
                lines.append(f"  ATM ({atm['strike']:,})  CE: {atm['ce_oi']:,}  PE: {atm['pe_oi']:,}")
            lines.append("  Strikes:")
            for s in oi["near"]:
                marker = "  ← ATM" if s.get("is_atm") else ""
                lines.append(f"    {s['strike']:,}  CE: {s['ce_oi']:,}  PE: {s['pe_oi']:,}{marker}")
            ce = oi.get("ce", {})
            pe = oi.get("pe", {})
            if ce:
                lines.append(f"  Max CE: {ce['strike']:,} @ {ce['oi']:,}")
            if pe:
                lines.append(f"  Max PE: {pe['strike']:,} @ {pe['oi']:,}")
            pcr = oi.get("pcr", 0)
            if pcr:
                lines.append(f"  PCR: {pcr:.2f}")

    trades = data.get("trades")
    if trades and trades.get("total", 0) > 0:
        lines.append("")
        lines.append("💰 TODAY'S TRADES")
        lines.append(f"  Trades: {trades['total']}")
        pnl = trades.get("pnl", 0)
        icon = "✅" if pnl >= 0 else "❌"
        lines.append(f"  P&L: {icon} ₹{pnl:+,.2f}")
        lines.append(f"  Charges: ₹{trades.get('charges', 0):,.2f}")
        net = trades.get("net", 0)
        net_icon = "✅" if net >= 0 else "❌"
        lines.append(f"  Net: {net_icon} ₹{net:+,.2f}")

    send_telegram("\n".join(lines), level="INFO")


# ── Decorator ─────────────────────────────────────────────

def alert_on_error(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            send_telegram(f"Function: {func.__name__}\nError: {e}\n\n{tb}", level="ERROR")
            raise
    return wrapper
