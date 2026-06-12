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
