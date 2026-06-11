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
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request
from kiteconnect import KiteConnect

BOT_DIR = Path(__file__).parent
CONFIG_FILE = BOT_DIR / "kite_config.json"
HEARTBEAT_FILE = BOT_DIR / ".bot_heartbeat.txt"

STRATEGIES = ["ic", "cs", "sma"]

app = Flask(__name__)
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
}

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; font-size: 18px; }
  .container { max-width: 100%; margin: 0 auto; }
  h1 { color: #58a6ff; font-size: 1.8rem; margin-bottom: 24px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  h1 small { font-size: 0.85rem; color: #8b949e; font-weight: 400; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 24px; margin-bottom: 20px; }
  .strategy-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .strat { background: #0d1117; border: 1px solid #30363d; border-radius: 10px; padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .strat .name { font-size: 1.1rem; font-weight: 700; color: #58a6ff; }
  .strat .status { font-size: 0.9rem; }
  .strat .status .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }
  .strat .status .dot.green { background: #3fb950; }
  .strat .status .dot.red { background: #f85149; }
  .strat .status .dot.gray { background: #484f58; }
  .strat .btn-row { display: flex; gap: 8px; }
  .strat .btn-row .btn { flex: 1; }
  .strat .mini-log { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 8px; font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace; font-size: 0.7rem; max-height: 120px; overflow-y: auto; line-height: 1.5; color: #8b949e; }
  .strat .mini-log .hl { color: #c9d1d9; }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 20px; border: none; border-radius: 8px; font-size: 0.9rem; font-weight: 600; cursor: pointer; }
  .btn-primary { background: #238636; color: #fff; }
  .btn-primary:hover { background: #2ea043; }
  .btn-primary:disabled { background: #1b5e2a; opacity: 0.5; cursor: not-allowed; }
  .btn-danger { background: #b71c1c; color: #fff; }
  .btn-danger:hover { background: #d32f2f; }
  .btn-danger:disabled { background: #7a1414; opacity: 0.5; cursor: not-allowed; }
  .btn-secondary { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-secondary:hover { background: #30363d; }
  .top-row { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
  .top-row .stat-box { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px 20px; text-align: center; min-width: 120px; }
  .top-row .stat-box .label { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  .top-row .stat-box .value { font-size: 1.2rem; font-weight: 700; margin-top: 4px; }
  .top-row .stat-box .value.green { color: #3fb950; }
  .top-row .stat-box .value.red { color: #f85149; }
  .top-row .stat-box .value.blue { color: #58a6ff; }
  .tab { display: none; }
  .tab.active { display: block; }
  .tab-bar { display: flex; gap: 4px; margin-bottom: 14px; }
  .tab-bar button { padding: 10px 22px; border: 1px solid #30363d; background: #0d1117; color: #8b949e; border-radius: 8px 8px 0 0; cursor: pointer; font-size: 0.95rem; font-weight: 600; }
  .tab-bar button.active { background: #161b22; color: #c9d1d9; border-bottom: 2px solid #58a6ff; }
  input { padding: 12px 16px; border-radius: 8px; border: 1px solid #30363d; background: #0d1117; color: #c9d1d9; font-size: 1rem; }
  input[type="text"], input[type="password"] { width: 100%; }
  .row { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
  .col { display: flex; flex-direction: column; gap: 12px; }
  .flex-1 { flex: 1; }
  .mt-8 { margin-top: 8px; }
  .mb-8 { margin-bottom: 8px; }
  .msg { padding: 14px 18px; border-radius: 8px; margin: 10px 0; font-size: 0.95rem; line-height: 1.5; }
  .msg-success { background: #1b7e3a22; border: 1px solid #1b7e3a; color: #7ee787; }
  .msg-error { background: #b71c1c22; border: 1px solid #b71c1c; color: #f85149; }
  .msg-info { background: #1f6feb22; border: 1px solid #1f6feb; color: #58a6ff; }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .text-xs { font-size: 0.85rem; color: #8b949e; }
  @media (max-width: 768px) {
    body { padding: 10px; }
    .strategy-grid { grid-template-columns: 1fr; gap: 12px; }
    .top-row .stat-box { min-width: 80px; padding: 8px 12px; }
    .top-row .stat-box .value { font-size: 1rem; }
    .card { padding: 12px; }
    .tab-bar button { padding: 6px 10px; font-size: 0.78rem; }
    .strat .mini-log { font-size: 0.6rem; max-height: 80px; }
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
      <div class="stat-box"><div class="label">Running</div><div class="value blue" id="s-count">0/3</div></div>
    </div>
  </div>

  <!-- Strategy cards -->
  <div class="strategy-grid" id="strat-grid">
    {% for sid in ['ic','cs','sma'] %}
    <div class="strat" id="strat-{{ sid }}">
      <div class="name">{{ {'ic':'Iron Condor (NIFTY)','cs':'Credit Spread (NIFTY)','sma':'SMA Crossover (SENSEX)'}[sid] }}</div>
      <div class="status"><span class="dot gray" id="dot-{{ sid }}"></span><span id="s-{{ sid }}">STOPPED</span></div>
      <div class="btn-row">
        <button class="btn btn-primary" id="start-{{ sid }}" onclick="action('{{ sid }}','start')">&#9654; Start</button>
        <button class="btn btn-danger" id="stop-{{ sid }}" onclick="action('{{ sid }}','stop')" disabled>&#9632; Stop</button>
      </div>
      <div class="mini-log" id="log-{{ sid }}"><div class="hl">Waiting...</div></div>
    </div>
    {% endfor %}
  </div>

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
        <button type="submit" class="btn btn-primary">Save Keys</button>
      </form>
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
  if (cmd === 'start') {
    await fetch('/api/start', { method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'strategy=' + strategy });
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
    for (const s of ['ic','cs','sma']) {
      const running = d.strategies && d.strategies[s];
      if (running) count++;
      const dot = $('dot-' + s);
      dot.className = 'dot ' + (running ? 'green' : 'red');
      $('s-' + s).textContent = running ? 'RUNNING' : 'STOPPED';
      $('start-' + s).disabled = running;
      $('stop-' + s).disabled = !running;
    }
    $('s-count').textContent = count + '/3';
  } catch(e) {}
}
setInterval(fetchStatus, 5000);
fetchStatus();

async function fetchLogs() {
  try {
    for (const s of ['ic','cs','sma']) {
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

$('btn-login')?.addEventListener('click', () => {
  $('login-hint').style.display = 'block';
});

document.querySelectorAll('.msg-success, .msg-error').forEach(m => setTimeout(() => m.remove(), 8000));
</script>
</body>
</html>"""


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
        has_token=has_token,
        has_api_key=has_api_key,
        api_key=api_key,
        login_url=login_url,
    )


@app.route("/callback")
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
def api_start():
    strategy = request.form.get("strategy", "")
    if strategy not in STRATEGIES:
        return jsonify({"ok": False, "error": "Invalid strategy"}), 400
    proc = bot_processes.get(strategy)
    if proc and proc.poll() is None:
        return jsonify({"ok": False, "error": f"{strategy.upper()} already running"}), 400
    cmd = [sys.executable, "-u", str(BOT_DIR / "iron_condor_algo.py"), f"--strategy={strategy}"]
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
    return jsonify({"ok": True, "strategy": strategy})


@app.route("/api/status")
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
    return jsonify({
        "strategies": running,
        "heartbeat": heartbeat,
        "token_ok": token_ok,
        "has_api_key": bool(api_key),
    })


@app.route("/api/log")
def api_log():
    strategy = request.args.get("strategy", "ic")
    if strategy not in STRATEGIES:
        strategy = "ic"
    with bot_output_locks[strategy]:
        out = list(bot_outputs[strategy])
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
