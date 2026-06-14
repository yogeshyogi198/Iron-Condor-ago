"""
Ratio Strategy — NIFTYBEES vs GOLDBEES (Turtle System 1)
========================================================
Run:  python ratio_strategy.py

Checks daily at 15:31 IST for ratio breakouts.
Entry/exit based on 20-day/10-day Turtle rules.
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from kiteconnect import KiteConnect

import telegram_logger

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
STATE_FILE = BOT_DIR / "ratio_state.json"
HEARTBEAT_FILE = BOT_DIR / ".bot_heartbeat.txt"

NIFTYBEES_SYMBOL = "NIFTYBEES"
GOLDBEES_SYMBOL = "GOLDBEES"
LOOKBACK_DAYS = 60
ENTRY_WINDOW = 20
EXIT_WINDOW = 10
CHECK_HOUR = 15
CHECK_MINUTE = 31
LOOP_SLEEP = 60


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {"holding": None, "entry_date": "", "entry_price": 0, "quantity": 0}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_token_map(kite: KiteConnect) -> dict[str, int]:
    instruments = kite.instruments("NSE")
    result = {}
    for r in instruments:
        itype = r.get("instrument_type")
        tsym = r.get("tradingsymbol")
        if tsym and itype in ("", None, "EQ"):
            result[tsym] = int(r["instrument_token"])
    return result


def _fetch_daily(kite: KiteConnect, token: int,
                 days: int = LOOKBACK_DAYS) -> list[dict]:
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    try:
        return kite.historical_data(token, from_dt, to_dt, "day")
    except Exception:
        return []


def _align_prices(data_niftybees: list[dict],
                   data_goldbees: list[dict]) -> tuple[list, list]:
    dates_n = {c["date"][:10] for c in data_niftybees}
    dates_g = {c["date"][:10] for c in data_goldbees}
    common = sorted(dates_n & dates_g)

    n_map = {c["date"][:10]: c["close"] for c in data_niftybees}
    g_map = {c["date"][:10]: c["close"] for c in data_goldbees}

    close_n, close_g = [], []
    for d in common:
        close_n.append(n_map[d])
        close_g.append(g_map[d])
    return close_n, close_g


def _get_ltp(kite: KiteConnect, symbol: str) -> float:
    try:
        q = kite.ltp(f"NSE:{symbol}")
        return float(q[f"NSE:{symbol}"]["last_price"])
    except Exception:
        return 0.0


def _get_holdings_qty(kite: KiteConnect, symbol: str) -> int:
    try:
        holdings = kite.holdings()
        for h in holdings:
            if h.get("tradingsymbol") == symbol:
                return int(h.get("quantity", 0))
    except Exception:
        pass
    return 0


def _get_available_margin(kite: KiteConnect) -> float:
    try:
        margins = kite.margins("equity")
        return float(margins.get("available", {}).get("cash", 0))
    except Exception:
        return 0.0


def _place_cnc_order(kite: KiteConnect, symbol: str, qty: int,
                     transaction_type: str) -> bool:
    try:
        order_id = kite.place_order(
            variety="regular",
            exchange="NSE",
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=qty,
            product="CNC",
            order_type="MARKET",
            validity="DAY",
        )
        print(f"  Order: {transaction_type} {qty} {symbol} (ID: {order_id})")
        return True
    except Exception as e:
        print(f"  Order failed: {e}")
        telegram_logger.error_alert("RatioStrategy", f"Order failed: {e}")
        return False


def _get_turtle_signals(close_n: list, close_g: list):
    """Return (current_ratio, entry_high, entry_low, exit_high, exit_low)."""
    ratios = [n / g for n, g in zip(close_n, close_g)]
    current = ratios[-1]

    entry = ratios[-ENTRY_WINDOW - 1:-1]
    entry_high = max(entry) if entry else current
    entry_low = min(entry) if entry else current

    exit_ = ratios[-EXIT_WINDOW - 1:-1]
    exit_high = max(exit_) if exit_ else current
    exit_low = min(exit_) if exit_ else current

    return current, entry_high, entry_low, exit_high, exit_low


def _send_report(current_ratio: float, entry_high: float, entry_low: float,
                 exit_high: float, exit_low: float, state: dict, mode: str,
                 signal: str = "", action_detail: str = ""):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    holding = state.get("holding") or "None"
    qty = state.get("quantity", 0)
    entry_p = state.get("entry_price", 0)
    lines = [
        f"\U0001f504 RATIO STRATEGY",
        f"\U0001f4ca Ratio: {current_ratio:.4f}",
        f"\U0001f4c8 Holding: {holding}",
    ]
    if qty:
        lines.append(f"\U0001f4b0 Qty: {qty} | Entry: \u20b9{entry_p:.2f}")
    lines.append(f"20d High: {entry_high:.4f} | Low: {entry_low:.4f}")
    lines.append(f"10d High: {exit_high:.4f} | Low: {exit_low:.4f}")
    if signal:
        lines.append(f"\u2757 Signal: {signal}")
    if action_detail:
        lines.append(f"\U0001f501 {action_detail}")
    lines.append(f"\u23f0 Time: {now_str}")
    lines.append(f"\U0001f4c4 Mode: {mode}")
    telegram_logger.send_telegram("\n".join(lines), level="TRADE" if signal else "INFO")


def main():
    cfg = _load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    paper_trade = cfg.get("ratio_paper_trade", True)

    if not api_key or not access_token:
        print("Not authenticated. Run iron_condor_algo.py --login first.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=30)
    state = _load_state()
    mode_str = "PAPER" if paper_trade else "LIVE"

    print(f"Ratio Strategy \u2014 Turtle System 1 ({mode_str} mode)")
    print("=" * 50)
    print(f"Current holding: {state['holding'] or 'None'}")

    token_map = {}

    def check_and_act(send_report_only: bool = False) -> bool:
        nonlocal state, token_map
        now = datetime.now()
        HEARTBEAT_FILE.write_text(now.strftime("%Y-%m-%d %H:%M:%S"))

        if now.weekday() >= 5 and not send_report_only:
            return False

        print(f"\n[{now.strftime('%H:%M:%S')}] Checking ratio...")

        if not token_map:
            token_map = _get_token_map(kite)
            print(f"  Token map: {len(token_map)} symbols")

        n_token = token_map.get(NIFTYBEES_SYMBOL)
        g_token = token_map.get(GOLDBEES_SYMBOL)
        if not n_token or not g_token:
            print("  NIFTYBEES/GOLDBEES tokens not found, rebuilding map...")
            token_map = _get_token_map(kite)
            n_token = token_map.get(NIFTYBEES_SYMBOL)
            g_token = token_map.get(GOLDBEES_SYMBOL)
            if not n_token or not g_token:
                print("  Still not found. Retrying later.")
                return False

        ndata = _fetch_daily(kite, n_token)
        gdata = _fetch_daily(kite, g_token)

        if len(ndata) < LOOKBACK_DAYS or len(gdata) < LOOKBACK_DAYS:
            print(f"  Insufficient data: NIFTYBEES={len(ndata)}, "
                  f"GOLDBEES={len(gdata)}")
            return False

        close_n, close_g = _align_prices(ndata, gdata)
        if len(close_n) < ENTRY_WINDOW + 1:
            print(f"  Aligned data too short: {len(close_n)} days")
            return False

        n_ltp = _get_ltp(kite, NIFTYBEES_SYMBOL)
        g_ltp = _get_ltp(kite, GOLDBEES_SYMBOL)
        if not n_ltp or not g_ltp:
            print("  Could not fetch LTP")
            return False

        current_ratio = n_ltp / g_ltp
        _, entry_high, entry_low, exit_high, exit_low = \
            _get_turtle_signals(close_n, close_g)

        print(f"  Ratio: {current_ratio:.4f}")
        print(f"  20d High: {entry_high:.4f}  Low: {entry_low:.4f}")
        print(f"  10d High: {exit_high:.4f}  Low: {exit_low:.4f}")

        if send_report_only:
            _send_report(current_ratio, entry_high, entry_low,
                         exit_high, exit_low, state, mode_str)
            return True

        # ── Exit check ──
        if state["holding"]:
            do_exit = False
            exit_reason = ""
            if state["holding"] == NIFTYBEES_SYMBOL and current_ratio < exit_low:
                do_exit = True
                exit_reason = "ratio < 10-day lowest"
            elif state["holding"] == GOLDBEES_SYMBOL and current_ratio > exit_high:
                do_exit = True
                exit_reason = "ratio > 10-day highest"

            if do_exit:
                sell_symbol = state["holding"]
                qty = state.get("quantity", 0)
                print(f"  EXIT {sell_symbol} ({exit_reason})")

                if not paper_trade and qty > 0:
                    _place_cnc_order(kite, sell_symbol, qty, "SELL")

                ltp_sold = n_ltp if sell_symbol == NIFTYBEES_SYMBOL else g_ltp
                pnl = round((ltp_sold - state["entry_price"]) * qty, 2)

                _send_report(current_ratio, entry_high, entry_low,
                             exit_high, exit_low, state, mode_str,
                             signal=f"EXIT {sell_symbol}",
                             action_detail=f"Reason: {exit_reason} | "
                             f"P&L: \u20b9{pnl:+,.2f}")

                state = {"holding": None, "entry_date": "",
                         "entry_price": 0, "quantity": 0}
                _save_state(state)
                print("  Position closed. Waiting for next entry...\n")
                return True

        # ── Entry check ──
        if not state["holding"]:
            signal = None
            if current_ratio > entry_high:
                signal = "BUY_NIFTYBEES"
            elif current_ratio < entry_low:
                signal = "BUY_GOLDBEES"

            if signal:
                buy_symbol = (NIFTYBEES_SYMBOL if signal == "BUY_NIFTYBEES"
                              else GOLDBEES_SYMBOL)
                sell_symbol = (GOLDBEES_SYMBOL if signal == "BUY_NIFTYBEES"
                               else NIFTYBEES_SYMBOL)

                margin = _get_available_margin(kite)
                buy_ltp = n_ltp if buy_symbol == NIFTYBEES_SYMBOL else g_ltp
                qty = int(margin / buy_ltp) if buy_ltp > 0 else 0

                if qty < 1:
                    print(f"  Insufficient margin: \u20b9{margin:.2f}, "
                          f"LTP: \u20b9{buy_ltp:.2f}")
                    qty = 0

                if not paper_trade and qty > 0:
                    sell_qty = _get_holdings_qty(kite, sell_symbol)
                    if sell_qty > 0:
                        _place_cnc_order(kite, sell_symbol, sell_qty, "SELL")
                        time.sleep(2)
                    _place_cnc_order(kite, buy_symbol, qty, "BUY")
                elif paper_trade:
                    print(f"  [PAPER] Would sell {sell_symbol}, "
                          f"buy {buy_symbol} x {qty}")

                state = {
                    "holding": buy_symbol,
                    "entry_date": now.strftime("%Y-%m-%d"),
                    "entry_price": buy_ltp,
                    "quantity": qty,
                }
                _save_state(state)

                action_str = f"Sell {sell_symbol} \u2192 Buy {buy_symbol}"
                _send_report(current_ratio, entry_high, entry_low,
                             exit_high, exit_low, state, mode_str,
                             signal=signal, action_detail=action_str)
                print(f"  ENTERED: {buy_symbol} x {qty} @ \u20b9{buy_ltp:.2f}")
                return True
            else:
                print(f"  No entry signal. Ratio within {entry_low:.4f} "
                      f"- {entry_high:.4f} range.")
        else:
            print(f"  Holding: {state['holding']} x {state['quantity']} "
                  f"(@ \u20b9{state['entry_price']:.2f})")
        return False

    try:
        check_and_act(send_report_only=True)
    except Exception as e:
        telegram_logger.error_alert("RatioStrategy", str(e))

    while True:
        try:
            now = datetime.now()
            HEARTBEAT_FILE.write_text(now.strftime("%Y-%m-%d %H:%M:%S"))

            if now.weekday() >= 5:
                time.sleep(LOOP_SLEEP)
                continue

            if now.hour < CHECK_HOUR or (now.hour == CHECK_HOUR
                                         and now.minute < CHECK_MINUTE):
                time.sleep(LOOP_SLEEP)
                continue

            check_and_act()

            time.sleep(LOOP_SLEEP)

        except KeyboardInterrupt:
            print("\n  Graceful exit.")
            telegram_logger.send_telegram(
                "\U0001f504 RATIO STRATEGY stopped (manual)", level="INFO")
            break
        except Exception as e:
            tb = traceback.format_exc()
            print(f"  Error: {tb}")
            telegram_logger.error_alert("RatioStrategy", str(e))
            time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Exiting...")
    except Exception as e:
        telegram_logger.error_alert("RatioStrategy", traceback.format_exc())
        raise
