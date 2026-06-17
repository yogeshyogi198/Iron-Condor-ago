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

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    from pytz import timezone
    IST = timezone("Asia/Kolkata")

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


def _ist_now() -> str:
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p")


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
    time_str = _ist_now()
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


# ═══════════════════════════════════════════════════════════
#  1. ENTRY ALERT — enriched with direction, PCR, IST time
# ═══════════════════════════════════════════════════════════

def trade_alert(symbol: str, action: str, price: float, qty: int):
    send_telegram(f"{symbol} | {action.upper()} {qty} @ ₹{price:.2f}", level="TRADE")


def strategy_entry_alert(
    strategy: str,
    legs: list,
    pcr_classification: str = "N/A",
):
    instrument = legs[0].get("tradingsymbol", "NIFTY") if legs else "NIFTY"
    instrument = instrument.replace("NIFTY", "").replace("SENSEX", "").replace("BANKNIFTY", "").strip() or \
                next((l.get("tradingsymbol", "") for l in legs if l.get("tradingsymbol")), "NIFTY")

    sell_legs = [l for l in legs if l.get("action") == "SELL"]
    buy_legs = [l for l in legs if l.get("action") == "BUY"]

    if sell_legs and buy_legs:
        if all(l.get("option_type") == "CE" for l in legs):
            direction = "Bull Call Spread"
        elif all(l.get("option_type") == "PE" for l in legs):
            direction = "Bear Put Spread"
        elif any(l.get("option_type") == "CE" for l in sell_legs) and any(l.get("option_type") == "PE" for l in sell_legs):
            direction = "Iron Condor — Dual Side"
        else:
            side = sell_legs[0].get("option_type", "?")
            d = "Bullish" if side == "CE" else "Bearish"
            direction = f"{d} Credit Spread"
    elif len(legs) == 1:
        act = legs[0].get("action", "")
        otype = legs[0].get("option_type", "")
        d = "Bullish" if otype == "CE" else "Bearish"
        direction = f"{act} {otype} — {d} Crossover"
    else:
        direction = "Multi-Leg Strategy"

    lots = 0
    fill_prices = []
    tsyms = []
    for leg in legs:
        tsyms.append(leg.get("tradingsymbol", ""))
        prem = leg.get("premium", 0)
        if prem > 0:
            fill_prices.append(prem)

    net_credit = sum(l.get("premium", 0) for l in sell_legs) - sum(l.get("premium", 0) for l in buy_legs)

    exec_price_str = " | ".join(f"₹{p:.2f}" for p in fill_prices[:4]) if fill_prices else "LTP-based"

    # Estimate lots from first leg
    from_tsym = tsyms[0] if tsyms else ""
    if "NIFTY" in from_tsym:
        lots_str = "1 (65 qty)"
    elif "SENSEX" in from_tsym:
        lots_str = "1 (15-20 qty)"
    elif "BANKNIFTY" in from_tsym:
        lots_str = "1 (15 qty)"
    else:
        lots_str = "1"

    time_str = _ist_now()

    msg = (
        f"🚀 [ENTRY] — {strategy} ({instrument})\n"
        f"{'─' * 40}\n"
        f"Direction/Logic:  {direction}\n"
        f"Execution Price:  {exec_price_str}\n"
        f"Lots/Quantity:    {lots_str}\n"
        f"Net Credit:       ₹{net_credit:+.2f}\n"
        f"Market PCR:       {pcr_classification}\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}"
    )
    send_telegram(msg, level="TRADE")


# ═══════════════════════════════════════════════════════════
#  2. EXIT / P&L ALERT — financial breakdown with charges
# ═══════════════════════════════════════════════════════════

def pnl_alert(pnl: float, trade_id: str = "", gross_pnl: float = None, charges: float = None):
    time_str = _ist_now()
    icon = "✅" if pnl >= 0 else "❌"
    gross = gross_pnl if gross_pnl is not None else pnl
    chg = charges if charges is not None else 0.0
    net = gross - chg

    msg = (
        f"🏁 [P&L UPDATE] — {trade_id or 'Trade'}\n"
        f"{'─' * 40}\n"
        f"Gross P&L:   ₹{gross:+,.2f}\n"
        f"Charges:     ₹{chg:,.2f}\n"
        f"NET P&L:     {icon} ₹{net:+,.2f}\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}"
    )
    send_telegram(msg, level="PROFIT" if net >= 0 else "LOSS")


