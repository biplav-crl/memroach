#!/usr/bin/env python3
"""MemRoach Daemon — lightweight background sync watcher.

Polls CockroachDB for changes from other machines and auto-pulls them.
Runs as a background process, started manually or via launchd/systemd.

Usage:
    python memroach_daemon.py                    # Run in foreground
    python memroach_daemon.py --daemonize        # Fork to background
    python memroach_daemon.py --interval 30      # Poll every 30 seconds
    python memroach_daemon.py --stop             # Stop running daemon
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"
PID_FILE = Path("/tmp/memroach_daemon.pid")
LOG_FILE = Path("/tmp/memroach_daemon.log")
DEFAULT_INTERVAL = 60  # seconds


def _log(msg: str):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except OSError:
        pass


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def check_for_changes(config: dict, last_check: str) -> list[dict]:
    """Query DB for files changed by other machines since last_check."""
    # Import here to avoid circular imports
    from memroach_sync import get_connection, get_machine_id

    machine_id = get_machine_id(config)
    user = config["db_user"]

    conn = get_connection(config)
    rows = conn.run(
        "SELECT file_path, file_type, machine_id, synced_at "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "AND machine_id != :machine "
        "AND synced_at > :since::TIMESTAMPTZ "
        "ORDER BY synced_at DESC",
        user=user,
        machine=machine_id,
        since=last_check,
    )
    conn.close()

    return [{"path": r[0], "type": r[1], "machine": r[2],
             "synced_at": r[3].isoformat() if hasattr(r[3], 'isoformat') else str(r[3])}
            for r in rows]


def pull_changes(config: dict):
    """Run a quiet pull to sync changes from other machines."""
    from memroach_sync import cmd_pull
    try:
        cmd_pull(config, quiet=True, verbose=False)
    except Exception as e:
        _log(f"Pull error: {e}")


def run_daemon(interval: int):
    """Main daemon loop."""
    _log(f"Daemon started (pid={os.getpid()}, interval={interval}s)")

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    # Handle graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        running = False
        _log("Daemon stopping (signal received)")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_check = datetime.now(timezone.utc).isoformat()

    while running:
        try:
            config = load_config()
            changes = check_for_changes(config, last_check)

            if changes:
                _log(f"Found {len(changes)} changes from other machines")
                pull_changes(config)
                _log(f"Pulled {len(changes)} changes")

            last_check = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            _log(f"Daemon error: {e}")

        # Sleep in small increments so we can respond to signals
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    # Cleanup
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    _log("Daemon stopped")


def stop_daemon():
    """Stop a running daemon."""
    if not PID_FILE.exists():
        print("No daemon running (no PID file)")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (pid={pid})")
        # Wait for it to stop
        for _ in range(10):
            try:
                os.kill(pid, 0)  # Check if still running
                time.sleep(0.5)
            except ProcessLookupError:
                print("Daemon stopped")
                return
        print("Daemon did not stop — sending SIGKILL")
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        print(f"Daemon not running (stale PID file, pid={pid})")
        PID_FILE.unlink(missing_ok=True)


def status_daemon():
    """Check daemon status."""
    if not PID_FILE.exists():
        print("Daemon: not running")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 0)
        print(f"Daemon: running (pid={pid})")
        # Show last few log lines
        if LOG_FILE.exists():
            lines = LOG_FILE.read_text().strip().split("\n")
            print(f"Recent log ({len(lines)} entries):")
            for line in lines[-5:]:
                print(f"  {line}")
    except ProcessLookupError:
        print(f"Daemon: not running (stale PID file, pid={pid})")
        PID_FILE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(
        prog="memroach-daemon",
        description="MemRoach background sync daemon",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--daemonize", action="store_true",
                        help="Fork to background")
    parser.add_argument("--stop", action="store_true",
                        help="Stop running daemon")
    parser.add_argument("--status", action="store_true",
                        help="Check daemon status")

    args = parser.parse_args()

    if args.stop:
        stop_daemon()
        return

    if args.status:
        status_daemon()
        return

    if not CONFIG_FILE.exists():
        print(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)

    if args.daemonize:
        # Double fork to fully detach
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        if os.fork() > 0:
            sys.exit(0)

        # Redirect stdio
        sys.stdin = open(os.devnull, "r")
        sys.stdout = open(LOG_FILE, "a")
        sys.stderr = sys.stdout

        print(f"Daemon forked to background (pid={os.getpid()})")

    run_daemon(args.interval)


if __name__ == "__main__":
    main()
