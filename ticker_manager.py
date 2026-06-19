"""IV provider — fetches ATM option prices via REST API and calculates IV + IVP."""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from iv_calculator import calculate_iv

logger = logging.getLogger(__name__)

INDEX_CONFIG = [
    ("nifty", "NFO", "NIFTY", 50, "NSE:NIFTY 50"),
    ("banknifty", "NFO", "BANKNIFTY", 100, "NSE:NIFTY BANK"),
    ("sensex", "BFO", "SENSEX", 100, "BSE:SENSEX"),
]

IV_HISTORY_FILE = Path(__file__).parent / "iv_history.json"


def _load_iv_history() -> dict:
    if IV_HISTORY_FILE.exists():
        try:
            return json.loads(IV_HISTORY_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_iv_history(data: dict):
    try:
        IV_HISTORY_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("_save_iv_history: %s", e)


def compute_ivp(current_iv: float, history_values: list[float]) -> float:
    if not history_values:
        return 50.0
    count_below = sum(1 for v in history_values if v < current_iv)
    return round(count_below / len(history_values) * 100, 1)


def record_iv(iv_data: dict[str, float]):
    today = datetime.now().strftime("%Y-%m-%d")
    hist = _load_iv_history()
    changed = False
    for idx, iv in iv_data.items():
        records = hist.setdefault(idx, {"dates": [], "values": []})
        if not records["dates"] or records["dates"][-1] != today:
            records["dates"].append(today)
            records["values"].append(round(iv, 2))
            changed = True
            if len(records["values"]) > 252:
                records["dates"] = records["dates"][-252:]
                records["values"] = records["values"][-252:]
    if changed:
        _save_iv_history(hist)


def get_ivp(idx_name: str, current_iv: float) -> float:
    hist = _load_iv_history()
    records = hist.get(idx_name, {})
    return compute_ivp(current_iv, records.get("values", []))


def ivp_label(percent: float) -> str:
    if percent < 30:
        return "Low"
    if percent > 70:
        return "High"
    return "Medium"


def fetch_iv_all(kite, instruments_by_exchange=None):
    """Fetch IV for all tracked indices using REST API.

    If *instruments_by_exchange* is provided (dict of exchange -> instrument list),
    it will be used instead of calling ``kite.instruments()`` again.

    Returns dict keyed by result-key (lowercase index name), e.g. ``{"nifty": 14.32}``.
    """
    result = {}
    try:
        now = datetime.now()

        by_exchange = {}
        for idx_name, exchange, name, step, sym in INDEX_CONFIG:
            by_exchange.setdefault(exchange, []).append((idx_name, name, step, sym))

        if instruments_by_exchange is None:
            instruments_by_exchange = {}

        for exchange, indices in by_exchange.items():
            resp = instruments_by_exchange.get(exchange)
            if resp is None:
                try:
                    resp = kite.instruments(exchange)
                except Exception as e:
                    logger.warning("fetch_iv_all: kite.instruments(%s) failed — %s", exchange, e)
                    continue
            for idx_name, name, step, sym in indices:
                try:
                    spot_q = kite.quote(sym).get(sym, {})
                    spot = spot_q.get("last_price", 0)
                    if not spot:
                        logger.warning("fetch_iv_all: no spot price for %s", sym)
                        continue

                    expiry = _nearest_expiry(resp, name, now)
                    if not expiry:
                        logger.warning("fetch_iv_all: no expiry found for %s", name)
                        continue

                    atm_strike = round(spot / step) * step

                    ce_symbol = pe_symbol = None
                    for r in resp:
                        if r.get("name") != name or r.get("strike") is None or float(r["strike"]) != atm_strike or str(r.get("expiry")) != expiry:
                            continue
                        if r.get("instrument_type") == "CE":
                            ce_symbol = f"{exchange}:{r['tradingsymbol']}"
                        elif r.get("instrument_type") == "PE":
                            pe_symbol = f"{exchange}:{r['tradingsymbol']}"

                    quote_keys = [s for s in (ce_symbol, pe_symbol) if s]
                    if not quote_keys:
                        logger.warning("fetch_iv_all: no CE/PE symbol at ATM strike %s for %s", atm_strike, name)
                        continue

                    qs = kite.quote(quote_keys)

                    def _mid_price(sym):
                        q = qs.get(sym, {})
                        bid = q.get("depth", {}).get("buy", [{}])[0].get("price", 0) if q.get("depth") else 0
                        ask = q.get("depth", {}).get("sell", [{}])[0].get("price", 0) if q.get("depth") else 0
                        ltp = q.get("last_price", 0)
                        if bid and ask:
                            return (bid + ask) / 2
                        return ltp

                    ivs = []
                    if ce_symbol:
                        p = _mid_price(ce_symbol)
                        if p:
                            iv = calculate_iv(p, spot, atm_strike, expiry, is_call=True)
                            if iv and iv > 0:
                                ivs.append(iv)
                    if pe_symbol:
                        p = _mid_price(pe_symbol)
                        if p:
                            iv = calculate_iv(p, spot, atm_strike, expiry, is_call=False)
                            if iv and iv > 0:
                                ivs.append(iv)

                    if ivs:
                        result[idx_name] = round(sum(ivs) / len(ivs), 2)
                    else:
                        logger.warning("fetch_iv_all: no valid IV calculated for %s", idx_name)
                except Exception as e:
                    logger.warning("fetch_iv_all: error for %s — %s", idx_name, e)

    except Exception as e:
        logger.warning("fetch_iv_all: unexpected error — %s", e)
    return result


def _nearest_expiry(instruments, name, now):
    """Find the nearest weekly expiry for a given index."""
    expiries = {}
    matched_name = False
    for r in instruments:
        if r.get("name") == name and r.get("instrument_type") == "CE":
            matched_name = True
            raw = r.get("expiry")
            if not raw:
                continue
            if isinstance(raw, datetime):
                dt = raw
            elif isinstance(raw, str):
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%d")
                except (ValueError, TypeError):
                    continue
            else:
                try:
                    dt = datetime.combine(raw, datetime.min.time()) if hasattr(raw, "timetuple") else datetime(raw.year, raw.month, raw.day)
                except Exception:
                    continue
            if dt > now:
                expiries[dt] = r["tradingsymbol"]
    if not matched_name:
        logger.warning("_nearest_expiry(%s): NO instruments matched name + CE", name)
    if not expiries:
        return None
    nearest_dt = min(expiries.keys())
    return nearest_dt.strftime("%Y-%m-%d")
