# Iron Condor Algo Bot

Automated trading bot for NIFTY/SENSEX options on Zerodha.

## Strategies

| Flag | Strategy | Symbol |
|------|----------|--------|
| `--strategy ic` (default) | Iron Condor | NIFTY |
| `--strategy cs` | Credit Spread (ADX+EMA), fallback to IC | NIFTY |
| `--strategy sma` | SMA(60) Crossover | SENSEX |

## One-Time Login

Run once to authorize (token saved for months):

```
cd "C:\Users\yogesha M\Desktop\iron_condor_algo"
python iron_condor_algo.py --login
```

## Run

```
python iron_condor_algo.py --strategy sma
```

## Schedule (24/7 background)

1. Open **Task Scheduler** (Win+R -> `taskschd.msc`)
2. **Create Task**:
   - General: Name=`IronCondorBot`, check "Run whether user is logged on"
   - Triggers: "At startup" or "Daily" at 9:00 AM
   - Actions: Start `python` with arg `iron_condor_algo.py --strategy sma`
   - Start in: `C:\Users\yogesha M\Desktop\iron_condor_algo`
   - Conditions: Uncheck battery/AC power options
3. Click OK (enter Windows password if prompted)

Bot auto-recovers on reboot. Stop via Task Scheduler -> Disable.

## Notes

- One API key = one bot. Don't run multiple instances.
- Login again if token expires (rare, Kite tokens last months).
- Trade log: `trade_log.csv` in the same folder.
