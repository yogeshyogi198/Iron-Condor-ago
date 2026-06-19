"""IV provider — fetches ATM option prices via REST API and calculates IV."""
import logging
from datetime import datetime

from iv_calculator import calculate_iv

logger = logging.getLogger(__name__)

INDEX_CONFIG = [
    ("nifty", "NFO", "NIFTY", 50, "NSE:NIFTY 50"),
    ("banknifty", "NFO", "BANKNIFTY", 100, "NSE:NIFTY BANK"),
    ("sensex", "BFO", "SENSEX", 100, "BSE:SENSEX"),
]


def fetch_iv_all(kite):
    """Fetch IV for all tracked indices using REST API."""
    result = {}
    try:
        resp = kite.instruments("NFO")
        now = datetime.now()

        for idx_name, exchange, name, step, sym in INDEX_CONFIG:
            try:
                spot_q = kite.quote(sym).get(sym, {})
                spot = spot_q.get("last_price", 0)
                if not spot:
                    continue

                expiry = _nearest_expiry(resp, name, now)
                if not expiry:
                    continue

                atm_strike = round(spot / step) * step

                ce_symbol = None
                for r in resp:
                    if (r.get("name") == name and r.get("instrument_type") == "CE"
                            and r.get("strike") and float(r["strike"]) == atm_strike
                            and r.get("expiry") == expiry):
                        ce_symbol = f"{exchange}:{r['tradingsymbol']}"
                        break

                if not ce_symbol:
                    continue

                ce_q = kite.quote(ce_symbol).get(ce_symbol, {})
                opt_price = ce_q.get("last_price", 0)
                if not opt_price:
                    continue

                iv = calculate_iv(opt_price, spot, atm_strike, expiry, is_call=True)
                if iv is not None and iv > 0:
                    result[idx_name] = iv
            except Exception:
                continue

    except Exception:
        pass
    return result


def _nearest_expiry(instruments, name, now):
    """Find the nearest weekly expiry for a given index."""
    expiries = set()
    for r in instruments:
        if r.get("name") == name and r.get("instrument_type") == "CE" and r.get("expiry"):
            try:
                dt = datetime.strptime(r["expiry"], "%Y-%m-%d")
                if dt > now:
                    expiries.add(r["expiry"])
            except (ValueError, TypeError):
                continue
    return min(expiries) if expiries else None
