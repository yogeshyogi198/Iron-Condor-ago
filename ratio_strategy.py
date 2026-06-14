"""
Ratio Strategy — NIFTYBEES vs GOLDBEES (Turtle System 1 + 2)
=============================================================
Run:  python ratio_strategy.py

Checks daily at 15:31 IST for ratio breakouts.
- System 1 (20/10): 20-day entry, 10-day exit
- System 2 (55/20): 55-day entry, 20-day exit
Entry when either system signals. Exit uses the system that entered.
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
LOOKBACK_DAYS = 90
MIN_DATA_POINTS = 30
ENTRY_WINDOW_1 = 20
EXIT_WINDOW_1 = 10
ENTRY_WINDOW_2 = 55
EXIT_WINDOW_2 = 20
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
    return {"holding": None, "entry_date": "", "entry_price": 0,
            "quantity": 0, "entry_system": None}


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
    def _date_key(d):
        dt = d.get("date")
        if isinstance(dt, str):
            return dt[:10]
        return dt.strftime("%Y-%m-%d")

    dates_n = {_date_key(c) for c in data_niftybees}
    dates_g = {_date_key(c) for c in data_goldbees}
    common = sorted(dates_n & dates_g)

    n_map = {_date_key(c): c["close"] for c in data_niftybees}
    g_map = {_date_key(c): c["close"] for c in data_goldbees}

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


def _channel(ratios: list, window: int):
    """Return (high, low) for the given lookback window."""
    if len(ratios) < window + 1:
        return ratios[-1], ratios[-1]
    chunk = ratios[-window - 1:-1]
    return max(chunk), min(chunk)


def _send_report(current_ratio: float, s1: dict, s2: dict,
                 state: dict, mode: str, signal: str = "",
                 action_detail: str = ""):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    holding = state.get("holding") or "None"
    qty = state.get("quantity", 0)
    entry_p = state.get("entry_price", 0)
    sys_label = state.get("entry_system", "")
    lines = [
        "\U0001f504 RATIO STRATEGY",
        f"\U0001f4ca Ratio: {current_ratio:.4f}",
        f"\U0001f4c8 Holding: {holding}",
    ]
    if qty:
        parts = [f"\U0001f4b0 Qty: {qty}"]
        if entry_p:
            parts.append(f"Entry: \u20b9{entry_p:.2f}")
        if sys_label:
            parts.append(f"System: {sys_label}")
        lines.append(" | ".join(parts))
    lines.append(f"S1 20d H:{s1['entry_high']:.4f} L:{s1['entry_low']:.4f}")
    lines.append(f"S1 10d H:{s1['exit_high']:.4f} L:{s1['exit_low']:.4f}")
    lines.append(f"S2 55d H:{s2['entry_high']:.4f} L:{s2['entry_low']:.4f}")
    lines.append(f"S2 20d H:{s2['exit_high']:.4f} L:{s2['exit_low']:.4f}")
    if signal:
        lines.append(f"\u2757 Signal: {signal}")
    if action_detail:
        lines.append(f"\U0001f501 {action_detail}")
    lines.append(f"\u23f0 Time: {now_str}")
    lines.append(f"\U0001f4c4 Mode: {mode}")
    result = telegram_logger.send_telegram("\n".join(lines),
                                           level="TRADE" if signal else "INFO")
    print(f"  Telegram send: {result}")


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

    # ── Sync state with actual Zerodha holdings ──
    try:
        actual_holdings = kite.holdings()
        print(f"  Holdings API returned {len(actual_holdings)} items")
        for h in actual_holdings:
            print(f"    {h.get('tradingsymbol')} x {h.get('quantity')}")
        n_qty = sum(h.get("quantity", 0) + h.get("t1_quantity", 0)
                    for h in actual_holdings
                    if h.get("tradingsymbol") == NIFTYBEES_SYMBOL)
        g_qty = sum(h.get("quantity", 0) + h.get("t1_quantity", 0)
                    for h in actual_holdings
                    if h.get("tradingsymbol") == GOLDBEES_SYMBOL)
        print(f"  NIFTYBEES qty: {n_qty}, GOLDBEES qty: {g_qty}")
        actual_holding = None
        if n_qty > 0 and g_qty > 0:
            print("  WARNING: Holding both NIFTYBEES and GOLDBEES. "
                  "Clearing state.")
            state = {"holding": None, "entry_date": "", "entry_price": 0,
                     "quantity": 0, "entry_system": None}
        elif n_qty > 0:
            actual_holding = NIFTYBEES_SYMBOL
        elif g_qty > 0:
            actual_holding = GOLDBEES_SYMBOL

        if actual_holding and state.get("holding") != actual_holding:
            print(f"  Synced state: holdings show {actual_holding} "
                  f"(state was {state.get('holding') or 'None'})")
            state = {"holding": actual_holding, "entry_date": "",
                     "entry_price": 0, "quantity": n_qty or g_qty,
                     "entry_system": None}
        elif not actual_holding and state.get("holding"):
            print(f"  Synced state: no holdings found (state had "
                  f"{state['holding']}). Clearing.")
            state = {"holding": None, "entry_date": "", "entry_price": 0,
                     "quantity": 0, "entry_system": None}
        _save_state(state)
    except Exception as e:
        print(f"  Could not sync holdings: {e}")

    print("Ratio Strategy \u2014 Turtle System 1 (20/10) + System 2 (55/20)")
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

        if len(ndata) < MIN_DATA_POINTS or len(gdata) < MIN_DATA_POINTS:
            print(f"  Insufficient data: NIFTYBEES={len(ndata)}, "
                  f"GOLDBEES={len(gdata)}")
            return False

        close_n, close_g = _align_prices(ndata, gdata)
        if len(close_n) < max(ENTRY_WINDOW_2, ENTRY_WINDOW_1) + 1:
            print(f"  Aligned data too short: {len(close_n)} days")
            return False

        n_ltp = _get_ltp(kite, NIFTYBEES_SYMBOL)
        g_ltp = _get_ltp(kite, GOLDBEES_SYMBOL)
        if not n_ltp or not g_ltp:
            print("  Could not fetch LTP")
            return False

        current_ratio = n_ltp / g_ltp
        ratios = [n / g for n, g in zip(close_n, close_g)]

        # Compute both systems
        s1 = {}
        s1["entry_high"], s1["entry_low"] = _channel(ratios, ENTRY_WINDOW_1)
        s1["exit_high"], s1["exit_low"] = _channel(ratios, EXIT_WINDOW_1)

        s2 = {}
        s2["entry_high"], s2["entry_low"] = _channel(ratios, ENTRY_WINDOW_2)
        s2["exit_high"], s2["exit_low"] = _channel(ratios, EXIT_WINDOW_2)

        print(f"  Ratio: {current_ratio:.4f}")
        print(f"  S1 20d H:{s1['entry_high']:.4f} L:{s1['entry_low']:.4f}")
        print(f"  S1 10d H:{s1['exit_high']:.4f} L:{s1['exit_low']:.4f}")
        print(f"  S2 55d H:{s2['entry_high']:.4f} L:{s2['entry_low']:.4f}")
        print(f"  S2 20d H:{s2['exit_high']:.4f} L:{s2['exit_low']:.4f}")

        if send_report_only:
            _send_report(current_ratio, s1, s2, state, mode_str)
            return True

        # ── Exit check (use the system that entered) ──
        if state["holding"]:
            sys_name = state.get("entry_system", "s1")
            exit_channel = s1 if sys_name == "s1" else s2
            do_exit = False
            exit_reason = ""
            if state["holding"] == NIFTYBEES_SYMBOL and \
               current_ratio < exit_channel["exit_low"]:
                do_exit = True
                w = EXIT_WINDOW_1 if sys_name == "s1" else EXIT_WINDOW_2
                exit_reason = f"ratio < {w}-day lowest"
            elif state["holding"] == GOLDBEES_SYMBOL and \
                 current_ratio > exit_channel["exit_high"]:
                do_exit = True
                w = EXIT_WINDOW_1 if sys_name == "s1" else EXIT_WINDOW_2
                exit_reason = f"ratio > {w}-day highest"

            if do_exit:
                sell_symbol = state["holding"]
                qty = state.get("quantity", 0)
                print(f"  EXIT {sell_symbol} ({exit_reason})")

                if not paper_trade and qty > 0:
                    _place_cnc_order(kite, sell_symbol, qty, "SELL")

                ltp_sold = n_ltp if sell_symbol == NIFTYBEES_SYMBOL else g_ltp
                pnl = round((ltp_sold - state["entry_price"]) * qty, 2)

                _send_report(current_ratio, s1, s2, state, mode_str,
                             signal=f"EXIT {sell_symbol} ({sys_name})",
                             action_detail=f"{exit_reason} | "
                             f"P&L: \u20b9{pnl:+,.2f}")

                state = {"holding": None, "entry_date": "",
                         "entry_price": 0, "quantity": 0,
                         "entry_system": None}
                _save_state(state)
                print("  Position closed. Waiting for next entry...\n")
                return True

        # ── Entry check (either system) ──
        if not state["holding"]:
            signal = None
            entry_system = None
            buy_symbol = None
            sell_symbol = None

            if current_ratio > s1["entry_high"]:
                signal = "BUY_NIFTYBEES"
                entry_system = "s1"
            elif current_ratio < s1["entry_low"]:
                signal = "BUY_GOLDBEES"
                entry_system = "s1"

            if not signal:
                if current_ratio > s2["entry_high"]:
                    signal = "BUY_NIFTYBEES"
                    entry_system = "s2"
                elif current_ratio < s2["entry_low"]:
                    signal = "BUY_GOLDBEES"
                    entry_system = "s2"

            if signal:
                buy_symbol = (NIFTYBEES_SYMBOL
                              if signal == "BUY_NIFTYBEES"
                              else GOLDBEES_SYMBOL)
                sell_symbol = (GOLDBEES_SYMBOL
                               if signal == "BUY_NIFTYBEES"
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
                    "entry_system": entry_system,
                }
                _save_state(state)

                w = f"S1(20)" if entry_system == "s1" else f"S2(55)"
                action_str = f"Sell {sell_symbol} \u2192 Buy {buy_symbol} [{w}]"
                _send_report(current_ratio, s1, s2, state, mode_str,
                             signal=signal, action_detail=action_str)
                print(f"  ENTERED: {buy_symbol} x {qty} @ "
                      f"\u20b9{buy_ltp:.2f} [{w}]")
                return True
            else:
                print("  No signal. Ratio within both channels.")
        else:
            sys_name = state.get("entry_system", "s1")
            print(f"  Holding: {state['holding']} x {state['quantity']} "
                  f"(@ \u20b9{state['entry_price']:.2f}) [{sys_name}]")
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