def strategy_exit_alert(
    strategy: str,
    reason: str,
    pnl: float,
    gross_pnl: float = None,
    charges: float = None,
    exit_price: float = None,
):
    time_str = _ist_now()
    gross = gross_pnl if gross_pnl is not None else pnl
    chg = charges if charges is not None else 0.0
    net = gross - chg
    icon = "✅" if net >= 0 else "❌"

    reason_icon_map = {
        "TARGET": "✅ TARGET HIT",
        "EXIT_PROFIT": "✅ TARGET HIT",
        "EXIT_LOSS": "❌ STOP LOSS HIT",
        "SL_HIT": "❌ STOP LOSS HIT",
        "EXIT_TIME": "⏰ TIME EXIT (3:15 PM)",
        "GLOBAL_MTM_HALT": "🚨 EMERGENCY KILL SWITCH",
        "EXIT_REQUESTED": "🛑 MANUAL EXIT",
        "PARTIAL_FILL": "⚠️ PARTIAL FILL — SQUARED OFF",
        "TARGET_1_2": "🎯 1:2 TARGET ACHIEVED",
    }
    reason_label = reason_icon_map.get(reason, reason)

    exit_line = f"Exit Price:   ₹{exit_price:.2f}\n" if exit_price else ""

    msg = (
        f"🏁 [EXIT] — {strategy}\n"
        f"{'─' * 40}\n"
        f"Exit Reason:  {reason_label}\n"
        f"{exit_line}"
        f"Gross P&L:    ₹{gross:+,.2f}\n"
        f"Charges:      ₹{chg:,.2f}\n"
        f"NET P&L:      {icon} ₹{net:+,.2f}\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}"
    )
    send_telegram(msg, level="PROFIT" if net >= 0 else "LOSS")


# ═══════════════════════════════════════════════════════════
#  3. HOURLY SUMMARY — account snapshot
# ═══════════════════════════════════════════════════════════

def hourly_report_alert(
    active_trades: int = 0,
    realized_pnl: float = 0.0,
    unrealized_mtm: float = 0.0,
    total_charges: float = 0.0,
    mtm_total: float = 0.0,
    mtm_limit: float = 10000.0,
):
    time_str = _ist_now()
    total_net = realized_pnl + unrealized_mtm - total_charges
    pnl_icon = "🟢" if total_net >= 0 else "🔴"

    mtm_remaining = mtm_limit + mtm_total
    mtm_pct = abs(mtm_total) / mtm_limit * 100 if mtm_limit else 0
    if mtm_total < -mtm_limit * 0.8:
        mtm_status = f"🚨 CRITICAL — {mtm_pct:.0f}% of limit used"
    elif mtm_total < -mtm_limit * 0.5:
        mtm_status = f"⚠️ WARNING — {mtm_pct:.0f}% of limit used"
    elif mtm_total < 0:
        mtm_status = f"⚠️ {mtm_pct:.0f}% of limit used"
    else:
        mtm_status = f"✅ Safe (₹{mtm_remaining:+,.2f} remaining)"

    msg = (
        f"📊 [HOURLY REPORT] — BOT STATUS\n"
        f"{'─' * 40}\n"
        f"Status:        🟢 LIVE & SCANNING\n"
        f"Active Trades: {active_trades}\n"
        f"{'─' * 40}\n"
        f"Realized P&L:      ₹{realized_pnl:+,.2f}\n"
        f"Unrealized MTM:    ₹{unrealized_mtm:+,.2f}\n"
        f"Est. Charges:      ₹{total_charges:+,.2f}\n"
        f"{'─' * 40}\n"
        f"Total Net P&L: {pnl_icon} ₹{total_net:+,.2f}\n"
        f"{'─' * 40}\n"
        f"Global MTM Limit:\n"
        f"  {mtm_status}\n"
        f"  (Limit: -₹{mtm_limit:,.0f} | Current: ₹{mtm_total:+,.2f})\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}"
    )
    send_telegram(msg, level="INFO")


# ═══════════════════════════════════════════════════════════
#  4. CRITICAL / ERROR ALERTS — high-priority emergency
# ═══════════════════════════════════════════════════════════

def error_alert(context: str, error_msg: str, action_taken: str = ""):
    time_str = _ist_now()
    action_line = f"\nAction Taken: {action_taken}" if action_taken else ""
    msg = (
        f"🔴 [ERROR] — {context}\n"
        f"{'─' * 40}\n"
        f"⚠️  {error_msg}{action_line}\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}"
    )
    send_telegram(msg, level="ERROR")


def critical_alert(
    context: str,
    error_msg: str,
    action_taken: str = "",
    is_mtm_breach: bool = False,
):
    time_str = _ist_now()
    banner = "🚨 GLOBAL MTM LIMIT BREACHED 🚨" if is_mtm_breach else "💀 CRITICAL SYSTEM FAILURE 💀"
    action_line = f"\n🛠 Action Taken: {action_taken}" if action_taken else ""
    footer = "System halted — manual intervention required." if is_mtm_breach else "Immediate attention required."

    msg = (
        f"{'⚠️' * 12}\n"
        f"  {banner}\n"
        f"{'⚠️' * 12}\n"
        f"{'─' * 40}\n"
        f"Component:     {context}\n"
        f"Error:         🟥 {error_msg}{action_line}\n"
        f"{'─' * 40}\n"
        f"🕐 {time_str}\n"
        f"{'─' * 40}\n"
        f"{footer}"
    )
    send_telegram(msg, level="CRITICAL")


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
