"""
Market Data Fetcher - Nifty, Sensex, Bank Nifty
Spot, OI, Change, Sentiment
"""
import json, os, sys
from datetime import datetime

from kiteconnect import KiteConnect

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "kite_config.json")

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

def main():
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if not api_key or not access_token:
        print("No API key or token found. Run --login first.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=30)

    symbols = [
        "NSE:NIFTY 50",
        "BSE:SENSEX",
        "NSE:NIFTY BANK",
    ]

    now = datetime.now()
    print()
    print("=" * 60)
    print(f"  MARKET DATA - {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    quotes = {}
    for sym in symbols:
        try:
            q = kite.quote(sym)[sym]
            quotes[sym] = q
        except Exception as e:
            quotes[sym] = {"error": str(e)}

    labels = {
        "NSE:NIFTY 50": "NIFTY",
        "BSE:SENSEX": "SENSEX",
        "NSE:NIFTY BANK": "BANK NIFTY",
    }

    for sym, label in labels.items():
        q = quotes.get(sym, {})
        if "error" in q:
            print(f"\n  {label}: ERROR - {q['error']}")
            continue

        ohlc = q.get("ohlc", {})
        ltp = q.get("last_price", 0)
        net_change = q.get("net_change", 0)
        prev_close = ohlc.get("close", 0)
        change_pct = (net_change / prev_close * 100) if prev_close else 0

        print(f"\n  {'-' * 50}")
        print(f"  {label}")
        print(f"  {'-' * 50}")
        print(f"    Spot:            {ltp:>10.2f}")
        print(f"    Open:            {ohlc.get('open', 0):>10.2f}")
        print(f"    High:            {ohlc.get('high', 0):>10.2f}")
        print(f"    Low:             {ohlc.get('low', 0):>10.2f}")
        print(f"    Prev Close:      {prev_close:>10.2f}")
        print(f"    Change:          {net_change:>+10.2f}  ({change_pct:+.2f}%)")

    # --- SENTIMENT via PCR (NIFTY options) ---
    print(f"\n  {'-' * 50}")
    print(f"  SENTIMENT (NIFTY Options)")
    print(f"  {'-' * 50}")

    try:
        resp = kite.instruments("NFO")
        nifty_opts = [
            r for r in resp
            if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")
        ]
        if not nifty_opts:
            print("    No NIFTY options found")
        else:
            tsyms = [r["tradingsymbol"] for r in nifty_opts]
            ce_oi = 0
            pe_oi = 0
            ce_oi_change = 0
            pe_oi_change = 0
            for i in range(0, len(tsyms), 500):
                batch = tsyms[i:i+500]
                keys = [f"NFO:{s}" for s in batch]
                batch_quotes = kite.quote(keys)
                for k, v in batch_quotes.items():
                    if v.get("oi") is None:
                        continue
                    sym = k.replace("NFO:", "")
                    strikes = [r for r in nifty_opts if r["tradingsymbol"] == sym]
                    if not strikes:
                        continue
                    itype = strikes[0]["instrument_type"]
                    if itype == "CE":
                        ce_oi += v.get("oi", 0) or 0
                        ce_oi_change += v.get("change_in_oi", 0) or 0
                    elif itype == "PE":
                        pe_oi += v.get("oi", 0) or 0
                        pe_oi_change += v.get("change_in_oi", 0) or 0

            print(f"    CE Total OI:     {ce_oi:>15,}")
            print(f"    PE Total OI:     {pe_oi:>15,}")
            pcr = pe_oi / ce_oi if ce_oi else 0
            print(f"    PCR (PE/CE):     {pcr:>10.4f}")
            print(f"    CE OI Chg:       {ce_oi_change:>+15,}")
            print(f"    PE OI Chg:       {pe_oi_change:>+15,}")

            if pcr > 1.3:
                sentiment = "STRONG BULLISH"
            elif pcr > 1.15:
                sentiment = "BULLISH"
            elif pcr > 1.0:
                sentiment = "WEAK BULLISH"
            elif pcr > 0.85:
                sentiment = "WEAK BEARISH"
            elif pcr > 0.7:
                sentiment = "BEARISH"
            else:
                sentiment = "STRONG BEARISH"
            print(f"    Sentiment:       {sentiment}")
    except Exception as e:
        print(f"    Error: {e}")

    print(f"\n{'=' * 60}")
    print()

if __name__ == "__main__":
    main()
