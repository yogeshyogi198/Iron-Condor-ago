"""
Iron Condor Bot — Web Dashboard (Multi-Strategy)
=================================================
Run:  python dashboard.py
Then: Open http://localhost:5000 in browser

Features:
  - Auto-capture Zerodha OAuth token (set Redirect URL to http://localhost:5000/callback)
  - Manual token entry fallback
  - Run IC, CS, and SMA strategies simultaneously
  - Per-strategy Start/Stop with live console output
"""

import json
import os
import signal
import secrets
import subprocess
import sys
import threading
import time
from functools import wraps
from pathlib import Path

from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template_string, request, session
from kiteconnect import KiteConnect

load_dotenv()

_DASH_API_CALLS: list[float] = []

def _dash_rate_limit():
    now = time.time()
    _DASH_API_CALLS[:] = [t for t in _DASH_API_CALLS if now - t < 1]
    if len(_DASH_API_CALLS) >= 3:
        time.sleep(0.35)
    _DASH_API_CALLS.append(time.time())

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
HEARTBEAT_FILE = BOT_DIR / ".bot_heartbeat.txt"
TRADE_LOG = BOT_DIR / "trade_log.csv"

STRATEGIES = ["ic", "cs", "sma", "mt", "bnf", "n1h", "sw", "sr", "ratio", "sc_nifty", "sc_bnf", "sc_sensex"]
LOT_SIZE = 65  # NIFTY lot size for charges estimation

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

