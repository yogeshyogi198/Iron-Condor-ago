"""
Iron Condor Bot — Web Dashboard
================================
Run:  python dashboard.py
Then: Open http://localhost:5000 in browser

Features:
  - Auto-capture Zerodha OAuth token (set Redirect URL to http://localhost:5000/callback)
  - Manual token entry fallback
  - Select strategy & start/stop bot
  - Live status monitoring with bot console output
"""

import json
import os
import signal
import subprocess
import sys
import threading
import csv
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request
from kiteconnect import KiteConnect

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
HEARTBEAT_FILE = BOT_DIR / ".bot_heartbeat.txt"
LOCK_FILE = BOT_DIR / ".bot.lock"
TRADE_LOG = BOT_DIR / "trade_log.csv"

app = Flask(__name__)
bot_process: subprocess.Popen | None = None
bot_strategy: str = "ic"
bot_output: list[str] = []
bot_output_lock = threading.Lock()


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _reader_thread(proc: subprocess.Popen):
    """Read subprocess stdout line by line into bot_output list."""
    global bot_output
    try:
        for line in iter(proc.stdout.readline, ""):
            with bot_output_lock:
                bot_output.append(line.rstrip())
                if len(bot_output) > 200:
                    bot_output = bot_output[-200:]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Iron Condor Bot</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; font-size: 18px; }
  .container { max-width: 100%; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 1.8rem; margin-bottom: 24px; display: flex; align-items: center; gap: 12px; }
  h1 small { font-size: 0.85rem; color: #8b949e; font-weight: 400; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px; margin-bottom: 20px; }
  .status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  .stat { text-align: center; padding: 18px 12px; background: #0d1117; border-radius: 8px; }
  .stat .label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat .value { font-size: 1.6rem; font-weight: 700; margin-top: 6px; }
  .stat .value.green { color: #3fb950; }
  .stat .value.red { color: #f85149; }
  .stat .value.blue { color: #58a6ff; }
  .stat .value.yellow { color: #d29922; }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 12px 28px; border: none; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; }
  .btn-primary { background: #238636; color: #fff; }
  .btn-primary:hover { background: #2ea043; }
  .btn-danger { background: #b71c1c; color: #fff; }
  .btn-danger:hover { background: #d32f2f; }
  .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-secondary:hover { background: #30363d; }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  select, input { padding: 12px 16px; border-radius: 8px; border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 1rem; }
  select { min-width: 200px; }
  input[type="text"], input[type="password"] { width: 100%; }
  .row { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  .col { display: flex; flex-direction: column; gap: 12px; }
  .flex-1 { flex: 1; }
  .mt-8 { margin-top: 8px; }
  .mb-8 { margin-bottom: 8px; }
  .w-full { width: 100%; }
  .msg { padding: 14px 18px; border-radius: 8px; margin: 10px 0; font-size: 0.95rem; line-height: 1.5; }
  .msg-success { background: #1b7e3a22; border: 1px solid #1b7e3a; color: #7ee787; }
  .msg-error { background: #b71c1c22; border: 1px solid #b71c1c; color: #f85149; }
  .msg-info { background: #1f6feb22; border: 1px solid #1f6feb; color: #58a6ff; }
  .log-box { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 14px; font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 0.9rem; max-height: 350px; overflow-y: auto; margin-top: 10px; line-height: 1.6; }
  .log-box div { padding: 4px 0; border-bottom: 1px solid #21262d; }
  .log-box .ts { color: #8b949e; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .tab { display: none; }
  .tab.active { display: block; }
  .tab-bar { display: flex; gap: 4px; margin-bottom: 14px; }
  .tab-bar button { padding: 10px 22px; border: 1px solid #30363d; background: #0d1117; color: #8b949e; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.95rem; font-weight: 600; }
  .tab-bar button.active { background: #161b22; color: #c9d1d9; border-bottom: 2px solid #58a6ff; }
  .text-xs { font-size: 0.85rem; color: #8b949e; }
  @media (max-width: 640px) {
    body { padding: 10px; }
    .container { max-width: 100%; }
    .status-grid { grid-template-columns: 1fr 1fr; gap: 6px; }
    .stat { padding: 10px 4px; }
    .stat .value { font-size: 1rem; }
    .card { padding: 12px; }
    .row { flex-direction: column; align-items: stretch; }
    .row .btn { width: 100%; justify-content: center; }
    select { width: 100%; }
    .tab-bar button { padding: 6px 10px; font-size: 0.78rem; }
    .log-box { font-size: 0.7rem; max-height: 200px; }
  }
</style>
</head>
<body>
<div class="container">
  <h1>&#9889; Iron Condor Bot <small>v1.0</small></h1>

  {% set msgs = request.args.get('msg','').split('|') if request.args.get('msg') else [] %}
  {% for m in msgs %}
    {% if m %}
    <div class="msg msg-{{ m.split(':')[0] }}">{{ m.split(':',1)[1] if ':' in m else m }}</div>
    {% endif %}
  {% endfor %}

  <!-- Status -->
  <div class="card">
    <div class="status-grid" id="status-grid">
      <div class="stat"><div class="label">Bot</div><div class="value" id="s-status">—</div></div>
      <div class="stat"><div class="label">Strategy</div><div class="value blue" id="s-strategy">{{ strategy or '—' }}</div></div>
      <div class="stat"><div class="label">Heartbeat</div><div class="value" id="s-heartbeat">—</div></div>
      <div class="stat"><div class="label">Token</div><div class="value" id="s-token">—</div></div>
    </div>
  </div>

  <!-- Controls -->
  <div class="card">
    <div class="row">
      <select id="sel-strategy" style="flex:1;">
        <option value="ic" {{ 'selected' if strategy == 'ic' }}>Iron Condor (NIFTY)</option>
        <option value="cs" {{ 'selected' if strategy == 'cs' }}>Credit Spread (NIFTY)</option>
        <option value="sma" {{ 'selected' if strategy == 'sma' }}>SMA Crossover (SENSEX)</option>
      </select>
      <div class="row" style="flex:1;">
        <button class="btn btn-primary flex-1" id="btn-start" onclick="action('start')">&#9654; Start</button>
        <button class="btn btn-danger flex-1" id="btn-stop" onclick="action('stop')" disabled>&#9632; Stop</button>
      </div>
    </div>
  </div>

  <!-- Settings tabs -->
  <div class="card">
    <div class="tab-bar">
      <button class="active" data-tab="tab-login">Login</button>
      <button data-tab="tab-api">API Keys</button>
    </div>

    <!-- Login tab -->
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

    <!-- API Keys tab -->
    <div id="tab-api" class="tab">
      <form method="POST" action="/api/config" class="col">
        <input type="text" name="api_key" placeholder="API Key" value="{{ api_key }}">
        <input type="password" name="api_secret" placeholder="API Secret">
        <button type="submit" class="btn btn-primary">Save Keys</button>
      </form>
    </div>
  </div>

  <!-- Console log -->
  <div class="card">
    <div class="log-box" id="log-box">
      <div class="ts">Waiting for output...</div>
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

async function action(cmd) {
  if (cmd === 'start') {
    const strategy = $('sel-strategy').value;
    await fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'strategy=' + strategy });
  } else {
    await fetch('/api/stop', { method: 'POST' });
  }
  await fetchStatus();
}

async function fetchStatus() {
  try {
    const d = await (await fetch('/api/status')).json();
    const s = $('s-status');
    s.textContent = d.bot_running ? 'RUNNING' : 'STOPPED';
    s.className = 'value ' + (d.bot_running ? 'green' : 'red');
    $('s-strategy').textContent = d.bot_strategy || '\u2014';
    const hb = d.heartbeat || '\u2014';
    $('s-heartbeat').textContent = hb.length > 30 ? hb.slice(0, 28) + '\u2026' : hb;
    const tk = $('s-token');
    if (d.token_ok) { tk.textContent = '\u2713 OK'; tk.className = 'value green'; }
    else if (d.token_ok === false) { tk.textContent = '\u2717 EXPIRED'; tk.className = 'value red'; }
    else { tk.textContent = '\u2014'; tk.className = 'value'; }
    $('btn-start').disabled = d.bot_running;
    $('btn-stop').disabled = !d.bot_running;
  } catch(e) {}
}
setInterval(fetchStatus, 5000);
fetchStatus();

async function fetchLog() {
  try {
    const d = await (await fetch('/api/log')).json();
    const box = $('log-box');
    if (d.output && d.output.length) {
      box.innerHTML = d.output.slice(-50).map(l => `<div><span class="ts">${l}</span></div>`).join('');
      box.scrollTop = box.scrollHeight;
    }
  } catch(e) {}
}
setInterval(fetchLog, 5000);
fetchLog();

$('btn-login')?.addEventListener('click', () => {
  $('login-hint').style.display = 'block';
});

document.querySelectorAll('.msg-success, .msg-error').forEach(m => setTimeout(() => m.remove(), 8000));
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
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
        strategy=bot_strategy,
        has_token=has_token,
        has_api_key=has_api_key,
        api_key=api_key,
        login_url=login_url,
    )


@app.route("/callback")
def callback():
    """OAuth callback from Zerodha — auto-captures request_token."""
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
def api_token():
    """Manually extract and save access_token from redirect URL."""
    raw = request.form.get("redirect_url", "").strip()
    if not raw:
        return redirect("/?msg=error:No URL provided")

    # Extract request_token from URL
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
def api_start():
    global bot_process, bot_strategy, bot_output
    if bot_process and bot_process.poll() is None:
        return jsonify({"ok": False, "error": "Bot already running"}), 400

    strategy = request.form.get("strategy", "ic")
    bot_strategy = strategy
    bot_output = []

    # Remove stale lock file
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()

    cmd = [sys.executable, "-u", str(BOT_DIR / "iron_condor_algo.py"), f"--strategy={strategy}"]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    bot_process = subprocess.Popen(
        cmd,
        cwd=str(BOT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    # Start reader thread
    t = threading.Thread(target=_reader_thread, args=(bot_process,), daemon=True)
    t.start()

    return jsonify({"ok": True, "strategy": strategy})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global bot_process
    if not bot_process or bot_process.poll() is not None:
        return jsonify({"ok": False, "error": "Bot not running"}), 400

    if os.name == "nt":
        bot_process.terminate()
    else:
        os.kill(bot_process.pid, signal.SIGTERM)

    try:
        bot_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        bot_process.kill()
        bot_process.wait()

    if LOCK_FILE.exists():
        LOCK_FILE.unlink()

    bot_process = None
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    global bot_process, bot_strategy
    running = bot_process is not None and bot_process.poll() is None

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

    return jsonify({
        "bot_running": running,
        "bot_strategy": bot_strategy if running else "",
        "heartbeat": heartbeat,
        "token_ok": token_ok,
        "has_api_key": bool(api_key),
    })


@app.route("/api/log")
def api_log():
    global bot_output
    with bot_output_lock:
        out = list(bot_output)
    return jsonify({"output": out[-100:]})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        cfg = load_config()
        if request.form.get("api_key"):
            cfg["api_key"] = request.form["api_key"].strip()
        if request.form.get("api_secret"):
            cfg["api_secret"] = request.form["api_secret"].strip()
        save_config(cfg)
        return redirect("/?msg=success:API keys saved")
    cfg = load_config()
    return jsonify({
        "api_key": cfg.get("api_key", ""),
        "has_token": bool(cfg.get("access_token", "")),
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
    app.run(host=host, port=port, debug=True)
