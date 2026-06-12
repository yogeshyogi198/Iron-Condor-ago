# Iron Condor Algo Bot

Automated trading bot for NIFTY/SENSEX options on Zerodha.

## Strategies

| Flag | Strategy | Symbol |
|------|----------|--------|
| `--strategy ic` (default) | Iron Condor | NIFTY |
| `--strategy cs` | Credit Spread (ADX+EMA), fallback to IC | NIFTY |
| `--strategy sma` | SMA(60) Crossover | SENSEX |

## Setup

1. Clone the repo
2. Copy `.env.example` to `.env` and fill in your secrets
3. Configure your Zerodha API credentials in `kite_config.json`:

```json
{
  "api_key": "your_api_key",
  "api_secret": "your_api_secret",
  "access_token": "your_access_token"
}
```

4. Install dependencies:

```bash
pip install -r requirements.txt
```

## One-Time Login

```bash
python iron_condor_algo.py --login
```

## Run Bot

```bash
python iron_condor_algo.py --strategy ic
```

## Dashboard

```bash
# Local
python dashboard.py

# Production (Digital Ocean / Linux)
gunicorn dashboard:app --bind 0.0.0.0:8000 --workers 2
```

Set `FLASK_DEBUG=1` env var to enable debug mode.

## Deploy to Digital Ocean

1. Push this repo to GitHub
2. Create App Platform app → select GitHub repo
3. Set **Run Command**: `gunicorn dashboard:app --bind 0.0.0.0:8000 --workers 2`
4. Add these **Environment Variables** in App Platform settings:
   - `DASHBOARD_PASSWORD` — your dashboard password
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token
   - `TELEGRAM_CHAT_ID` — your Telegram chat ID
   - `FLASK_DEBUG` — leave empty (or `0`)
5. Deploy

> Bot (`iron_condor_algo.py`) runs inside the dashboard's "Start" button — no separate process needed.

## Notes

- One API key = one bot. Don't run multiple instances.
- Login again if token expires (rare, Kite tokens last months).
- Trade log: `trade_log.csv` in the same folder.
