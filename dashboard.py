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

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
HEARTBEAT_FILE = BOT_DIR / ".bot_heartbeat.txt"
TRADE_LOG = BOT_DIR / "trade_log.csv"

STRATEGIES = ["ic", "cs", "sma", "mt", "bnf", "n1h", "sw", "sr", "ratio"]

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

PASSWORD_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard Login</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 14px; padding: 40px; width: 440px; }
  h1 { color: #58a6ff; font-size: 1.8rem; margin-bottom: 24px; text-align: center; }
  input { width: 100%; padding: 16px 20px; border-radius: 10px; border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 1.2rem; margin-bottom: 18px; }
  .btn { width: 100%; padding: 16px; border: none; border-radius: 10px; font-size: 1.2rem; font-weight: 600; cursor: pointer; background: #238636; color: #fff; }
  .btn:hover { background: #2ea043; }
  .error { color: #f85149; text-align: center; margin-top: 12px; font-size: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h1>&#128274; Dashboard Login</h1>
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Enter password" autofocus>
    <button type="submit" class="btn">Unlock</button>
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
}

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 30px; font-size: 28px; }
  .container { max-width: 100%; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 3rem; margin-bottom: 28px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 small { font-size: 1.3rem; color: #8b949e; font-weight: 400; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 14px; padding: 30px; margin-bottom: 28px; }
  .strategy-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; }
  .strat { background: #0d1117; border: 1px solid #30363d; border-radius: 14px; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .strat .name { font-size: 1.8rem; font-weight: 700; color: #58a6ff; }
  .strat .status { font-size: 1.4rem; }
  .strat .status .dot { display: inline-block; width: 16px; height: 16px; border-radius: 50%; margin-right: 8px; }
  .strat .status .dot.green { background: #3fb950; }
  .strat .status .dot.red { background: #f85149; }
  .strat .status .dot.gray { background: #484f58; }
  .strat .btn-row { display: flex; gap: 12px; }
  .strat .btn-row .btn { flex: 1; }
  .strat .mini-log { background: #0d1117; border: 1px solid #21262d; border-radius: 10px; padding: 14px; font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 1.2rem; max-height: 300px; overflow-y: auto; line-height: 1.6; color: #8b949e; }
  .strat .mini-log .hl { color: #c9d1d9; }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 16px 32px; border: none; border-radius: 12px; font-size: 1.4rem; font-weight: 600; cursor: pointer; }
  .btn-primary { background: #238636; color: #fff; }
  .btn-primary:hover { background: #2ea043; }
  .btn-primary:disabled { background: #1b5e2a; opacity: 0.5; cursor: not-allowed; }
  .btn-danger { background: #b71c1c; color: #fff; }
  .btn-danger:hover { background: #d32f2f; }
  .btn-danger:disabled { background: #7a1414; opacity: 0.5; cursor: not-allowed; }
  .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-secondary:hover { background: #30363d; }
  .btn-sm { padding: 4px 10px; font-size: 0.85rem; border-radius: 6px; min-width: auto; }
  .top-row { display: flex; gap: 18px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }
  .top-row .stat-box { background: #0d1117; border: 1px solid #30363d; border-radius: 10px; padding: 16px 28px; text-align: center; min-width: 160px; }
  .top-row .stat-box .label { font-size: 0.9rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  .top-row .stat-box .value { font-size: 1.6rem; font-weight: 700; margin-top: 6px; }
  .top-row .stat-box .value.green { color: #3fb950; }
  .top-row .stat-box .value.red { color: #f85149; }
  .top-row .stat-box .value.blue { color: #58a6ff; }
  .tab { display: none; }
  .tab.active { display: block; }
  .tab-bar { display: flex; gap: 6px; margin-bottom: 18px; }
  .tab-bar button { padding: 14px 28px; border: 1px solid #30363d; background: #0d1117; color: #8b949e; border-radius: 10px 10px 0 0; cursor: pointer; font-size: 1.1rem; font-weight: 600; }
  .tab-bar button.active { background: #161b22; color: #c9d1d9; border-bottom: 2px solid #58a6ff; }
  input { padding: 14px 20px; border-radius: 10px; border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 1.1rem; }
  input[type="text"], input[type="password"] { width: 100%; }
  .row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .col { display: flex; flex-direction: column; gap: 16px; }
  .flex-1 { flex: 1; }
  .mt-8 { margin-top: 8px; }
  .mb-8 { margin-bottom: 8px; }
  .msg { padding: 18px 22px; border-radius: 10px; margin: 12px 0; font-size: 1.1rem; line-height: 1.6; }
  .msg-success { background: #1b7e3a22; border: 1px solid #1b7e3a; color: #7ee787; }
  .msg-error { background: #b71c1c22; border: 1px solid #b71c1c; color: #f85149; }
  .msg-info { background: #1f6feb22; border: 1px solid #1f6feb; color: #58a6ff; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .text-xs { font-size: 0.95rem; color: #8b949e; }
  /* ── Tablet ── */
  @media (max-width: 1024px) {
    body { padding: 18px; font-size: 22px; }
    .strategy-grid { grid-template-columns: repeat(2, 1fr); gap: 18px; }
    .top-row .stat-box { min-width: 120px; padding: 12px 20px; }
    .top-row .stat-box .value { font-size: 1.5rem; }
    .card { padding: 22px; }
    .tab-bar button { padding: 12px 20px; font-size: 1.1rem; }
    h1 { font-size: 2.2rem; }
    .strat .name { font-size: 1.5rem; }
    .btn { padding: 14px 24px; font-size: 1.2rem; }
    .strat .mini-log { font-size: 1rem; }
  }
  /* ── Mobile ── */
  @media (max-width: 768px) {
    body { padding: 12px; font-size: 18px; }
    .container { padding: 0; }
    h1 { font-size: 1.8rem; margin-bottom: 16px; gap: 8px; }
    h1 small { font-size: 0.9rem; }
    .card { padding: 14px; margin-bottom: 14px; border-radius: 12px; }
    .strategy-grid { grid-template-columns: 1fr; gap: 14px; }
    .strat { padding: 16px; gap: 12px; }
    .strat .name { font-size: 1.3rem; }
    .strat .status { font-size: 1rem; }
    .strat .status .dot { width: 12px; height: 12px; }
    .strat .btn-row { gap: 8px; flex-wrap: wrap; }
    .strat .btn-row .btn { flex: 1; min-width: 80px; padding: 12px 14px; font-size: 1rem; }
    .strat .mini-log { font-size: 0.8rem; max-height: 150px; padding: 10px; }
    .top-row { gap: 8px; margin-bottom: 12px; }
    .top-row .stat-box { min-width: 80px; padding: 8px 10px; border-radius: 8px; flex: 1; }
    .top-row .stat-box .label { font-size: 0.75rem; }
    .top-row .stat-box .value { font-size: 1.2rem; margin-top: 4px; }
    .tab-bar { gap: 4px; overflow-x: auto; }
    .tab-bar button { padding: 8px 14px; font-size: 0.85rem; white-space: nowrap; }
    .btn { padding: 10px 16px; font-size: 0.9rem; border-radius: 8px; }
    input { padding: 10px 14px; font-size: 0.9rem; }
    .msg { padding: 12px 14px; font-size: 0.9rem; }
    .text-xs { font-size: 0.8rem; }
    .lots-row input { width: 50px !important; font-size: 0.85rem !important; }
  }
  /* ── Market data & summary grid responsive ── */
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
  .grid-5 { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
  @media (max-width: 1024px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); gap: 12px; }
    .grid-5 { grid-template-columns: repeat(3, 1fr); gap: 10px; }
  }
  @media (max-width: 768px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); gap: 8px; }
    .grid-5 { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  }
</style>
</head>
<body>
<div class="container">
  <h1>&#9889; Trading Bot <small>multi-strategy</small></h1>

  {% set msgs = request.args.get('msg','').split('|') if request.args.get('msg') else [] %}
  {% for m in msgs %}
    {% if m %}
    <div class="msg msg-{{ m.split(':')[0] }}">{{ m.split(':',1)[1] if ':' in m else m }}</div>
    {% endif %}
  {% endfor %}

  <!-- Top status bar -->
  <div class="card">
    <div class="top-row">
      <div class="stat-box"><div class="label">Heartbeat</div><div class="value" id="s-heartbeat">---</div></div>
      <div class="stat-box"><div class="label">Token</div><div class="value" id="s-token">---</div></div>
      <div class="stat-box"><div class="label">Running</div><div class="value blue" id="s-count">0/8</div><div style="font-size:1rem;color:#8b949e;margin-top:4px;" id="s-running-list"></div></div>
    </div>
  </div>

  <!-- Market Data card -->
  <div class="card">
    <div class="top-row" style="margin-bottom:12px;">
      <div style="font-size:1.2rem;font-weight:700;color:#58a6ff;">Market Data</div>
      <div style="font-size:0.85rem;color:#8b949e;" id="market-time"></div>
    </div>
    <div class="grid-4">
      <div class="stat-box" style="min-width:0;">
        <div class="label">NIFTY</div>
        <div class="value blue" style="font-size:1.5rem;" id="m-nifty-spot">---</div>
        <div style="font-size:1.2rem;margin-top:6px;"><span id="m-nifty-chg"></span></div>
        <div style="font-size:1.1rem;color:#8b949e;margin-top:4px;">O: <span id="m-nifty-open"></span> H: <span id="m-nifty-high"></span> L: <span id="m-nifty-low"></span></div>
        <div style="font-size:1.1rem;color:#d29922;margin-top:4px;">Range: <span id="m-nifty-range"></span></div>
      </div>
      <div class="stat-box" style="min-width:0;">
        <div class="label">SENSEX</div>
        <div class="value blue" style="font-size:1.5rem;" id="m-sensex-spot">---</div>
        <div style="font-size:1.2rem;margin-top:6px;"><span id="m-sensex-chg"></span></div>
        <div style="font-size:1.1rem;color:#8b949e;margin-top:4px;">O: <span id="m-sensex-open"></span> H: <span id="m-sensex-high"></span> L: <span id="m-sensex-low"></span></div>
        <div style="font-size:1.1rem;color:#d29922;margin-top:4px;">Range: <span id="m-sensex-range"></span></div>
      </div>
      <div class="stat-box" style="min-width:0;">
        <div class="label">BANK NIFTY</div>
        <div class="value blue" style="font-size:1.5rem;" id="m-banknifty-spot">---</div>
        <div style="font-size:1.2rem;margin-top:6px;"><span id="m-banknifty-chg"></span></div>
        <div style="font-size:1.1rem;color:#8b949e;margin-top:4px;">O: <span id="m-banknifty-open"></span> H: <span id="m-banknifty-high"></span> L: <span id="m-banknifty-low"></span></div>
        <div style="font-size:1.1rem;color:#d29922;margin-top:4px;">Range: <span id="m-banknifty-range"></span></div>
      </div>
      <div class="stat-box" style="min-width:0;">
        <div class="label">SENTIMENT (PCR)</div>
        <div class="value" style="font-size:2.2rem;font-weight:800;" id="m-pcr">---</div>
        <div style="font-size:1.3rem;font-weight:700;margin-top:6px;" id="m-sentiment"></div>
        <div style="font-size:1rem;color:#8b949e;margin-top:6px;">CE OI: <span id="m-ce-oi"></span> | PE OI: <span id="m-pe-oi"></span></div>
      </div>
    </div>
  </div>

  <!-- Today's Summary card -->
  <div class="card">
    <div class="top-row" style="margin-bottom:8px;">
      <div style="font-size:1.1rem;font-weight:700;color:#58a6ff;">Today's Summary</div>
      <div style="font-size:0.85rem;color:#8b949e;" id="trade-date"></div>
    </div>
    <div class="grid-5">
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">Running</div>
        <div class="value blue" style="font-size:1.3rem;" id="td-running">0</div>
        <div style="font-size:0.75rem;color:#8b949e;" id="td-running-list"></div>
      </div>
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">Closed</div>
        <div class="value blue" style="font-size:1.3rem;" id="td-closed">0</div>
        <div style="font-size:0.75rem;color:#8b949e;" id="td-closed-list"></div>
      </div>
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">P&L (closed)</div>
        <div class="value" style="font-size:1.3rem;" id="td-pnl">₹0</div>
        <div style="font-size:0.75rem;color:#8b949e;">today</div>
      </div>
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">Live P&L (Zerodha)</div>
        <div class="value" style="font-size:1.3rem;" id="td-live-pnl">₹0</div>
        <div style="font-size:0.75rem;color:#8b949e;" id="td-live-pos"></div>
      </div>
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">Est. Charges</div>
        <div class="value red" style="font-size:1.3rem;" id="td-charges">₹0</div>
        <div style="font-size:0.75rem;color:#8b949e;" id="td-charge-legs"></div>
      </div>
      <div class="stat-box" style="min-width:0;padding:12px 16px;">
        <div class="label">Net P&L</div>
        <div class="value" style="font-size:1.3rem;" id="td-net">₹0</div>
        <div style="font-size:0.75rem;color:#8b949e;">after charges</div>
      </div>
    </div>
  </div>

  <!-- Strategy cards -->
   <div class="strategy-grid" id="strat-grid">
    {% for sid in ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio'] %}
    <div class="strat" id="strat-{{ sid }}">
      <div class="name">{{ {'ic':'Iron Condor (NIFTY)','cs':'Credit Spread (NIFTY)','sma':'SMA Crossover (SENSEX)','mt':'Manual Trade (Trail SL)','bnf':'Bank Nifty 2H SMA(60)','n1h':'Nifty 1H SMA Options','sw':'Swing Scanner (Weekly)','sr':'Swing Rebalancer (Daily)','ratio':'NIFTYBEES/GOLDBEES Ratio'}[sid] }}</div>
      <div class="status"><span class="dot gray" id="dot-{{ sid }}"></span><span id="s-{{ sid }}">STOPPED</span> <span class="text-xs" id="resume-{{ sid }}" style="color:#d29922;display:none;">&#9888; Position saved</span></div>
      <div class="btn-row">
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
        <label style="font-size:0.9rem;color:#8b949e;">Lots:</label>
        <input type="number" id="lots-input-{{ sid }}" value="1" min="1" max="10" style="width:60px;padding:6px 8px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;font-size:1rem;">
        {% endif %}
      </div>
      {% if sid == 'mt' %}
      <div id="mt-positions" style="display:none;margin-top:8px;"></div>
      {% endif %}
      <div class="mini-log" id="log-{{ sid }}"><div class="hl">Waiting...</div></div>
    </div>
    {% endfor %}
  </div>
  <script>
    document.addEventListener('DOMContentLoaded', function() {
      for (const s of ['sw','sr']) {
        var el = document.getElementById('lots-' + s);
        if (el) el.style.display = 'none';
      }
    });

    var mtPositions = [];

    async function scanMt() {
      const r = await fetch('/api/mt-scan', {method:'POST'});
      const d = await r.json();
      const container = document.getElementById('mt-positions');
      if (!d.ok) { container.innerHTML = '<div style="color:#f85149;">Error: ' + d.error + '</div>'; container.style.display='block'; return; }
      if (!d.positions.length) { container.innerHTML = '<div>No positions found.</div>'; container.style.display='block'; return; }
      mtPositions = d.positions;
      let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse;">';
      html += '<tr style="color:#8b949e;"><th style="padding:4px;text-align:left;">Side</th><th style="padding:4px;text-align:left;">Symbol</th><th style="padding:4px;text-align:right;">Qty</th><th style="padding:4px;text-align:right;">Avg</th><th style="padding:4px;text-align:right;">LTP</th><th style="padding:4px;text-align:right;">SL price</th><th style="padding:4px;"></th></tr>';
      for (let i = 0; i < d.positions.length; i++) {
        const p = d.positions[i];
        html += '<tr>';
        html += '<td style="padding:4px;">'+p.side+'</td>';
        html += '<td style="padding:4px;max-width:140px;overflow:hidden;text-overflow:ellipsis;">'+p.tsym+'</td>';
        html += '<td style="padding:4px;text-align:right;">'+p.qty+'</td>';
        html += '<td style="padding:4px;text-align:right;">₹'+p.avg_price.toFixed(2)+'</td>';
        html += '<td style="padding:4px;text-align:right;">₹'+p.ltp.toFixed(2)+'</td>';
        html += '<td style="padding:4px;text-align:right;"><input type="number" id="mt-sl-'+i+'" value="" placeholder="SL price" min="0.01" step="0.01" max="99999" style="width:70px;padding:4px;border-radius:4px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;"></td>';
        html += '<td style="padding:4px;"><button class="btn btn-sm" onclick="setMt('+i+')" id="mt-set-'+i+'">Set</button></td>';
        html += '</tr>';
      }
      html += '</table>';
      container.innerHTML = html;
      container.style.display = 'block';
    }

    async function setMt(i) {
      const p = mtPositions[i];
      if (!p) return;
      const slInput = document.getElementById('mt-sl-'+i);
      const sl_price = parseFloat(slInput.value);
      if (!sl_price || sl_price <= 0) { alert('Enter a valid SL price'); return; }
      const status = await (await fetch('/api/status')).json();
      const running = status.strategies && status.strategies['mt'];
      const endpoint = running ? '/api/mt-add' : '/api/mt-start';
      const r = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({trades:[{...p, sl_price}]})});
      const d = await r.json();
      if (!d.ok) { alert('Error: ' + d.error); return; }
      const btn = document.getElementById('mt-set-'+i);
      if (btn) { btn.textContent = '✓'; btn.disabled = true; btn.style.opacity = '0.6'; }
      await fetchStatus();
    }
  </script>

  <!-- Settings tabs -->
  <div class="card">
    <div class="tab-bar">
      <button class="active" data-tab="tab-login">Login</button>
      <button data-tab="tab-api">API Keys</button>
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
        <div class="msg msg-info">
          API Key not configured. Go to <strong>API Keys</strong> tab.
        </div>
      {% endif %}
    </div>

    <div id="tab-api" class="tab">
      <form method="POST" action="/api/config" class="col">
        <input type="text" name="api_key" placeholder="API Key" value="{{ api_key }}">
        <input type="password" name="api_secret" placeholder="API Secret">
        <input type="password" name="dashboard_password" placeholder="Dashboard Password (leave blank to disable)">
        <button type="submit" class="btn btn-primary">Save</button>
      </form>
      <div class="mt-8 text-xs">&#128274; Set a dashboard password to protect controls. <a href="/logout">Logout</a></div>
    </div>
  </div>

</div>

<script>
function $(id) { return document.getElementById(id); }

document.querySelectorAll('[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    $(btn.dataset.tab).classList.add('active');
  });
});

async function action(strategy, cmd) {
  if (cmd === 'start' || cmd === 'resume') {
    var extra = '';
    var inp = document.getElementById('lots-input-' + strategy);
    if (inp) extra += '&lots=' + (parseInt(inp.value) || 1);
    var sl_inp = document.getElementById('sl-input-' + strategy);
    if (sl_inp) extra += '&sl=' + (parseInt(sl_inp.value) || 50);
    await fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'strategy=' + strategy + '&resume=' + (cmd === 'resume' ? '1' : '0') + extra });
  } else {
    await fetch('/api/stop', { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'strategy=' + strategy });
  }
  await fetchStatus();
}

async function fetchStatus() {
  try {
    const d = await (await fetch('/api/status')).json();
    const hb = d.heartbeat || '---';
    $('s-heartbeat').textContent = hb.length > 30 ? hb.slice(0, 28) + '...' : hb;
    const tk = $('s-token');
    if (d.token_ok) { tk.textContent = 'OK'; tk.className = 'value green'; }
    else if (d.token_ok === false) { tk.textContent = 'EXPIRED'; tk.className = 'value red'; }
    else { tk.textContent = '---'; tk.className = 'value'; }
    let count = 0;
    var runningNames = [];
    for (const s of ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio']) {
      const running = d.strategies && d.strategies[s];
      if (running) { count++; runningNames.push(s.toUpperCase()); }
      const dot = $('dot-' + s);
      dot.className = 'dot ' + (running ? 'green' : 'red');
      $('s-' + s).textContent = running ? 'RUNNING' : 'STOPPED';
      const startBtn = $('start-' + s);
      if (startBtn) startBtn.disabled = running;
      $('stop-' + s).disabled = !running;
      const ri = $('resume-' + s);
      const rb = $('resume-btn-' + s);
      if (d.positions && d.positions[s]) {
        ri.style.display = 'inline';
        ri.title = 'Expiry: ' + d.positions[s].expiry + ' | Credit: ?' + d.positions[s].entry_credit;
        if (!running) { rb.style.display = 'inline'; }
      } else {
        ri.style.display = 'none';
        rb.style.display = 'none';
      }
      if (s === 'mt') {
        const scanBtn = $('scan-mt');
        if (scanBtn) scanBtn.style.display = 'inline-block';
      }
    }
    $('s-count').textContent = count + '/9';
    $('s-running-list').textContent = runningNames.length ? runningNames.join(', ') : 'none';
  } catch(e) {}
}
setInterval(fetchStatus, 5000);
fetchStatus();

async function fetchLogs() {
  try {
    for (const s of ['ic','cs','sma','mt','bnf','n1h','sw','sr','ratio']) {
      const d = await (await fetch('/api/log?strategy=' + s)).json();
      const box = $('log-' + s);
      if (d.output && d.output.length) {
        box.innerHTML = d.output.slice(-30).map(l => '<div>' + l.replace(/</g,'&lt;') + '</div>').join('');
        box.scrollTop = box.scrollHeight;
      }
    }
  } catch(e) {}
}
setInterval(fetchLogs, 5000);
fetchLogs();

async function fetchMarket() {
  try {
    const d = await (await fetch('/api/market')).json();
    if (d.error) return;
    const green = d.nifty && d.nifty.change >= 0;
    for (const [prefix, key] of [['nifty','nifty'],['sensex','sensex'],['banknifty','banknifty']]) {
      const v = d[key];
      if (!v) continue;
      const spotEl = $('m-' + prefix + '-spot');
      spotEl.textContent = v.spot.toLocaleString('en-IN', {minimumFractionDigits:2});
      spotEl.className = 'value ' + (v.change >= 0 ? 'green' : 'red');
      $('m-' + prefix + '-chg').textContent = (v.change >= 0 ? '+' : '') + v.change.toFixed(2) + ' (' + (v.change_pct >= 0 ? '+' : '') + v.change_pct.toFixed(2) + '%)';
      $('m-' + prefix + '-chg').style.color = v.change >= 0 ? '#3fb950' : '#f85149';
      $('m-' + prefix + '-open').textContent = v.open.toLocaleString('en-IN', {minimumFractionDigits:2});
      $('m-' + prefix + '-high').textContent = v.high.toLocaleString('en-IN', {minimumFractionDigits:2});
      $('m-' + prefix + '-low').textContent = v.low.toLocaleString('en-IN', {minimumFractionDigits:2});
      var range = v.high - v.low;
      $('m-' + prefix + '-range').textContent = range.toLocaleString('en-IN', {minimumFractionDigits:2}) + ' pts';
    }
    if (d.pcr) {
      $('m-pcr').textContent = d.pcr.toFixed(2);
      $('m-pcr').className = 'value';
      $('m-pcr').style.fontSize = '1.5rem';
      let sentColor = '#d29922';
      if (d.sentiment.includes('STRONG BEAR')) sentColor = '#f85149';
      else if (d.sentiment.includes('BEARISH')) sentColor = '#da3633';
      else if (d.sentiment.includes('WEAK BEAR')) sentColor = '#d29922';
      else if (d.sentiment.includes('WEAK BULL')) sentColor = '#58a6ff';
      else if (d.sentiment.includes('BULLISH') || d.sentiment.includes('STRONG BULL')) sentColor = '#3fb950';
      $('m-pcr').style.color = sentColor;
      $('m-sentiment').textContent = d.sentiment;
      $('m-sentiment').style.color = sentColor;
      $('m-ce-oi').textContent = (d.ce_oi / 1e7).toFixed(2) + 'Cr';
      $('m-pe-oi').textContent = (d.pe_oi / 1e7).toFixed(2) + 'Cr';
    }
    $('market-time').textContent = 'Updated: ' + (d.time || '');
  } catch(e) {}
}
setInterval(fetchMarket, 15000);
fetchMarket();

async function fetchTrades() {
  try {
    const d = await (await fetch('/api/trades')).json();
    if (d.error) return;
    $('trade-date').textContent = d.date;
    $('td-running').textContent = d.running_trades;
    $('td-running-list').textContent = d.running_details.join(', ') || 'none';
    $('td-closed').textContent = d.closed_trades;
    const stratList = Object.entries(d.by_strategy).map(([k,v]) => k + ':' + v).join(', ');
    $('td-closed-list').textContent = stratList || 'none';
    const isGreen = d.closed_pnl >= 0;
    $('td-pnl').textContent = '₹' + d.closed_pnl.toLocaleString('en-IN', {minimumFractionDigits:2});
    $('td-pnl').className = 'value';
    $('td-pnl').style.fontSize = '1.3rem';
    $('td-pnl').style.color = isGreen ? '#3fb950' : '#f85149';
    // Live P&L from Zerodha
    const livePnl = d.live_pnl;
    if (livePnl !== undefined) {
      const liveGreen = livePnl >= 0;
      $('td-live-pnl').textContent = '₹' + livePnl.toLocaleString('en-IN', {minimumFractionDigits:2});
      $('td-live-pnl').style.color = liveGreen ? '#3fb950' : '#f85149';
      $('td-live-pos').textContent = (d.live_positions || 0) + ' positions';
    }
    $('td-charges').textContent = '₹' + d.estimated_charges.toLocaleString('en-IN');
    $('td-charge-legs').textContent = d.charge_legs + ' legs @ ₹50/leg';
    const net = d.closed_pnl - d.estimated_charges;
    $('td-net').textContent = '₹' + net.toLocaleString('en-IN', {minimumFractionDigits:2});
    $('td-net').className = 'value';
    $('td-net').style.fontSize = '1.3rem';
    $('td-net').style.color = net >= 0 ? '#3fb950' : '#f85149';
  } catch(e) {}
}
setInterval(fetchTrades, 10000);
fetchTrades();

$('btn-login')?.addEventListener('click', () => {
  $('login-hint').style.display = 'block';
});

document.querySelectorAll('.msg-success, .msg-error').forEach(m => setTimeout(() => m.remove(), 8000));
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
    if cfg.get("manual_trades"):
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


_market_cache = {"data": None, "time": 0}

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
            if pcr > 1.3:
                result["sentiment"] = "STRONG BEARISH"
            elif pcr > 1.15:
                result["sentiment"] = "BEARISH"
            elif pcr > 1.0:
                result["sentiment"] = "WEAK BEARISH"
            elif pcr > 0.85:
                result["sentiment"] = "WEAK BULLISH"
            elif pcr > 0.7:
                result["sentiment"] = "BULLISH"
            else:
                result["sentiment"] = "STRONG BULLISH"
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
        kite = KiteConnect(api_key=api_key, access_token=access_token, timeout=15)
        data = fetch_market_data(kite)
        _market_cache = {"data": data, "time": now}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
@require_auth
def api_trades():
    """Return today's trade summary: count, P&L, charges, running trades."""
    today = datetime.now().strftime("%Y-%m-%d")
    closed = {"total": 0, "pnl": 0.0, "by_strategy": {}, "legs": 0}
    if TRADE_LOG.exists():
        try:
            import csv
            with open(TRADE_LOG, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("date", "") == today:
                        pnl = float(row.get("pnl", 0))
                        closed["total"] += 1
                        closed["pnl"] += pnl
                        strat = row.get("strategy", "?")
                        closed["by_strategy"][strat] = closed["by_strategy"].get(strat, 0) + 1
                        # Estimate legs per trade type
                        if strat == "IC":
                            closed["legs"] += 4
                        elif strat and strat.startswith("CS"):
                            closed["legs"] += 2
                        else:
                            closed["legs"] += 1
        except Exception:
            pass

    # Charges estimate (Zerodha): ~Rs 50 per option leg (brokerage + STT + taxes)
    est_charge_per_leg = 50
    total_charges = closed["legs"] * est_charge_per_leg

    # Live P&L from Zerodha positions
    live_pnl = None
    live_positions = 0
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
        except Exception:
            pass

    # Running trades from config + dashboard processes
    running = 0
    running_details = []
    for key, label in [("position", "IC"), ("cs_position", "CS"), ("manual_trades", "MT")]:
        if cfg.get(key):
            running += 1
            running_details.append(label)
    for s, proc in bot_processes.items():
        if proc and proc.poll() is None:
            short = {"ic":"IC","cs":"CS","sma":"SMA","mt":"MT","bnf":"BNF","n1h":"N1H","sw":"SW","sr":"SR","ratio":"RATIO"}.get(s, s.upper())
            if short not in running_details:
                running += 1
                running_details.append(short)

    return jsonify({
        "date": today,
        "closed_trades": closed["total"],
        "closed_pnl": round(closed["pnl"], 2),
        "by_strategy": closed["by_strategy"],
        "estimated_charges": total_charges,
        "charge_legs": closed["legs"],
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
