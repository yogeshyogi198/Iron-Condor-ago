"""
cleanup_live_launch.py — Wipe all test data for fresh Day 1 live trading.
Preserves kite_config.json (API keys, tokens) intact.
Creates timestamped backup before deletion.
Run ONCE before tomorrow morning:
    python cleanup_live_launch.py
"""

import csv
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

BOT_DIR = Path(__file__).parent
BACKUP_DIR = BOT_DIR / f"pre_launch_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
PROTECTED = {"kite_config.json", "cleanup_live_launch.py"}


def backup():
    BACKUP_DIR.mkdir(exist_ok=True)
    zip_path = BOT_DIR / f"{BACKUP_DIR.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in BOT_DIR.iterdir():
            if f.name in PROTECTED or f.name.startswith(".") or f.is_dir() or f.suffix == ".py":
                continue
            z.write(f, f.name)
            print(f"  ✓ Backed up: {f.name}")
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    print(f"\n  Backup saved: {zip_path.name}")
    return zip_path


def wipe():
    print("=" * 50)
    print("  CLEANUP — Fresh Day 1 Live Launch")
    print("=" * 50)
    print()

    # Backup first
    print("  Creating backup...")
    zip_path = backup()
    print()

    # 1. Trade log
    trade_log = BOT_DIR / "trade_log.csv"
    trade_log.unlink(missing_ok=True)
    print(f"  ✓ Deleted: {trade_log.name}")

    # 2. State / cache / reference files
    for f in BOT_DIR.iterdir():
        if f.name in PROTECTED or f.name == zip_path.name:
            continue
        if f.suffix in (".json", ".csv", ".txt", ".md", ".log"):
            if f.name.startswith("kite_config") or f.name.startswith("cleanup"):
                continue
            f.unlink(missing_ok=True)
            print(f"  ✓ Deleted: {f.name}")

    # 3. Lock & heartbeat files
    for f in BOT_DIR.iterdir():
        if f.name.startswith(".bot_lock_") or f.name == ".bot_heartbeat.txt":
            f.unlink(missing_ok=True)
            print(f"  ✓ Deleted: {f.name}")

    # 4. __pycache__ directories
    for root, dirs, _ in os.walk(BOT_DIR):
        for d in dirs:
            if d == "__pycache__":
                path = os.path.join(root, d)
                shutil.rmtree(path, ignore_errors=True)
                print(f"  ✓ Removed: {os.path.relpath(path, BOT_DIR)}")

    # 5. Re-init empty trade_log.csv with headers
    fields = [
        "date", "strategy", "expiry", "entry_time", "exit_time",
        "entry_spot", "exit_spot", "entry_credit", "exit_value",
        "pnl", "charges", "max_profit_target", "stop_loss", "exit_reason",
    ]
    with open(trade_log, "w", newline="") as f:
        csv.writer(f).writerow(fields)
    print(f"  ✓ Re-initialised: {trade_log.name} (headers only)")

    # 6. Verify kite_config.json is untouched
    cfg = BOT_DIR / "kite_config.json"
    if cfg.exists():
        print(f"  ✓ Preserved: {cfg.name} (credentials intact)")
    else:
        print(f"  ⚠ {cfg.name} not found — run --login")

    print()
    print("=" * 50)
    print("  All test data wiped. Ready for live Day 1.")
    print(f"  Backup: {zip_path.name}")
    print("=" * 50)


if __name__ == "__main__":
    confirm = input(
        "This will DELETE all test data (trade log, states, caches, locks).\n"
        "A backup zip will be created first in the bot directory.\n"
        "kite_config.json will NOT be touched.\n"
        "Continue? (yes/no): "
    ).strip().lower()
    if confirm == "yes":
        wipe()
    else:
        print("Aborted.")
