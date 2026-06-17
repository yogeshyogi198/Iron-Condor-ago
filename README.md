# Iron Condor Algo — Multi-Strategy Auto Trading Bot

Automated trading bot for Zerodha Kite, supporting Iron Condor, Credit Spread, SMA Crossover, Manual Trade, and Swing strategies.

## Prerequisites

- Python 3.10+
- A [Zerodha](https://zerodha.com) trading account
- A [Kite Connect](https://developers.kite.trade) app (API Key & Secret)

## Setup

```bash
# 1. Install dependencies
pip install kiteconnect flask pandas numpy requests python-dotenv

# 2. One-time login (generates kite_config.json)
python iron_condor_algo.py --login
# Follow prompts: enter API Key, API Secret, then authorize in browser

# 3. Optional — Telegram alerts
# Create .env file with:
#   TELEGRAM_BOT_TOKEN=your_bot_token
#   TELEGRAM_CHAT_ID=your_chat_id
#   DASHBOARD_PASSWORD=your_password

# 4. Clean test data for fresh launch
python cleanup_live_launch.py
```

## Usage

| Command | Description |
|---------|-------------|
| `python iron_condor_algo.py` | Iron Condor scan → enter → monitor |
| `python iron_condor_algo.py --strategy ic` | Iron Condor (NIFTY, default) |
| `python iron_condor_algo.py --strategy cs` | Credit Spread (NIFTY) |
| `python iron_condor_algo.py --strategy sma` | SMA Crossover (SENSEX) |
| `python iron_condor_algo.py --strategy bnf` | Bank Nifty 2H SMA(60) |
| `python iron_condor_algo.py --strategy n1h` | Nifty 1H SMA Options |
| `python iron_condor_algo.py --strategy mt` | Manual Trade (interactive SL) |
| `python iron_condor_algo.py --lots 2` | 2x lot quantity |
| `python iron_condor_algo.py --kill` | Emergency kill switch |
| `python iron_condor_algo.py --resume` | Resume from live positions |
| `python iron_condor_algo.py --login` | One-time login only |

### Standalone Strategies

```bash
python swing_scanner.py      # Weekly equity swing scan (Nifty 500)
python swing_rebalancer.py   # Daily rebalancer alerts
python ratio_strategy.py     # NIFTYBEES/GOLDBEES ratio
```

### Dashboard

```bash
python dashboard.py                      # Start on port 5000
python dashboard.py --port 5001          # Custom port
python dashboard.py --host 127.0.0.1    # Localhost only
```

Open `http://localhost:5000` in browser.

## Config

Credentials and positions are stored in `kite_config.json`. Environment variables go in `.env`.

## Risk Management

- Global max daily loss: ₹10,000 (automatic kill switch on breach)
- Per-strategy lock files prevent duplicate runs
- Emergency kill: `python iron_condor_algo.py --kill` or dashboard button

## Project Structure

| File | Purpose |
|------|---------|
| `iron_condor_algo.py` | Main bot — all strategy engines |
| `dashboard.py` | Flask web dashboard |
| `telegram_logger.py` | Telegram alert system |
| `market_data.py` | Market data fetcher |
| `swing_scanner.py` | Equity swing scanner |
| `swing_rebalancer.py` | Daily rebalancer |
| `swing_indicators.py` | Technical indicators |
| `swing_config.py` | Swing strategy config |
| `ratio_strategy.py` | NIFTYBEES/GOLDBEES ratio |
| `cleanup_live_launch.py` | Reset for fresh launch |