PASSWORD_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard · Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:'Inter',-apple-system,'Segoe UI',sans-serif;
    min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#080c14; color:#e2e8f0;
    position:relative; overflow:hidden;
  }
  body::before {
    content:''; position:fixed; inset:0; z-index:0;
    background:
      radial-gradient(circle at 20% 30%, rgba(91,141,239,0.12), transparent 60%),
      radial-gradient(circle at 80% 70%, rgba(22,192,152,0.09), transparent 60%);
  }
  .card {
    position:relative; z-index:1;
    background:rgba(16,20,30,0.8); backdrop-filter:blur(24px);
    border:1px solid rgba(255,255,255,0.08);
    border-radius:24px; padding:48px 40px;
    width:420px; max-width:92vw;
    box-shadow:0 24px 80px rgba(0,0,0,0.6);
    transition:transform .3s ease;
  }
  .logo {
    width:56px; height:56px; border-radius:16px;
    background:linear-gradient(135deg,#5B8DEF,#16C098);
    display:flex; align-items:center; justify-content:center;
    margin:0 auto 20px; font-size:28px;
    box-shadow:0 8px 28px rgba(91,141,239,0.25);
  }
  h1 {
    font-family:'Sora',sans-serif; font-weight:700;
    font-size:1.6rem; text-align:center;
    background:linear-gradient(135deg,#e2e8f0,#94a3b8);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
    background-clip:text; margin-bottom:6px;
  }
  .sub { text-align:center; color:#64748b; font-size:0.9rem; margin-bottom:28px; }
  input {
    width:100%; padding:16px 20px; border-radius:14px;
    border:1px solid rgba(255,255,255,0.08);
    background:rgba(0,0,0,0.35); color:#e2e8f0;
    font-size:1rem; font-family:'Inter',sans-serif;
    margin-bottom:16px; transition:border-color .25s ease, box-shadow .25s ease;
    outline:none;
  }
  input:focus { border-color:#5B8DEF; box-shadow:0 0 0 3px rgba(91,141,239,0.15); }
  input::placeholder { color:#475569; }
  .btn {
    width:100%; padding:16px; border:none; border-radius:14px;
    font-size:1rem; font-weight:600; font-family:'Inter',sans-serif;
    cursor:pointer;
    background:linear-gradient(135deg,#5B8DEF,#16C098);
    color:#fff; transition:transform .2s ease, box-shadow .2s ease;
    box-shadow:0 4px 16px rgba(91,141,239,0.25);
  }
  .btn:hover { transform:translateY(-1px); box-shadow:0 6px 24px rgba(91,141,239,0.35); }
  .btn:active { transform:translateY(0); }
  .error {
    text-align:center; margin-top:16px; font-size:0.9rem;
    color:#f87171; background:rgba(248,113,113,0.1);
    padding:10px 16px; border-radius:10px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="logo">&#9889;</div>
  <h1>Trading Bot</h1>
  <div class="sub">Enter your password to continue</div>
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit" class="btn">Unlock Dashboard</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div>
</body>
</html>"""


def get_dashboard_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "") or load_config().get("dashboard_password", "")


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        pw = get_dashboard_password()
        if pw and not session.get("authenticated"):
            return render_template_string(PASSWORD_PAGE, error="")
        return f(*args, **kwargs)
    return wrapped
bot_processes: dict[str, subprocess.Popen | None] = {s: None for s in STRATEGIES}
bot_outputs: dict[str, list[str]] = {s: [] for s in STRATEGIES}
bot_output_locks: dict[str, threading.Lock] = {s: threading.Lock() for s in STRATEGIES}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _reader_thread(strategy: str, proc: subprocess.Popen):
    try:
        for line in iter(proc.stdout.readline, ""):
            with bot_output_locks[strategy]:
                bot_outputs[strategy].append(line.rstrip())
                if len(bot_outputs[strategy]) > 200:
                    bot_outputs[strategy] = bot_outputs[strategy][-200:]
    except Exception:
        pass


STRATEGY_LABELS = {
    "ic": "Iron Condor (NIFTY)",
    "cs": "Credit Spread (NIFTY)",
    "sma": "SMA Crossover (SENSEX)",
    "mt": "Manual Trade (Trail SL)",
    "sw": "Swing Scanner (Weekly)",
    "sr": "Swing Rebalancer (Daily)",
    "bnf": "Bank Nifty 2H SMA(60)",
    "n1h": "Nifty 1H SMA Options",
    "ratio": "NIFTYBEES/GOLDBEES Ratio",
    "sc_nifty": "3-Min Scalper (NIFTY)",
    "sc_bnf": "3-Min Scalper (BANKNIFTY)",
    "sc_sensex": "3-Min Scalper (SENSEX)",
}

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot · Dashboard</title>
<script>(function(){try{var t=localStorage.getItem('dashTheme')||'terminal';document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','terminal');}})();</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root, html[data-theme="terminal"] {
    --bg:#080c17;
    --mesh1: radial-gradient(circle at 15% 10%, rgba(91,141,239,0.18), transparent 60%);
    --mesh2: radial-gradient(circle at 85% 90%, rgba(22,192,152,0.12), transparent 60%);
    --card-bg: rgba(16,20,30,0.72);
    --card-border: rgba(255,255,255,0.06);
    --log-bg: rgba(0,0,0,0.40);
    --border: rgba(255,255,255,0.08);
    --text:#E8EDF5; --text-dim:#8892A8;
    --accent:#5B8DEF; --accent-2:#16C098; --accent-rgb:91,141,239;
    --green:#16C098; --green-rgb:22,192,152;
    --red:#FF5C72; --red-rgb:255,92,114;
    --amber:#F5A623; --amber-rgb:245,166,35;
    --deep-red:#E0344C;
    --shadow:0 12px 40px rgba(0,0,0,0.55);
    --radius:18px;
  }
  html[data-theme="dusk"] {
    --bg:#1a110e;
    --mesh1: radial-gradient(circle at 15% 10%, rgba(255,138,101,0.20), transparent 60%);
    --mesh2: radial-gradient(circle at 85% 90%, rgba(245,193,116,0.14), transparent 60%);
    --card-bg: rgba(38,28,22,0.70);
    --card-border: rgba(255,219,189,0.08);
    --log-bg: rgba(0,0,0,0.35);
    --border: rgba(255,219,189,0.10);
    --text:#F2E6DA; --text-dim:#B09A86;
    --accent:#FF8A65; --accent-2:#F5C174; --accent-rgb:255,138,101;
    --green:#4CC9A0; --green-rgb:76,201,160;
    --red:#FF6F61; --red-rgb:255,111,97;
    --amber:#F5C174; --amber-rgb:245,193,116;
    --deep-red:#E2574C;
    --shadow:0 12px 40px rgba(0,0,0,0.50);
    --radius:20px;
  }
  html[data-theme="daylight"] {
    --bg:#F7F4EE;
    --mesh1: radial-gradient(circle at 15% 10%, rgba(47,111,237,0.10), transparent 60%);
    --mesh2: radial-gradient(circle at 85% 90%, rgba(15,163,125,0.08), transparent 60%);
    --card-bg: rgba(255,255,255,0.78);
    --card-border: rgba(31,36,45,0.06);
    --log-bg: rgba(31,36,45,0.04);
    --border: rgba(31,36,45,0.08);
    --text:#1E2330; --text-dim:#6B7280;
    --accent:#2F6FED; --accent-2:#0FA37D; --accent-rgb:47,111,237;
    --green:#0FA37D; --green-rgb:15,163,125;
    --red:#E0344C; --red-rgb:224,52,78;
    --amber:#B97A1A; --amber-rgb:185,122,26;
    --deep-red:#C22B3E;
    --shadow:0 12px 36px rgba(31,36,45,0.10);
    --radius:18px;
  }

  * { margin:0; padding:0; box-sizing:border-box; }
  html { font-size:16px; }

  body {
    font-family:'Inter',-apple-system,'Segoe UI',sans-serif;
    background-color:var(--bg); color:var(--text);
    padding:32px; min-height:100vh; position:relative;
    transition:background-color .4s ease, color .4s ease;
  }
  body::before { content:''; position:fixed; inset:0; z-index:-1; background:var(--mesh1),var(--mesh2); }

  .container { max-width:1440px; margin:0 auto; }

  /* ── HERO ── */
  .hero {
    display:flex; justify-content:space-between; align-items:center;
    gap:20px; flex-wrap:wrap; margin-bottom:28px; animation:fadeDown .5s ease;
  }
  @keyframes fadeDown { 0%{opacity:0;transform:translateY(-12px)} 100%{opacity:1;transform:translateY(0)} }
  .hero-brand { display:flex; align-items:center; gap:16px; }
  .hero-icon {
    width:48px; height:48px; border-radius:14px;
    background:linear-gradient(135deg,var(--accent),var(--accent-2));
    display:flex; align-items:center; justify-content:center;
    font-size:24px; color:#fff;
    box-shadow:0 6px 20px rgba(var(--accent-rgb),0.30);
    flex-shrink:0;
  }
  .hero-title h1 {
    font-family:'Sora',sans-serif; font-weight:700; font-size:1.75rem;
    letter-spacing:-0.025em;
    background:linear-gradient(135deg,var(--text),var(--text-dim));
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
    background-clip:text;
  }
  .hero-sub {
    font-size:0.75rem; color:var(--text-dim); text-transform:uppercase;
    letter-spacing:0.1em; margin-top:1px;
  }
  .hero-actions { display:flex; align-items:center; gap:12px; }
  .hero-clock { font-size:0.85rem; color:var(--text-dim); font-variant-numeric:tabular-nums; }

  .theme-switch {
    display:flex; gap:6px; padding:5px;
    border-radius:999px; background:var(--log-bg);
    border:1px solid var(--card-border); backdrop-filter:blur(12px);
  }
  .swatch {
    width:28px; height:28px; border-radius:50%;
    border:2px solid transparent; cursor:pointer; padding:0;
    transition:transform .2s ease, border-color .2s ease;
  }
  .swatch:hover { transform:scale(1.12); }
  .swatch.active { border-color:var(--text); transform:scale(1.15); }
  .swatch-terminal { background:linear-gradient(135deg,#080c17,#5B8DEF); }
  .swatch-dusk { background:linear-gradient(135deg,#2b1c16,#FF8A65); }
  .swatch-daylight { background:linear-gradient(135deg,#F7F4EE,#2F6FED); }

  /* ── MESSAGES ── */
  .msg {
    padding:14px 20px; border-radius:12px; margin:12px 0;
    font-size:0.9rem; line-height:1.5; animation:fadeDown .35s ease;
  }
  .msg-success { background:rgba(var(--green-rgb),0.10); border:1px solid rgba(var(--green-rgb),0.30); color:var(--green); }
  .msg-error { background:rgba(var(--red-rgb),0.10); border:1px solid rgba(var(--red-rgb),0.30); color:var(--red); }
  .msg-info { background:rgba(var(--accent-rgb),0.10); border:1px solid rgba(var(--accent-rgb),0.30); color:var(--accent); }

  /* ── CARDS ── */
  .card {
    background:var(--card-bg); backdrop-filter:blur(20px);
    border:1px solid var(--card-border);
    border-radius:var(--radius); padding:24px;
    margin-bottom:22px; box-shadow:var(--shadow);
    transition:background-color .4s ease, box-shadow .3s ease;
  }
  .card-header {
    display:flex; justify-content:space-between; align-items:center;
    margin-bottom:16px; flex-wrap:wrap; gap:8px;
  }
  .card-title {
    font-family:'Sora',sans-serif; font-weight:700; font-size:1.1rem;
    display:flex; align-items:center; gap:10px;
  }
  .card-title .badge {
    font-size:0.65rem; font-weight:600; text-transform:uppercase;
    letter-spacing:0.06em; padding:3px 10px; border-radius:999px;
    font-family:'Inter',sans-serif;
    background:rgba(var(--accent-rgb),0.12); color:var(--accent);
    border:1px solid rgba(var(--accent-rgb),0.20);
  }

  /* ── TOP STATUS BAR ── */
  .status-row { display:flex; gap:12px; flex-wrap:wrap; }
  .stat-pill {
    display:flex; align-items:center; gap:8px;
    background:var(--log-bg); border:1px solid var(--card-border);
    border-radius:999px; padding:8px 18px;
    font-size:0.85rem; flex:1; min-width:130px; justify-content:center;
    transition:background .3s ease;
  }
  .stat-pill .sp-label { color:var(--text-dim); font-size:0.7rem; text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }
  .stat-pill .sp-value { font-weight:700; font-variant-numeric:tabular-nums; color:var(--text); }
  .stat-pill .sp-value.green { color:var(--green); }
  .stat-pill .sp-value.red { color:var(--red); }
  .stat-pill .sp-value.blue { color:var(--accent); }
  .stat-pill .sp-mini { font-size:0.75rem; color:var(--text-dim); margin-left:4px; }

  /* ── MARKET DATA ── */
  .mkt-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .mkt-card {
    background:var(--log-bg); border:1px solid var(--card-border);
    border-radius:14px; padding:16px;
    position:relative; overflow:hidden;
    transition:transform .2s ease, border-color .2s ease;
  }
  .mkt-card:hover { transform:translateY(-2px); border-color:var(--border); }
  .mkt-label { font-size:0.65rem; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:var(--text-dim); margin-bottom:4px; }
  .mkt-spot { font-family:'Sora',sans-serif; font-size:1.4rem; font-weight:700; }
  .mkt-spot.green { color:var(--green); } .mkt-spot.red { color:var(--red); } .mkt-spot.blue { color:var(--accent); }
  .mkt-chg { font-size:0.95rem; font-weight:600; margin-top:2px; }
  .mkt-ohl { font-size:0.75rem; color:var(--text-dim); margin-top:6px; line-height:1.5; }
  .mkt-range { font-size:0.75rem; color:var(--amber); margin-top:2px; font-weight:500; }
  .mkt-pcr-big { font-family:'Sora',sans-serif; font-size:1.8rem; font-weight:800; }
  .mkt-sentiment { font-size:1rem; font-weight:700; margin-top:2px; }
  .mkt-oi { font-size:0.75rem; color:var(--text-dim); margin-top:6px; }

  /* ── TODAY'S SUMMARY ── */
  .sum-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:10px; }
  .sum-item {
    background:var(--log-bg); border:1px solid var(--card-border);
    border-radius:12px; padding:14px 12px; text-align:center;
    transition:transform .2s ease;
  }
  .sum-item:hover { transform:translateY(-2px); }
  .sum-label { font-size:0.6rem; font-weight:600; text-transform:uppercase; letter-spacing:0.07em; color:var(--text-dim); }
  .sum-value { font-family:'Sora',sans-serif; font-size:1.15rem; font-weight:700; margin-top:4px; }
  .sum-sub { font-size:0.65rem; color:var(--text-dim); margin-top:3px; }
  .sum-value.green { color:var(--green); } .sum-value.red { color:var(--red); } .sum-value.blue { color:var(--accent); }

  /* ── STRATEGY CARDS ── */
  .strat-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(310px,1fr)); gap:18px; }
  .strat {
    background:var(--card-bg); backdrop-filter:blur(20px);
    border:1px solid var(--card-border);
    border-radius:var(--radius); padding:20px;
    display:flex; flex-direction:column; gap:12px;
    box-shadow:var(--shadow);
    position:relative; overflow:hidden;
    transition:border-color .3s ease, box-shadow .3s ease, transform .2s ease;
  }
  .strat:hover { transform:translateY(-3px); }
  .strat.is-running {
    border-color:rgba(var(--green-rgb),0.25);
    box-shadow:0 0 32px -8px rgba(var(--green-rgb),0.25), var(--shadow);
  }
  .strat::before {
    content:''; position:absolute; top:0; left:0; bottom:0; width:4px;
    border-radius:4px 0 0 4px;
    background:var(--border); transition:background .4s ease, box-shadow .4s ease;
  }
  .strat.is-running::before { background:var(--green); box-shadow:0 0 16px rgba(var(--green-rgb),0.5); }
  .strat-head { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; }
  .strat-meta { display:flex; flex-direction:column; gap:1px; }
  .strat-icon {
    width:40px; height:40px; border-radius:12px;
    display:flex; align-items:center; justify-content:center; font-size:18px;
    background:var(--log-bg); border:1px solid var(--card-border);
    flex-shrink:0;
  }
  .strat-name { font-family:'Sora',sans-serif; font-size:1.05rem; font-weight:700; }
  .strat-eyebrow { font-size:0.6rem; font-weight:600; text-transform:uppercase; letter-spacing:0.08em; color:var(--text-dim); }
  .strat .status-row { display:flex; align-items:center; gap:10px; }
  .status-badge {
    display:inline-flex; align-items:center; gap:6px;
    padding:3px 12px; border-radius:999px;
    font-size:0.72rem; font-weight:600; text-transform:uppercase;
    letter-spacing:0.04em;
    background:rgba(var(--red-rgb),0.10); color:var(--red);
    border:1px solid rgba(var(--red-rgb),0.15);
  }
  .status-badge.running { background:rgba(var(--green-rgb),0.10); color:var(--green); border-color:rgba(var(--green-rgb),0.20); }
  .status-badge .pulse-dot {
    width:6px; height:6px; border-radius:50%; background:currentColor;
    animation:pulseDot 2s ease-in-out infinite;
  }
  @keyframes pulseDot { 0%,100%{opacity:1} 50%{opacity:.35} }
  .resume-tag {
    font-size:0.7rem; color:var(--amber);
    padding:2px 8px; border-radius:6px;
    background:rgba(var(--amber-rgb),0.10);
    border:1px solid rgba(var(--amber-rgb),0.15);
  }
  .strat .actions { display:flex; gap:8px; }
  .strat .actions .btn { flex:1; }
  .lots-row { display:flex; align-items:center; gap:8px; }
  .lots-row label { font-size:0.82rem; color:var(--text-dim); }
  .lots-row input {
    width:60px; padding:6px 8px; border-radius:8px;
    border:1px solid var(--card-border); background:var(--log-bg);
    color:var(--text); font-size:0.9rem; text-align:center; font-weight:600;
  }
  .lots-row input:focus { outline:2px solid var(--accent); outline-offset:1px; }

  .mini-log {
    background:var(--log-bg); border:1px solid var(--card-border);
    border-radius:10px; padding:12px 14px;
    font-family:'JetBrains Mono','Cascadia Code','Fira Code',monospace;
    font-size:0.75rem; max-height:200px; overflow-y:auto;
    line-height:1.6; color:var(--text-dim);
  }
  .mini-log .hl { color:var(--text); }

  /* ── BUTTONS ── */
  .btn {
    display:inline-flex; align-items:center; justify-content:center;
    gap:6px; padding:11px 18px; border:none; border-radius:10px;
    font-family:'Inter',sans-serif; font-size:0.88rem; font-weight:600;
    cursor:pointer; transition:transform .15s ease, filter .15s ease, opacity .15s ease, box-shadow .15s ease;
  }
  .btn:hover:not(:disabled) { transform:translateY(-1px); filter:brightness(1.08); }
  .btn:active:not(:disabled) { transform:translateY(0); }
  .btn-primary { background:linear-gradient(135deg,var(--accent),var(--accent-2)); color:#fff; box-shadow:0 4px 14px rgba(var(--accent-rgb),0.25); }
  .btn-primary:disabled { opacity:.30; cursor:not-allowed; box-shadow:none; }
  .btn-danger { background:linear-gradient(135deg,var(--red),var(--deep-red)); color:#fff; box-shadow:0 4px 14px rgba(var(--red-rgb),0.20); }
  .btn-danger:disabled { opacity:.30; cursor:not-allowed; box-shadow:none; }
  .btn-secondary { background:transparent; color:var(--text); border:1px solid var(--border); }
  .btn-secondary:hover { background:rgba(var(--accent-rgb),0.08); border-color:var(--accent); }
  .btn-sm { padding:4px 10px; font-size:0.72rem; border-radius:6px; min-width:auto; }

  /* ── TABS ── */
  .tab.active { display:block; animation:fadeDown .3s ease; }
  .tab-bar { display:flex; gap:4px; margin-bottom:18px; background:var(--log-bg); border-radius:12px; padding:4px; }
  .tab-bar button {
    padding:10px 22px; border:none; background:transparent;
    color:var(--text-dim); border-radius:10px;
    cursor:pointer; font-size:0.88rem; font-weight:600;
    transition:background .2s ease, color .2s ease;
  }
  .tab-bar button.active { background:var(--card-bg); color:var(--text); box-shadow:0 2px 8px rgba(0,0,0,0.12); }
  .tab-bar button:hover:not(.active) { background:rgba(var(--accent-rgb),0.06); }

  /* ── FORMS ── */
  input {
    padding:12px 16px; border-radius:10px;
    border:1px solid var(--card-border); background:var(--log-bg);
    color:var(--text); font-size:0.9rem; font-family:'Inter',sans-serif;
    transition:border-color .25s ease, box-shadow .25s ease;
    outline:none;
  }
  input[type="text"], input[type="password"], input[type="number"] { width:100%; }
  input:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(var(--accent-rgb),0.10); }
  input::placeholder { color:var(--text-dim); opacity:.6; }

  .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  .col { display:flex; flex-direction:column; gap:12px; }
  .flex-1 { flex:1; }
  .mt-8 { margin-top:8px; }
  .mb-8 { margin-bottom:8px; }

  a { color:var(--accent); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .text-xs { font-size:0.82rem; color:var(--text-dim); }

  /* ── MT TABLE ── */
  .mt-table { width:100%; font-size:0.8rem; border-collapse:collapse; }
  .mt-table th { padding:6px 8px; text-align:left; color:var(--text-dim); font-weight:600; font-size:0.65rem; text-transform:uppercase; letter-spacing:0.06em; border-bottom:1px solid var(--card-border); }
  .mt-table td { padding:6px 8px; border-bottom:1px solid var(--card-border); }
  .mt-table input { padding:4px 6px; width:70px; font-size:0.8rem; border-radius:6px; }

  /* ── RESPONSIVE ── */
  @media (max-width:1200px) {
    .mkt-grid { grid-template-columns:repeat(2,1fr); }
    .sum-grid { grid-template-columns:repeat(4,1fr); }
  }
  @media (max-width:1024px) {
    body { padding:20px; }
    .card { padding:20px; }
    .hero-title h1 { font-size:1.45rem; }
    .strat-grid { gap:14px; }
    .stat-pill { padding:6px 14px; font-size:0.8rem; min-width:100px; }
  }
  @media (max-width:768px) {
    body { padding:12px; }
    .hero { margin-bottom:18px; }
    .hero-icon { width:40px; height:40px; font-size:20px; border-radius:10px; }
    .hero-title h1 { font-size:1.2rem; }
    .hero-sub { font-size:0.65rem; }
    .hero-clock { display:none; }
    .card { padding:14px; margin-bottom:14px; border-radius:14px; }
    .card-header { margin-bottom:10px; }
    .mkt-grid { grid-template-columns:1fr 1fr; gap:8px; }
    .mkt-card { padding:12px; }
    .mkt-spot { font-size:1.15rem; }
    .mkt-pcr-big { font-size:1.4rem; }
    .sum-grid { grid-template-columns:repeat(2,1fr); gap:8px; }
    .sum-item { padding:10px; }
    .sum-value { font-size:1rem; }
    .strat-grid { grid-template-columns:1fr; gap:12px; }
    .strat { padding:16px; border-radius:14px; }
    .strat .actions { gap:6px; flex-wrap:wrap; }
    .strat .actions .btn { flex:1; min-width:72px; padding:10px 12px; font-size:0.82rem; }
    .mini-log { font-size:0.7rem; max-height:140px; padding:10px; }
    .status-row { gap:8px; }
    .stat-pill { min-width:auto; flex:1; padding:6px 10px; font-size:0.75rem; }
    .stat-pill .sp-mini { display:none; }
    .tab-bar { gap:2px; }
    .tab-bar button { padding:8px 14px; font-size:0.8rem; }
    .btn { padding:10px 14px; font-size:0.82rem; }
    input { padding:10px 14px; font-size:0.85rem; }
  }

  @media (prefers-reduced-motion:reduce) {
    *,*::before,*::after { animation-duration:0s!important; transition-duration:0s!important; }
    .strat:hover, .btn:hover { transform:none!important; }
  }
</style>
</head>
<body>
<div class="container">
  <!-- HERO -->
  <div class="hero">
    <div class="hero-brand">
      <div class="hero-icon">&#9889;</div>
      <div class="hero-title">
        <h1>Dashboard</h1>
        <div class="hero-sub">Multi-strategy execution console</div>
      </div>
    </div>
    <div class="hero-actions">
      <span class="hero-clock" id="live-clock"></span>
      <div class="theme-switch" id="theme-switch">
        <button class="swatch swatch-terminal" data-theme-option="terminal" title="Terminal"></button>
        <button class="swatch swatch-dusk" data-theme-option="dusk" title="Dusk"></button>
        <button class="swatch swatch-daylight" data-theme-option="daylight" title="Daylight"></button>
      </div>
    </div>
  </div>

  <!-- Flash messages -->
  {% set msgs = request.args.get('msg','').split('|') if request.args.get('msg') else [] %}
  {% for m in msgs %}
    {% if m %}
    <div class="msg msg-{{ m.split(':')[0] }}">{{ m.split(':',1)[1] if ':' in m else m }}</div>
    {% endif %}
  {% endfor %}

  <!-- TOP STATUS BAR -->
  <div class="card" style="padding:16px 20px;">
    <div class="status-row">
      <div class="stat-pill"><span class="sp-label">Heartbeat</span><span class="sp-value" id="s-heartbeat">---</span></div>
      <div class="stat-pill"><span class="sp-label">Token</span><span class="sp-value" id="s-token">---</span></div>
      <div class="stat-pill"><span class="sp-label">Strategies</span><span class="sp-value blue" id="s-count">0/10</span><span class="sp-mini" id="s-running-list"></span></div>
    </div>
  </div>

  <!-- MARKET DATA -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">&#128200; Market Data <span class="badge" id="market-time">---</span></div>
    </div>
    <div class="mkt-grid">
      <div class="mkt-card">
        <div class="mkt-label">NIFTY 50</div>
        <div class="mkt-spot blue" id="m-nifty-spot">---</div>
        <div class="mkt-chg" id="m-nifty-chg"></div>
        <div class="mkt-ohl">O: <span id="m-nifty-open"></span> &middot; H: <span id="m-nifty-high"></span> &middot; L: <span id="m-nifty-low"></span></div>
        <div class="mkt-range">&#8646; Range: <span id="m-nifty-range"></span></div>
      </div>
      <div class="mkt-card">
        <div class="mkt-label">SENSEX</div>
        <div class="mkt-spot blue" id="m-sensex-spot">---</div>
        <div class="mkt-chg" id="m-sensex-chg"></div>
        <div class="mkt-ohl">O: <span id="m-sensex-open"></span> &middot; H: <span id="m-sensex-high"></span> &middot; L: <span id="m-sensex-low"></span></div>
        <div class="mkt-range">&#8646; Range: <span id="m-sensex-range"></span></div>
      </div>
      <div class="mkt-card">
        <div class="mkt-label">BANK NIFTY</div>
        <div class="mkt-spot blue" id="m-banknifty-spot">---</div>
        <div class="mkt-chg" id="m-banknifty-chg"></div>
        <div class="mkt-ohl">O: <span id="m-banknifty-open"></span> &middot; H: <span id="m-banknifty-high"></span> &middot; L: <span id="m-banknifty-low"></span></div>
        <div class="mkt-range">&#8646; Range: <span id="m-banknifty-range"></span></div>
      </div>
      <div class="mkt-card">
        <div class="mkt-label">SENTIMENT (PCR)</div>
        <div class="mkt-pcr-big" id="m-pcr">---</div>
        <div class="mkt-sentiment" id="m-sentiment"></div>
        <div class="mkt-oi">CE OI: <span id="m-ce-oi"></span> &middot; PE OI: <span id="m-pe-oi"></span></div>
      </div>
    </div>
  </div>

  <!-- TODAY'S SUMMARY -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">&#128202; Today's Summary <span class="badge" id="trade-date"></span></div>
    </div>
    <div class="sum-grid">
      <div class="sum-item">
        <div class="sum-label">Running</div>
        <div class="sum-value blue" id="td-running">0</div>
        <div class="sum-sub" id="td-running-list"></div>
      </div>
      <div class="sum-item">
        <div class="sum-label">Closed</div>
        <div class="sum-value blue" id="td-closed">0</div>
        <div class="sum-sub" id="td-closed-list"></div>
      </div>
      <div class="sum-item">
        <div class="sum-label">P&amp;L (Closed)</div>
        <div class="sum-value" id="td-pnl">&#8377;0</div>
        <div class="sum-sub">today</div>
      </div>
      <div class="sum-item">
        <div class="sum-label">Live P&amp;L</div>
        <div class="sum-value" id="td-live-pnl">&#8377;0</div>
        <div class="sum-sub" id="td-live-pos"></div>
      </div>
      <div class="sum-item">
        <div class="sum-label">Est. Charges</div>
        <div class="sum-value red" id="td-chg-running">&#8377;0</div>
        <div class="sum-sub">open @ LTP</div>
      </div>
      <div class="sum-item">
        <div class="sum-label">Actual Charges</div>
        <div class="sum-value red" id="td-chg-closed">&#8377;0</div>
        <div class="sum-sub">fill price</div>
      </div>
      <div class="sum-item">
        <div class="sum-label">Net P&amp;L</div>
        <div class="sum-value" id="td-net">&#8377;0</div>
        <div class="sum-sub">after charges</div>
      </div>
    </div>
  </div>

  <!-- STRATEGY CARDS -->
  <div class="strat-grid" id="strat-grid">
    {% for sid in ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio'] %}
    <div class="strat" id="strat-{{ sid }}">
      <div class="strat-head">
        <div class="strat-meta">
          <div class="strat-name">{{ {'ic':'Iron Condor','cs':'Credit Spread','sma':'SMA Crossover','mt':'Manual Trade','bnf':'Bank Nifty 2H SMA','n1h':'Nifty 1H SMA','sw':'Swing Scanner','sr':'Swing Rebalancer','ratio':'NiftyBees/GoldBees'}[sid] }}</div>
          <div class="strat-eyebrow">{{ {'ic':'NIFTY OPTIONS','cs':'NIFTY OPTIONS','sma':'SENSEX INDEX','mt':'MANUAL &middot; TRAIL SL','bnf':'BANK NIFTY','n1h':'NIFTY OPTIONS','sw':'WEEKLY SCAN','sr':'DAILY REBALANCE','ratio':'PAIR TRADE'}[sid] }}</div>
        </div>
        <div class="strat-icon">{{ {'ic':'&#9670;','cs':'&#9632;','sma':'&#8593;','mt':'&#9998;','bnf':'&#9632;','n1h':'&#9670;','sw':'&#128269;','sr':'&#128260;','ratio':'&#9881;'}[sid]|safe }}</div>
      </div>
      <div class="status-row">
        <span class="status-badge" id="sb-{{ sid }}"><span class="pulse-dot" id="dot-{{ sid }}" style="display:none;"></span><span id="s-{{ sid }}">STOPPED</span></span>
        <span class="resume-tag" id="resume-{{ sid }}" style="display:none;">&#9888; Saved</span>
      </div>
      <div class="actions">
        {% if sid == 'mt' %}
        <button class="btn btn-secondary" id="scan-{{ sid }}" onclick="scanMt()">&#128269; Scan</button>
        {% else %}
        <button class="btn btn-primary" id="start-{{ sid }}" onclick="action('{{ sid }}','start')">&#9654; Start</button>
        {% endif %}
        <button class="btn btn-secondary" id="resume-btn-{{ sid }}" onclick="action('{{ sid }}','resume')" style="display:none;">&#8635; Resume</button>
        <button class="btn btn-danger" id="stop-{{ sid }}" onclick="action('{{ sid }}','stop')" disabled>&#9632; Stop</button>
      </div>
      <div class="lots-row" id="lots-{{ sid }}">
        {% if sid == 'mt' %}
        {% else %}
        <label>Lots:</label>
        <input type="number" id="lots-input-{{ sid }}" value="1" min="1" max="10">
        {% endif %}
      </div>
      {% if sid == 'mt' %}
      <div id="mt-positions" style="display:none;"></div>
      {% endif %}
      <div class="mini-log" id="log-{{ sid }}"><div class="hl">Waiting for output...</div></div>
    </div>
    {% endfor %}

    <!-- Scalper cards — one per index, each matching the standard card layout -->
    {% for sc_sid in ['sc_nifty','sc_bnf','sc_sensex'] %}
    {% set sc_label = {'sc_nifty':'3-Min NIFTY','sc_bnf':'3-Min BANKNIFTY','sc_sensex':'3-Min SENSEX'}[sc_sid] %}
    {% set sc_icon = {'sc_nifty':'&#9670;','sc_bnf':'&#9632;','sc_sensex':'&#9670;'}[sc_sid] %}
    <div class="strat" id="strat-{{ sc_sid }}">
      <div class="strat-head">
        <div class="strat-meta">
          <div class="strat-name">{{ sc_label }}</div>
          <div class="strat-eyebrow">ADX + SUPERTREND</div>
        </div>
        <div class="strat-icon">{{ sc_icon|safe }}</div>
      </div>
      <div class="status-row">
        <span class="status-badge" id="sb-{{ sc_sid }}"><span class="pulse-dot" id="dot-{{ sc_sid }}" style="display:none;"></span><span id="s-{{ sc_sid }}">STOPPED</span></span>
        <span class="resume-tag" id="resume-{{ sc_sid }}" style="display:none;">&#9888; Saved</span>
      </div>
      <div class="actions">
        <button class="btn btn-primary" id="start-{{ sc_sid }}" onclick="action('{{ sc_sid }}','start')">&#9654; Start</button>
        <button class="btn btn-secondary" id="resume-btn-{{ sc_sid }}" onclick="action('{{ sc_sid }}','resume')" style="display:none;">&#8635; Resume</button>
        <button class="btn btn-danger" id="stop-{{ sc_sid }}" onclick="action('{{ sc_sid }}','stop')" disabled>&#9632; Stop</button>
      </div>
      <div class="lots-row">
        <label>Lots:</label>
        <input type="number" id="lots-input-{{ sc_sid }}" value="1" min="1" max="10">
      </div>
      <div class="mini-log" id="log-{{ sc_sid }}"><div class="hl">Waiting for output...</div></div>
    </div>
    {% endfor %}
  </div>

  <!-- SETTINGS -->
  <div class="card">
    <div class="tab-bar">
      <button class="active" data-tab="tab-login">&#128273; Login</button>
      <button data-tab="tab-api">&#128295; API Keys</button>
    </div>
    <div id="tab-login" class="tab active">
      {% if has_api_key %}
        <div class="row mb-8">
          <a href="{{ login_url }}" class="btn btn-secondary" id="btn-login" target="_blank">&#128273; Login at Zerodha</a>
          <span class="text-xs" id="login-status">{{ '&#10003; Logged in' if has_token else '&#10007; Not logged in' }}</span>
        </div>
        <div class="msg msg-info" id="login-hint" style="display:none;">
          1. Click Login &rarr; authorize on Zerodha<br>
          2. Auto-redirect captures token<br>
          <strong>Or</strong> paste redirect URL below:
        </div>
        <form method="POST" action="/api/token" class="row mt-8">
          <input type="text" name="redirect_url" placeholder="Paste redirect URL or request_token..." style="flex:1;">
          <button type="submit" class="btn btn-secondary">Save Token</button>
        </form>
      {% else %}
        <div class="msg msg-info">API Key not configured. Go to <strong>API Keys</strong> tab.</div>
      {% endif %}
    </div>
    <div id="tab-api" class="tab">
      <form method="POST" action="/api/config" class="col">
        <input type="text" name="api_key" placeholder="API Key" value="{{ api_key }}">
        <input type="password" name="api_secret" placeholder="API Secret">
        <input type="password" name="dashboard_password" placeholder="Dashboard Password (leave blank to disable)">
        <button type="submit" class="btn btn-primary">Save Settings</button>
      </form>
      <div class="mt-8 text-xs">&#128274; Set a dashboard password to protect controls. <a href="/logout">Logout</a></div>
    </div>
  </div>

</div>

<script>
function $(id){return document.getElementById(id)}
const C={green:'#16C098',red:'#FF5C72',amber:'#F5A623',blue:'#5B8DEF',deepRed:'#E0344C'};

// ── THEME ──
function setActiveSwatch(t){
  document.querySelectorAll('[data-theme-option]').forEach(b=>b.classList.toggle('active',b.dataset.themeOption===t));
}
setActiveSwatch(document.documentElement.getAttribute('data-theme')||'terminal');
document.querySelectorAll('[data-theme-option]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const t=btn.dataset.themeOption;
    document.documentElement.setAttribute('data-theme',t);
    try{localStorage.setItem('dashTheme',t)}catch(e){}
    setActiveSwatch(t);
  });
});

// ── LIVE CLOCK ──
function updateClock(){
  const n=new Date();
  $('live-clock').textContent=n.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
setInterval(updateClock,1000);updateClock();

// ── TABS ──
document.querySelectorAll('[data-tab]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.tab-bar button').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    btn.classList.add('active');
    $(btn.dataset.tab).classList.add('active');
  });
});

// ── ACTION ──
async function action(strategy,cmd){
  if(cmd==='start'||cmd==='resume'){
    var extra='';
    var inp=document.getElementById('lots-input-'+strategy);
    if(inp) extra+='&lots='+(parseInt(inp.value)||1);
    var sym=document.getElementById('sym-'+strategy);
    if(sym) extra+='&symbol='+sym.value;
    await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'strategy='+strategy+'&resume='+(cmd==='resume'?'1':'0')+extra});
  }else{
    await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'strategy='+strategy});
  }
  await fetchStatus();
}

// ── STATUS ──
async function fetchStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    const hb=d.heartbeat||'---';
    $('s-heartbeat').textContent=hb.length>30?hb.slice(0,28)+'...':hb;
    const tk=$('s-token');
    if(d.token_ok){tk.textContent='OK';tk.className='sp-value green';}
    else if(d.token_ok===false){tk.textContent='EXPIRED';tk.className='sp-value red';}
    else{tk.textContent='---';tk.className='sp-value';}
    let count=0;var runningNames=[];let scRunning=false;
    for(const s of ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio','sc_nifty','sc_bnf','sc_sensex']){
      const running=d.strategies&&d.strategies[s];
      if(s.startsWith('sc_')){if(running)scRunning=true;}
      else{if(running){count++;runningNames.push(s.toUpperCase());}}
      const dot=$('dot-'+s);
      const badge=$('sb-'+s);
      if(dot)dot.style.display=running?'inline':'none';
      if(badge)badge.className='status-badge'+(running?' running':'');
      $('s-'+s).textContent=running?'RUNNING':'STOPPED';
      const cardEl=$('strat-'+s);
      if(cardEl)cardEl.classList.toggle('is-running',running);
      const startBtn=$('start-'+s);
      if(startBtn)startBtn.disabled=running;
      const stopBtn=$('stop-'+s);
      if(stopBtn)stopBtn.disabled=!running;
      const ri=$('resume-'+s);
      const rb=$('resume-btn-'+s);
      if(d.positions&&d.positions[s]){
        ri.style.display='inline';
        ri.title='Expiry: '+d.positions[s].expiry+' | Credit: '+d.positions[s].entry_credit;
        if(!running){rb.style.display='inline';}
      }else{ri.style.display='none';rb.style.display='none';}
      if(s==='mt'){const scanBtn=$('scan-mt');if(scanBtn)scanBtn.style.display='inline-block';}
    }
    if(scRunning){count++;runningNames.push('SCALPER');}
    $('s-count').textContent=count+'/10';
    $('s-running-list').textContent=runningNames.length?runningNames.join(', '):'';
  }catch(e){}
}
setInterval(fetchStatus,5000);fetchStatus();

// ── LOGS ──
async function fetchLogs(){
  try{
    for(const s of ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio','sc_nifty','sc_bnf','sc_sensex']){
      const d=await(await fetch('/api/log?strategy='+s)).json();
      const box=$('log-'+s);
      if(d.output&&d.output.length){
        box.innerHTML=d.output.slice(-30).map(l=>'<div>'+l.replace(/</g,'&lt;')+'</div>').join('');
        box.scrollTop=box.scrollHeight;
      }
    }
  }catch(e){}
}
setInterval(fetchLogs,5000);fetchLogs();

// ── MARKET ──
async function fetchMarket(){
  try{
    const d=await(await fetch('/api/market')).json();
    if(d.error)return;
    for(const [prefix,key] of [['nifty','nifty'],['sensex','sensex'],['banknifty','banknifty']]){
      const v=d[key];if(!v)continue;
      const spotEl=$('m-'+prefix+'-spot');
      spotEl.textContent=v.spot.toLocaleString('en-IN',{minimumFractionDigits:2});
      spotEl.className='mkt-spot '+(v.change>=0?'green':'red');
      $('m-'+prefix+'-chg').textContent=(v.change>=0?'+':'')+v.change.toFixed(2)+' ('+(v.change_pct>=0?'+':'')+v.change_pct.toFixed(2)+'%)';
      $('m-'+prefix+'-chg').style.color=v.change>=0?C.green:C.red;
      $('m-'+prefix+'-open').textContent=v.open.toLocaleString('en-IN',{minimumFractionDigits:2});
      $('m-'+prefix+'-high').textContent=v.high.toLocaleString('en-IN',{minimumFractionDigits:2});
      $('m-'+prefix+'-low').textContent=v.low.toLocaleString('en-IN',{minimumFractionDigits:2});
      $('m-'+prefix+'-range').textContent=(v.high-v.low).toLocaleString('en-IN',{minimumFractionDigits:2})+' pts';
    }
    if(d.pcr){
      $('m-pcr').textContent=d.pcr.toFixed(2);
      let sc=C.amber;
      if(d.sentiment.includes('STRONG BEAR'))sc=C.red;
      else if(d.sentiment.includes('BEARISH'))sc=C.deepRed;
      else if(d.sentiment.includes('WEAK BEAR'))sc=C.amber;
      else if(d.sentiment.includes('WEAK BULL'))sc=C.blue;
      else if(d.sentiment.includes('BULLISH')||d.sentiment.includes('STRONG BULL'))sc=C.green;
      $('m-pcr').style.color=sc;
      $('m-sentiment').textContent=d.sentiment;
      $('m-sentiment').style.color=sc;
      $('m-ce-oi').textContent=(d.ce_oi/1e7).toFixed(2)+'Cr';
      $('m-pe-oi').textContent=(d.pe_oi/1e7).toFixed(2)+'Cr';
    }
    $('market-time').textContent='Updated: '+(d.time||'');
  }catch(e){}
}
setInterval(fetchMarket,15000);fetchMarket();

// ── TRADES ──
async function fetchTrades(){
  try{
    const d=await(await fetch('/api/trades')).json();
    if(d.error)return;
    $('trade-date').textContent=d.date;
    $('td-running').textContent=d.running_trades;
    $('td-running-list').textContent=d.running_details.join(', ')||'none';
    $('td-closed').textContent=d.closed_trades;
    $('td-closed-list').textContent=Object.entries(d.by_strategy).map(([k,v])=>k+':'+v).join(', ')||'none';
    const pnlCls=d.closed_pnl>=0?'green':'red';
    $('td-pnl').textContent='\u20B9'+d.closed_pnl.toLocaleString('en-IN',{minimumFractionDigits:2});
    $('td-pnl').className='sum-value '+pnlCls;
    if(d.live_pnl!==undefined){
      const lc=d.live_pnl>=0?C.green:C.red;
      $('td-live-pnl').textContent='\u20B9'+d.live_pnl.toLocaleString('en-IN',{minimumFractionDigits:2});
      $('td-live-pnl').style.color=lc;
      $('td-live-pos').textContent=(d.live_positions||0)+' positions';
    }
    $('td-chg-running').textContent='\u20B9'+(d.est_charges_running||0).toLocaleString('en-IN');
    $('td-chg-closed').textContent='\u20B9'+(d.actual_charges_closed||0).toLocaleString('en-IN');
    const net=(d.closed_pnl||0)+(d.live_pnl||0)-(d.total_charges||0);
    $('td-net').textContent='\u20B9'+net.toLocaleString('en-IN',{minimumFractionDigits:2});
    $('td-net').className='sum-value '+(net>=0?'green':'red');
  }catch(e){}
}
setInterval(fetchTrades,10000);fetchTrades();

// ── MT SCAN ──
var mtPositions=[];
async function scanMt(){
  const r=await fetch('/api/mt-scan',{method:'POST'});
  const d=await r.json();
  const c=$('mt-positions');
  if(!d.ok){c.innerHTML='<div style="color:var(--red);padding:8px;">Error: '+d.error+'</div>';c.style.display='block';return;}
  if(!d.positions.length){c.innerHTML='<div style="padding:8px;color:var(--text-dim);">No positions found.</div>';c.style.display='block';return;}
  mtPositions=d.positions;
  let h='<table class="mt-table">';
  h+='<tr><th>Side</th><th>Symbol</th><th style="text-align:right;">Qty</th><th style="text-align:right;">Avg</th><th style="text-align:right;">LTP</th><th style="text-align:right;">SL</th><th></th></tr>';
  for(let i=0;i<d.positions.length;i++){
    const p=d.positions[i];
    h+='<tr>';
    h+='<td>'+p.side+'</td>';
    h+='<td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;">'+p.tsym+'</td>';
    h+='<td style="text-align:right;">'+p.qty+'</td>';
    h+='<td style="text-align:right;">\u20B9'+p.avg_price.toFixed(2)+'</td>';
    h+='<td style="text-align:right;">\u20B9'+p.ltp.toFixed(2)+'</td>';
    h+='<td style="text-align:right;"><input type="number" id="mt-sl-'+i+'" placeholder="SL" min="0.01" step="0.01"></td>';
    h+='<td><button class="btn btn-sm btn-secondary" onclick="setMt('+i+')" id="mt-set-'+i+'">Set</button></td>';
    h+='</tr>';
  }
  h+='</table>';
  c.innerHTML=h;c.style.display='block';
}
async function setMt(i){
  const p=mtPositions[i];if(!p)return;
  const slInput=$('mt-sl-'+i);
  const sl_price=parseFloat(slInput.value);
  if(!sl_price||sl_price<=0){alert('Enter a valid SL price');return;}
  const status=await(await fetch('/api/status')).json();
  const running=status.strategies&&status.strategies['mt'];
  const endpoint=running?'/api/mt-add':'/api/mt-start';
  const r=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trades:[{...p,sl_price}]})});
  const d=await r.json();
  if(!d.ok){alert('Error: '+d.error);return;}
  const btn=$('mt-set-'+i);
  if(btn){btn.textContent='\u2713';btn.disabled=true;btn.style.opacity='0.6';}
  await fetchStatus();
}

// ── DOM READY ──
document.addEventListener('DOMContentLoaded',function(){
  for(const s of ['sw','sr']){
    var el=document.getElementById('lots-'+s);
    if(el)el.style.display='none';
  }
});
$('btn-login')?.addEventListener('click',()=>{$('login-hint').style.display='block';});
document.querySelectorAll('.msg-success,.msg-error').forEach(m=>setTimeout(()=>m.remove(),8000));
</script>
</body>
</html>"""


@app.route("/login", methods=["POST"])
def login():
    pw = get_dashboard_password()
    if not pw:
        session["authenticated"] = True
        return redirect("/")
    if request.form.get("password", "") == pw:
        session["authenticated"] = True
        return redirect("/")
    return render_template_string(PASSWORD_PAGE, error="Wrong password")


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect("/login")


@app.route("/")
@require_auth
def home():
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    has_token = bool(cfg.get("access_token", ""))
    has_api_key = bool(api_key)
    login_url = ""
    if api_key:
        login_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
    return render_template_string(
        PAGE,
        has_token=has_token,
        has_api_key=has_api_key,
        api_key=api_key,
        login_url=login_url,
    )


@app.route("/callback")
@require_auth
def callback():
    request_token = request.args.get("request_token", "")
    if not request_token:
        return redirect("/?msg=error:No request_token received")
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    api_secret = cfg.get("api_secret", "")
    if not api_key or not api_secret:
        return redirect("/?msg=error:API key/secret not configured")
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        cfg["access_token"] = data["access_token"]
        save_config(cfg)
        return redirect("/?msg=success:Login successful! Token saved.")
    except Exception as e:
        return redirect(f"/?msg=error:Login failed: {e}")


@app.route("/api/token", methods=["POST"])
@require_auth
def api_token():
    raw = request.form.get("redirect_url", "").strip()
    if not raw:
        return redirect("/?msg=error:No URL provided")
    if "request_token=" in raw:
        raw = raw.split("request_token=")[1].split("&")[0]
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    api_secret = cfg.get("api_secret", "")
    if not api_key or not api_secret:
        return redirect("/?msg=error:API key/secret not configured")
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(raw, api_secret=api_secret)
        cfg["access_token"] = data["access_token"]
        save_config(cfg)
        return redirect("/?msg=success:Token saved successfully!")
    except Exception as e:
        return redirect(f"/?msg=error:Token exchange failed: {e}")


@app.route("/api/start", methods=["POST"])
@require_auth
def api_start():
    strategy = request.form.get("strategy", "")
    resume = request.form.get("resume", "0") == "1"
    lots = request.form.get("lots", "1")
    if strategy not in STRATEGIES:
        return jsonify({"ok": False, "error": "Invalid strategy"}), 400
    proc = bot_processes.get(strategy)
    if proc and proc.poll() is None:
        return jsonify({"ok": False, "error": f"{strategy.upper()} already running"}), 400
    if strategy in ("sw", "sr", "ratio"):
        scripts = {"sw": "swing_scanner.py", "sr": "swing_rebalancer.py", "ratio": "ratio_strategy.py"}
        cmd = [sys.executable, "-u", str(BOT_DIR / scripts[strategy])]
    else:
        cmd = [sys.executable, "-u", str(BOT_DIR / "iron_condor_algo.py"), f"--strategy={strategy}"]
        if resume:
            cmd.append("--resume")
        if lots and lots != "1":
            cmd.append(f"--lots={lots}")
        if strategy == "mt":
            sl = request.form.get("sl", "50")
            cmd.append(f"--sl={sl}")
        if strategy.startswith("sc_"):
            sym_map = {"sc_nifty": "NIFTY", "sc_bnf": "BANKNIFTY", "sc_sensex": "SENSEX"}
            cmd = [sys.executable, "-u", str(BOT_DIR / "iron_condor_algo.py"), "--strategy=sc", f"--symbol={sym_map.get(strategy, 'NIFTY')}"]
            if lots and lots != "1":
                cmd.append(f"--lots={lots}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        cmd,
        cwd=str(BOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    bot_processes[strategy] = proc
    with bot_output_locks[strategy]:
        bot_outputs[strategy] = []
    t = threading.Thread(target=_reader_thread, args=(strategy, proc), daemon=True)
    t.start()
    return jsonify({"ok": True, "strategy": strategy})


@app.route("/api/stop", methods=["POST"])
@require_auth
def api_stop():
    strategy = request.form.get("strategy", "")
    if strategy not in STRATEGIES:
        return jsonify({"ok": False, "error": "Invalid strategy"}), 400
    proc = bot_processes.get(strategy)
    if not proc or proc.poll() is not None:
        return jsonify({"ok": False, "error": f"{strategy.upper()} not running"}), 400
    if os.name == "nt":
        proc.terminate()
    else:
        os.kill(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    bot_processes[strategy] = None
    cfg = load_config()
    pos_key = {"ic": "position", "cs": "cs_position", "mt": "manual_trades"}.get(strategy)
    if pos_key and cfg.get(pos_key):
        cfg.pop(pos_key, None)
        save_config(cfg)
    return jsonify({"ok": True, "strategy": strategy})


@app.route("/api/mt-scan", methods=["POST"])
@require_auth
def api_mt_scan():
    """Scan Zerodha positions and return list of single-leg option trades."""
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if not api_key or not access_token:
        return jsonify({"ok": False, "error": "Not logged in"}), 400
    try:
        kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=15)
        all_pos = kite.positions()
        raw_positions = all_pos.get("day", []) + all_pos.get("net", [])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Deduplicate by tradingsymbol
    seen = set()
    positions = []
    for p in raw_positions:
        tsym = p.get("tradingsymbol", "")
        if tsym not in seen:
            seen.add(tsym)
            positions.append(p)

    tracked_tsyms = set()
    for key in ("position", "cs_position"):
        saved = cfg.get(key)
        if saved and "legs" in saved:
            for leg in saved["legs"]:
                tracked_tsyms.add(leg.get("tradingsymbol", ""))

    results = []
    for p in positions:
        tsym = p.get("tradingsymbol", "")
        qty = p.get("quantity", 0)
        if qty == 0 or tsym in tracked_tsyms:
            continue
        exchange = p.get("exchange", "")
        if exchange not in ("NFO", "BFO"):
            continue
        otype = "CE" if tsym.endswith("CE") else ("PE" if tsym.endswith("PE") else "")
        if otype not in ("CE", "PE"):
            continue
        try:
            ltp_data = kite.ltp(f"{exchange}:{tsym}")
            ltp = ltp_data.get(f"{exchange}:{tsym}", {}).get("last_price", 0)
        except Exception:
            ltp = 0
        results.append({
            "tsym": tsym,
            "exchange": exchange,
            "option_type": otype,
            "side": "BUY" if qty > 0 else "SELL",
            "qty": abs(qty),
            "strike": float(p.get("strike_price", 0)),
            "expiry": (p.get("expiry_date", "") or "")[:10],
            "avg_price": float(p.get("average_price", 0)),
            "ltp": ltp,
        })
    return jsonify({"ok": True, "positions": results})


@app.route("/api/mt-start", methods=["POST"])
@require_auth
def api_mt_start():
    """Save selected manual trades with SLs and start monitoring."""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400
    trades = data.get("trades", [])
    if not trades:
        return jsonify({"ok": False, "error": "No trades selected"}), 400
    cfg = load_config()
    trade_list = []
    for t in trades:
        avg = float(t.get("avg_price", 0))
        sl_price = float(t.get("sl_price", 0))
        if sl_price <= 0:
            sl_price = avg - 10 if t.get("side") == "BUY" else avg + 10
        trade_list.append({
            "tsym": t["tsym"],
            "strike": float(t.get("strike", 0)),
            "option_type": t.get("option_type", ""),
            "side": t.get("side", ""),
            "qty": int(t.get("qty", 0)),
            "exchange": t.get("exchange", ""),
            "expiry": t.get("expiry", ""),
            "entry_price": avg,
            "entry_sl": sl_price,
            "sl": sl_price,
            "target_level": 1.0,
            "entry_ts": datetime.now().isoformat(),
        })
    cfg["manual_trades"] = trade_list
    save_config(cfg)

    # Launch bot
    proc = bot_processes.get("mt")
    if proc and proc.poll() is None:
        return jsonify({"ok": False, "error": "Manual Trade already running"}), 400
    cmd = [sys.executable, "-u", str(BOT_DIR / "iron_condor_algo.py"), "--strategy=mt", "--resume"]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(cmd, cwd=str(BOT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", bufsize=1, env=env)
    bot_processes["mt"] = proc
    with bot_output_locks["mt"]:
        bot_outputs["mt"] = []
    t = threading.Thread(target=_reader_thread, args=("mt", proc), daemon=True)
    t.start()
    return jsonify({"ok": True, "strategy": "mt"})


@app.route("/api/mt-add", methods=["POST"])
@require_auth
def api_mt_add():
    """Add new manual trades to config while bot is running."""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400
    trades = data.get("trades", [])
    if not trades:
        return jsonify({"ok": False, "error": "No trades selected"}), 400
    cfg = load_config()
    existing = cfg.get("manual_trades", [])
    if isinstance(existing, dict):
        existing = [existing]
    if not isinstance(existing, list):
        existing = []
    existing_tsyms = {t["tsym"] for t in existing if "tsym" in t}
    for t in trades:
        avg = float(t.get("avg_price", 0))
        sl_price = float(t.get("sl_price", 0))
        if sl_price <= 0:
            sl_price = avg - 10 if t.get("side") == "BUY" else avg + 10
        entry = {
            "tsym": t["tsym"],
            "strike": float(t.get("strike", 0)),
            "option_type": t.get("option_type", ""),
            "side": t.get("side", ""),
            "qty": int(t.get("qty", 0)),
            "exchange": t.get("exchange", ""),
            "expiry": t.get("expiry", ""),
            "entry_price": avg,
            "entry_sl": sl_price,
            "sl": sl_price,
            "target_level": 1.0,
            "entry_ts": datetime.now().isoformat(),
        }
        if t["tsym"] in existing_tsyms:
            # Replace existing entry with same tsym
            for i, e in enumerate(existing):
                if e.get("tsym") == t["tsym"]:
                    existing[i] = entry
                    break
        else:
            existing.append(entry)
            existing_tsyms.add(t["tsym"])
    cfg["manual_trades"] = existing
    save_config(cfg)
    return jsonify({"ok": True, "count": len(trades)})


@app.route("/api/status")
@require_auth
def api_status():
    running = {}
    for s in STRATEGIES:
        proc = bot_processes.get(s)
        running[s] = proc is not None and proc.poll() is None
    heartbeat = ""
    if HEARTBEAT_FILE.exists():
        heartbeat = HEARTBEAT_FILE.read_text().strip()
    cfg = load_config()
    token_ok = None
    access_token = cfg.get("access_token", "")
    api_key = cfg.get("api_key", "")
    if access_token and api_key:
        try:
            k = KiteConnect(api_key=api_key, access_token=access_token, timeout=10)
            k.profile()
            token_ok = True
        except Exception:
            token_ok = False
    positions = {}
    if cfg.get("position"):
        p = cfg["position"]
        legs_info = [f"{l['action']} {l['tradingsymbol']} @ ?{l['premium']}" for l in p.get("legs", [])]
        positions["ic"] = {"expiry": p["expiry"], "entry_credit": p["entry_credit"], "legs": legs_info}
    if cfg.get("cs_position"):
        p = cfg["cs_position"]
        legs_info = [f"{l['action']} {l['tradingsymbol']} @ ?{l['premium']}" for l in p.get("legs", [])]
        positions["cs"] = {"expiry": p["expiry"], "entry_credit": p["net_credit"], "legs": legs_info}
    if running.get("mt") and cfg.get("manual_trades"):
        trades = cfg["manual_trades"]
        if isinstance(trades, dict):
            trades = [trades]
        legs = [f"{t['side']} {t['tsym']} SL @ {t.get('sl',0)}" for t in trades]
        positions["mt"] = {"expiry": trades[0].get("expiry", "") if trades else "", "entry_credit": 0, "legs": legs}
    return jsonify({
        "strategies": running,
        "heartbeat": heartbeat,
        "token_ok": token_ok,
        "has_api_key": bool(api_key),
        "positions": positions,
    })


@app.route("/api/log")
@require_auth
def api_log():
    strategy = request.args.get("strategy", "ic")
    if strategy not in STRATEGIES:
        strategy = "ic"
    with bot_output_locks[strategy]:
        out = list(bot_outputs[strategy])
    return jsonify({"output": out[-100:]})


def calc_charges(legs: list, lot_size: int, exchange: str = "NFO") -> float:
    """Mirror of iron_condor_algo's calc_charges for dashboard estimates."""
    orders = len(legs) * 2
    brokerage = orders * 20.0
    total_prem_turnover = 0.0
    sell_prem_value = 0.0
    for leg in legs:
        prem = float(leg.get("premium", 0))
        action = leg.get("action", "")
        value = prem * lot_size
        total_prem_turnover += value
        if action == "SELL":
            sell_prem_value += value
    stt = sell_prem_value * 0.0005
    rate_per_crore = 50.5 if exchange == "NFO" else 37.5
    turnover_cr = total_prem_turnover / 1_00_00_000
    trans_charge = turnover_cr * rate_per_crore
    sebi = turnover_cr * 10.0
    stamp = total_prem_turnover * 0.00003
    gst = (brokerage + trans_charge + sebi) * 0.18
    return round(brokerage + stt + trans_charge + sebi + stamp + gst, 2)


_market_cache = {"data": None, "time": 0}

def classify_pcr(pcr: float, price_change_pct: float = 0) -> str:
    if pcr >= 1.30:
        return "STRONG BULLISH"
    if pcr >= 1.06:
        return "WEAK BULLISH"
    if pcr >= 0.95:
        base = "NEUTRAL"
        if price_change_pct > 0.5:
            return f"{base} TO BULLISH"
        return base
    if pcr >= 0.75:
        return "WEAK BEARISH"
    return "STRONG BEARISH"

def fetch_market_data(kite: KiteConnect) -> dict:
    symbols = ["NSE:NIFTY 50", "BSE:SENSEX", "NSE:NIFTY BANK"]
    result = {"nifty": {}, "sensex": {}, "banknifty": {}, "pcr": 0, "sentiment": "---", "ce_oi": 0, "pe_oi": 0, "time": ""}
    try:
        quotes = {}
        for sym in symbols:
            try:
                q = kite.quote(sym)[sym]
                quotes[sym] = q
            except Exception:
                continue
        labels = {"NSE:NIFTY 50": "nifty", "BSE:SENSEX": "sensex", "NSE:NIFTY BANK": "banknifty"}
        for sym, key in labels.items():
            q = quotes.get(sym, {})
            ohlc = q.get("ohlc", {})
            ltp = q.get("last_price", 0)
            net_chg = q.get("net_change", 0)
            prev = ohlc.get("close", 0)
            chg_pct = (net_chg / prev * 100) if prev else 0
            result[key] = {
                "spot": round(ltp, 2),
                "open": round(ohlc.get("open", 0), 2),
                "high": round(ohlc.get("high", 0), 2),
                "low": round(ohlc.get("low", 0), 2),
                "change": round(net_chg, 2),
                "change_pct": round(chg_pct, 2),
            }
        # PCR / Sentiment
        resp = kite.instruments("NFO")
        nifty_opts = [r for r in resp if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")]
        if nifty_opts:
            tsyms = [r["tradingsymbol"] for r in nifty_opts]
            ce_oi = pe_oi = 0
            for i in range(0, len(tsyms), 500):
                batch = tsyms[i:i+500]
                keys = [f"NFO:{s}" for s in batch]
                batch_q = kite.quote(keys)
                for k, v in batch_q.items():
                    if v.get("oi") is None:
                        continue
                    sym = k.replace("NFO:", "")
                    strikes = [r for r in nifty_opts if r["tradingsymbol"] == sym]
                    if not strikes:
                        continue
                    if strikes[0]["instrument_type"] == "CE":
                        ce_oi += v.get("oi", 0) or 0
                    else:
                        pe_oi += v.get("oi", 0) or 0
            result["ce_oi"] = ce_oi
            result["pe_oi"] = pe_oi
            pcr = pe_oi / ce_oi if ce_oi else 0
            result["pcr"] = round(pcr, 4)
            nifty_chg = result.get("nifty", {}).get("change_pct", 0)
            result["sentiment"] = classify_pcr(pcr, nifty_chg)
        result["time"] = datetime.now().strftime("%H:%M:%S")
    except Exception:
        pass
    return result


@app.route("/api/market")
@require_auth
def api_market():
    global _market_cache
    now = time.time()
    if now - _market_cache["time"] < 15 and _market_cache["data"]:
        return jsonify(_market_cache["data"])
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if not api_key or not access_token:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        _dash_rate_limit()
        kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=15)
        data = fetch_market_data(kite)
        _market_cache = {"data": data, "time": now}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
@require_auth
def api_trades():
    """Return today's trade summary: closed & running P&L, actual & estimated charges."""
    today = datetime.now().strftime("%Y-%m-%d")
    closed = {"total": 0, "pnl": 0.0, "charges": 0.0, "by_strategy": {}}
    if TRADE_LOG.exists():
        try:
            import csv
            with open(TRADE_LOG, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("date", "") == today:
                        pnl = float(row.get("pnl", 0))
                        closed["total"] += 1
                        closed["pnl"] += pnl
                        chg = row.get("charges", "0")
                        try:
                            closed["charges"] += float(chg)
                        except Exception:
                            pass
                        strat = row.get("strategy", "?")
                        closed["by_strategy"][strat] = closed["by_strategy"].get(strat, 0) + 1
        except Exception:
            pass

    # Live P&L from Zerodha positions
    live_pnl = None
    live_positions = 0
    running_charges = 0.0
    actual_open_tsyms: set[str] = set()
    cfg = load_config()
    api_key = cfg.get("api_key", "")
    access_token = cfg.get("access_token", "")
    if api_key and access_token:
        try:
            k = KiteConnect(api_key=api_key, access_token=access_token, timeout=10)
            all_pos = k.positions()
            live_pnl = 0.0
            for p in all_pos.get("net", []):
                live_pnl += float(p.get("pnl", 0))
                if int(p.get("quantity", 0)) != 0 and p.get("tradingsymbol"):
                    live_positions += 1
                    actual_open_tsyms.add(p["tradingsymbol"])
            for p in all_pos.get("day", []):
                if int(p.get("quantity", 0)) != 0 and p.get("tradingsymbol"):
                    actual_open_tsyms.add(p["tradingsymbol"])

            # Estimated charges for running positions: use current LTP
            day_positions = all_pos.get("net", [])
            if day_positions:
                # Group positions by exchange for batch quoting
                by_exchange: dict[str, list] = {}
                for p in day_positions:
                    if not p.get("tradingsymbol"):
                        continue
                    ex = p.get("exchange", "NFO")
                    by_exchange.setdefault(ex, []).append(p)
                running_legs = []
                for ex, pos_list in by_exchange.items():
                    tsyms = [f"{ex}:{p['tradingsymbol']}" for p in pos_list]
                    for i in range(0, len(tsyms), 500):
                        batch = tsyms[i:i+500]
                        try:
                            quotes = k.quote(batch)
                            for full_sym, q in quotes.items():
                                prefix = f"{ex}:"
                                if full_sym.startswith(prefix):
                                    sym = full_sym[len(prefix):]
                                else:
                                    sym = full_sym
                                matches = [p for p in pos_list if p["tradingsymbol"] == sym]
                                if matches:
                                    p = matches[0]
                                    action = "BUY" if int(p.get("quantity", 0)) > 0 else "SELL"
                                    prem = q.get("last_price", 0)
                                    running_legs.append({"action": action, "premium": prem})
                        except Exception:
                            pass
                if running_legs:
                    running_charges = calc_charges(running_legs, LOT_SIZE)

        except Exception:
            pass

    # Running trades — cross-reference config with actual open positions
    running = 0
    running_details = []

    # Collect tracked symbols from manual_trades config
    mt_tsyms: set[str] = set()
    if cfg.get("manual_trades"):
        trades = cfg["manual_trades"]
        if isinstance(trades, dict):
            trades = [trades]
        mt_tsyms = {t["tsym"] for t in trades if "tsym" in t}

    for key, label in [("position", "IC"), ("cs_position", "CS"), ("manual_trades", "MT")]:
        if key == "manual_trades":
            if mt_tsyms and (not actual_open_tsyms or any(t in actual_open_tsyms for t in mt_tsyms)):
                running += 1
                running_details.append(label)
        elif cfg.get(key):
            running += 1
            running_details.append(label)
    for s, proc in bot_processes.items():
        if proc and proc.poll() is None:
            short = {"ic":"IC","cs":"CS","sma":"SMA","mt":"MT","bnf":"BNF","n1h":"N1H","sw":"SW","sr":"SR","ratio":"RATIO"}.get(s, s.upper())
            if short not in running_details:
                running += 1
                running_details.append(short)

    total_charges = round(closed["charges"] + running_charges, 2)

    return jsonify({
        "date": today,
        "closed_trades": closed["total"],
        "closed_pnl": round(closed["pnl"], 2),
        "by_strategy": closed["by_strategy"],
        "actual_charges_closed": round(closed["charges"], 2),
        "est_charges_running": round(running_charges, 2),
        "total_charges": total_charges,
        "running_trades": running,
        "running_details": running_details,
        "live_pnl": round(live_pnl, 2) if live_pnl is not None else None,
        "live_positions": live_positions,
    })


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = load_config()
    return jsonify({
        "api_key": cfg.get("api_key", ""),
        "has_token": bool(cfg.get("access_token", "")),
    })


@app.route("/api/config", methods=["POST"])
@require_auth
def api_config_post():
    cfg = load_config()
    if request.form.get("api_key"):
        cfg["api_key"] = request.form["api_key"].strip()
    if request.form.get("api_secret"):
        cfg["api_secret"] = request.form["api_secret"].strip()
    dp = request.form.get("dashboard_password", "")
    if dp:
        cfg["dashboard_password"] = dp
    save_config(cfg)
    return redirect("/?msg=success:Settings saved")



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Dashboard: http://localhost:{port}")
    print()
    print("Set Redirect URL in Zerodha Developer portal for auto-capture:")
    print(f"  http://localhost:{port}/callback")
    print()
    print("Or use the manual token paste fallback on the dashboard.")
    print()
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true"))
