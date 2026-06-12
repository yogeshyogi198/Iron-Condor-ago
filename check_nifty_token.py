"""Check NIFTY token lookup and 200 EMA data availability."""
import json
from kiteconnect import KiteConnect
from datetime import datetime, timedelta

cfg = json.load(open("kite_config.json"))
kite = KiteConnect(api_key=cfg["api_key"], access_token=cfg["access_token"], timeout=30)

print("=== NIFTY Token Lookup ===")
found = None
for exchange in ("NSE", "BSE"):
    for row in kite.instruments(exchange):
        tsym = row.get("tradingsymbol", "")
        if tsym in ("NIFTY 50", "NIFTY"):
            found = (exchange, row["instrument_token"], tsym)
            break
        if "NIFTY" in tsym and row.get("instrument_type", "") == "":
            found = (exchange, row["instrument_token"], tsym)
            break
    if found:
        break

if found:
    exch, token, tsym = found
    print(f"  Token: {token} ({tsym} on {exch})  ✓ CORRECT")
else:
    print("  NOT FOUND  ✗")

print()
print("=== Direct LTP Check ===")
for sym in ("NSE:NIFTY 50", "NSE:NIFTY BANK", "BSE:SENSEX"):
    try:
        q = kite.ltp(sym)
        print(f"  {sym}: {q[sym]['last_price']}  ✓")
    except Exception as e:
        print(f"  {sym}: {e}  ✗")

print()
print("=== 200 EMA Data Check (15-min candles) ===")
for hours in [55, 300, 350]:
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(hours=hours)
    candles = kite.historical_data(int(token), from_dt, to_dt, "15minute")
    enough = "✓ ENOUGH" if len(candles) >= 201 else "✗ NOT ENOUGH"
    print(f"  {hours}h lookback: {len(candles)} candles {enough}")
    if candles:
        print(f"    Range: {candles[0]['date']} → {candles[-1]['date']}")
