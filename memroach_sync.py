#!/usr/bin/env python3
"""MemRoach Sync — bidirectional file sync between ~/.claude/ and CockroachDB.

Dual-mode: CLI tool and Claude Code hook handler.
CLI: memroach push|pull|status|diff|search|share|init
Hook: reads hook_event_name from stdin, auto-pushes in background.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import socket
import subprocess
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pg8000.native

# Optional: embedding support (graceful if not configured)
try:
    from memroach_embed import embed_and_store, embed_texts, hybrid_search, get_provider
    HAS_EMBED = True
except ImportError:
    HAS_EMBED = False

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"
CLAUDE_DIR = Path.home() / ".claude"
STATE_FILE = CLAUDE_DIR / ".memroach_state.json"
LOG_FILE = Path("/tmp/memroach_sync.log")

# File type classification patterns
TYPE_PATTERNS = [
    ("memory", re.compile(r".*/memory/.*\.md$|.*/CLAUDE\.md$|^CLAUDE\.md$")),
    ("skill", re.compile(r".*/skills/.*")),
    ("config", re.compile(r"(^|.*/)settings(\.local)?\.json$|(^|.*/)mcp\.json$")),
    ("session", re.compile(r".*/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/.*")),
]


def classify_file(rel_path: str) -> str:
    """Auto-classify file type by path pattern."""
    for file_type, pattern in TYPE_PATTERNS:
        if pattern.match(rel_path):
            return file_type
    return "file"


def load_config() -> dict:
    """Load memroach configuration."""
    if not CONFIG_FILE.exists():
        print(f"Config not found: {CONFIG_FILE}")
        print(f"Copy {CONFIG_FILE.with_suffix('.json.example')} and fill in your DB credentials.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_machine_id(config: dict) -> str:
    """Get machine identifier."""
    return config.get("machine_id") or socket.gethostname()


def get_connection(config: dict) -> pg8000.native.Connection:
    """Create a CockroachDB connection via pg8000."""
    ssl_context = True
    sslrootcert = config.get("db_sslrootcert")
    if sslrootcert and os.path.exists(sslrootcert):
        ssl_context = ssl.create_default_context(cafile=sslrootcert)

    return pg8000.native.Connection(
        host=config["db_host"],
        port=int(config.get("db_port", 26257)),
        user=config["db_user"],
        password=config.get("db_password", ""),
        database=config.get("db_name", "memroach"),
        ssl_context=ssl_context,
    )


def load_state() -> dict:
    """Load the local sync state cache."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    """Save the local sync state cache."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def sha256_file(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_claude_dir(config: dict) -> list[dict]:
    """Scan ~/.claude/ recursively and return file info dicts."""
    if not CLAUDE_DIR.exists():
        return []

    max_size = config.get("max_file_size_mb", 50) * 1024 * 1024
    exclude_patterns = config.get("exclude_patterns", [])
    state = load_state()
    files = []

    for file_path in CLAUDE_DIR.rglob("*"):
        if not file_path.is_file():
            continue

        rel_path = str(file_path.relative_to(CLAUDE_DIR))

        # Skip state file itself
        if rel_path == ".memroach_state.json":
            continue

        # Skip excluded patterns
        if any(file_path.match(p) for p in exclude_patterns):
            continue

        try:
            stat = file_path.stat()
        except OSError:
            continue

        if stat.st_size > max_size:
            continue

        # Use cached hash if mtime and size haven't changed
        cached = state.get(rel_path)
        if cached and cached.get("mtime") == stat.st_mtime and cached.get("size") == stat.st_size:
            content_hash = cached["hash"]
        else:
            content_hash = sha256_file(file_path)

        files.append({
            "path": rel_path,
            "hash": content_hash,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "type": classify_file(rel_path),
            "abs_path": str(file_path),
        })

    return files


def cmd_init(config: dict):
    """Test DB connectivity and show status."""
    print("MemRoach — testing CockroachDB connection...")
    try:
        conn = get_connection(config)
        result = conn.run("SELECT version()")
        version = result[0][0] if result else "unknown"
        print(f"  Connected to: {config['db_host']}:{config.get('db_port', 26257)}")
        print(f"  Database: {config.get('db_name', 'memroach')}")
        print(f"  User: {config['db_user']}")
        print(f"  CockroachDB: {version[:80]}")
        print(f"  Machine ID: {get_machine_id(config)}")

        # Check if tables exist
        tables = conn.run(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'memroach_%'"
        )
        table_names = [r[0] for r in tables]
        if table_names:
            print(f"  Tables: {', '.join(table_names)}")
        else:
            print("  Tables: NOT FOUND — run schema/memroach_schema.sql first")

        conn.close()
        print("\nConnection successful.")
    except Exception as e:
        print(f"\nConnection FAILED: {e}")
        sys.exit(1)


def cmd_push(config: dict, force: bool = False, dry_run: bool = False, verbose: bool = False):
    """Push local files to CockroachDB."""
    machine_id = get_machine_id(config)
    user = config["db_user"]

    print("Scanning ~/.claude/ ...")
    local_files = scan_claude_dir(config)
    print(f"  Found {len(local_files)} files")

    if not local_files:
        print("Nothing to push.")
        return

    conn = get_connection(config)

    # Get current remote state for this user+machine
    remote_rows = conn.run(
        "SELECT file_path, content_hash, version FROM memroach_files "
        "WHERE user_name = :user AND machine_id = :machine AND is_deleted = false",
        user=user,
        machine=machine_id,
    )
    remote_state = {r[0]: {"hash": r[1], "version": r[2]} for r in remote_rows}

    # Find changed files
    to_push = []
    for f in local_files:
        remote = remote_state.get(f["path"])
        if not remote or remote["hash"] != f["hash"]:
            f["remote_version"] = remote["version"] if remote else 0
            to_push.append(f)

    if not to_push:
        print("Everything up to date.")
        _update_state_cache(local_files)
        conn.close()
        return

    print(f"  {len(to_push)} files changed")

    if dry_run:
        for f in to_push:
            print(f"  would push: {f['path']} ({f['type']}, {f['size']} bytes)")
        conn.close()
        return

    # Check which blobs already exist
    hashes = list({f["hash"] for f in to_push})
    existing_hashes = set()
    # Query in batches of 100
    for i in range(0, len(hashes), 100):
        batch = hashes[i:i + 100]
        placeholders = ", ".join(f":h{j}" for j in range(len(batch)))
        params = {f"h{j}": h for j, h in enumerate(batch)}
        rows = conn.run(
            f"SELECT content_hash FROM memroach_blobs WHERE content_hash IN ({placeholders})",
            **params,
        )
        existing_hashes.update(r[0] for r in rows)

    # Upload missing blobs
    new_blobs = [f for f in to_push if f["hash"] not in existing_hashes]
    if new_blobs:
        if verbose:
            print(f"  Uploading {len(new_blobs)} new blobs...")
        for f in new_blobs:
            with open(f["abs_path"], "rb") as fh:
                raw = fh.read()
            compressed = gzip.compress(raw)
            conn.run(
                "INSERT INTO memroach_blobs (content_hash, content_bytes, original_size) "
                "VALUES (:hash, :data, :size) "
                "ON CONFLICT (content_hash) DO NOTHING",
                hash=f["hash"],
                data=compressed,
                size=len(raw),
            )

    # Upsert file metadata
    pushed = 0
    conflicts = 0
    total_bytes = 0
    for f in to_push:
        if f["remote_version"] > 0 and not force:
            # Optimistic concurrency: update only if version matches
            result = conn.run(
                "UPDATE memroach_files SET "
                "content_hash = :hash, file_size = :size, file_mtime = :mtime, "
                "file_type = :ftype, version = version + 1, synced_at = now() "
                "WHERE user_name = :user AND machine_id = :machine AND file_path = :path "
                "AND version = :expected_version",
                hash=f["hash"],
                size=f["size"],
                mtime=f["mtime_iso"],
                ftype=f["type"],
                user=user,
                machine=machine_id,
                path=f["path"],
                expected_version=f["remote_version"],
            )
            # pg8000 native doesn't return rowcount for UPDATE easily,
            # so we verify by re-reading
            verify = conn.run(
                "SELECT version FROM memroach_files "
                "WHERE user_name = :user AND machine_id = :machine AND file_path = :path",
                user=user,
                machine=machine_id,
                path=f["path"],
            )
            if verify and verify[0][0] == f["remote_version"]:
                # Version didn't change — conflict
                conflicts += 1
                if verbose:
                    print(f"  CONFLICT: {f['path']} (version mismatch, use --force)")
                continue
        else:
            # Insert or force-update
            conn.run(
                "UPSERT INTO memroach_files "
                "(user_name, machine_id, file_path, file_type, content_hash, "
                "file_size, file_mtime, version, synced_at) "
                "VALUES (:user, :machine, :path, :ftype, :hash, :size, :mtime, "
                "COALESCE((SELECT version FROM memroach_files "
                "WHERE user_name = :user AND machine_id = :machine AND file_path = :path), 0) + 1, "
                "now())",
                user=user,
                machine=machine_id,
                path=f["path"],
                ftype=f["type"],
                hash=f["hash"],
                size=f["size"],
                mtime=f["mtime_iso"],
            )

        pushed += 1
        total_bytes += f["size"]
        if verbose:
            print(f"  pushed: {f['path']} ({f['type']})")

    # Generate embeddings for memory/skill files (if configured)
    embedded = 0
    if HAS_EMBED and config.get("embed_api_key"):
        embeddable = [f for f in to_push if f["type"] in ("memory", "skill") and f["size"] < 100000]
        if embeddable:
            if verbose:
                print(f"  Embedding {len(embeddable)} memory/skill files...")
            for f in embeddable:
                try:
                    with open(f["abs_path"], "r", errors="replace") as fh:
                        content = fh.read()
                    count = embed_and_store(conn, user, f["path"], content, f["hash"], config)
                    if count and count > 0:
                        embedded += count
                except Exception as e:
                    if verbose:
                        print(f"  embed warning: {f['path']}: {e}")

    # Log the operation
    conn.run(
        "INSERT INTO memroach_log (user_name, machine_id, operation, files_changed, bytes_transferred) "
        "VALUES (:user, :machine, 'push', :count, :bytes)",
        user=user,
        machine=machine_id,
        count=pushed,
        bytes=total_bytes,
    )

    conn.close()
    _update_state_cache(local_files)

    print(f"Pushed {pushed} files ({_human_size(total_bytes)})")
    if embedded:
        print(f"  Embedded {embedded} chunks for semantic search")
    if conflicts:
        print(f"  {conflicts} conflicts (use --force to overwrite)")


def cmd_pull(config: dict, target: Optional[str] = None, force: bool = False,
             dry_run: bool = False, verbose: bool = False):
    """Pull latest files from CockroachDB to disk."""
    user = config["db_user"]
    target_dir = Path(target) if target else CLAUDE_DIR

    conn = get_connection(config)

    # Get the latest version of each file across all machines (last-write-wins)
    remote_files = conn.run(
        "SELECT DISTINCT ON (file_path) file_path, content_hash, file_size, "
        "file_mtime, file_type, synced_at, machine_id "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "ORDER BY file_path, synced_at DESC",
        user=user,
    )

    if not remote_files:
        print("No files in remote.")
        conn.close()
        return

    # Compare against local
    to_pull = []
    for row in remote_files:
        rel_path, content_hash, file_size, file_mtime, file_type, synced_at, from_machine = row
        local_path = target_dir / rel_path

        if local_path.exists():
            local_hash = sha256_file(local_path)
            if local_hash == content_hash:
                continue  # Already up to date

            if not force:
                # Check if local is newer
                local_mtime = local_path.stat().st_mtime
                remote_mtime_ts = file_mtime.timestamp() if hasattr(file_mtime, 'timestamp') else 0
                if local_mtime > remote_mtime_ts:
                    if verbose:
                        print(f"  skip (local newer): {rel_path}")
                    continue

        to_pull.append({
            "path": rel_path,
            "hash": content_hash,
            "size": file_size,
            "type": file_type,
            "from_machine": from_machine,
        })

    if not to_pull:
        print("Everything up to date.")
        conn.close()
        return

    print(f"  {len(to_pull)} files to pull")

    if dry_run:
        for f in to_pull:
            print(f"  would pull: {f['path']} ({f['type']}, {f['size']} bytes, from {f['from_machine']})")
        conn.close()
        return

    # Fetch blobs and write files
    pulled = 0
    total_bytes = 0
    hashes_needed = list({f["hash"] for f in to_pull})

    # Fetch blobs in batches
    blob_map = {}
    for i in range(0, len(hashes_needed), 50):
        batch = hashes_needed[i:i + 50]
        placeholders = ", ".join(f":h{j}" for j in range(len(batch)))
        params = {f"h{j}": h for j, h in enumerate(batch)}
        rows = conn.run(
            f"SELECT content_hash, content_bytes FROM memroach_blobs "
            f"WHERE content_hash IN ({placeholders})",
            **params,
        )
        for content_hash, content_bytes in rows:
            blob_map[content_hash] = content_bytes

    for f in to_pull:
        compressed = blob_map.get(f["hash"])
        if not compressed:
            print(f"  ERROR: blob not found for {f['path']}")
            continue

        raw = gzip.decompress(compressed)
        local_path = target_dir / f["path"]
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as fh:
            fh.write(raw)

        pulled += 1
        total_bytes += len(raw)
        if verbose:
            print(f"  pulled: {f['path']} ({f['type']}, from {f['from_machine']})")

    # Log the operation
    machine_id = get_machine_id(config)
    conn.run(
        "INSERT INTO memroach_log (user_name, machine_id, operation, files_changed, bytes_transferred) "
        "VALUES (:user, :machine, 'pull', :count, :bytes)",
        user=user,
        machine=machine_id,
        count=pulled,
        bytes=total_bytes,
    )

    conn.close()
    print(f"Pulled {pulled} files ({_human_size(total_bytes)})")


def cmd_status(config: dict, verbose: bool = False):
    """Show what's changed locally vs remote."""
    machine_id = get_machine_id(config)
    user = config["db_user"]

    print("Scanning ~/.claude/ ...")
    local_files = scan_claude_dir(config)
    local_map = {f["path"]: f for f in local_files}

    conn = get_connection(config)

    # Get remote state for this user (latest across all machines)
    remote_rows = conn.run(
        "SELECT DISTINCT ON (file_path) file_path, content_hash, file_size, "
        "file_type, machine_id, synced_at "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "ORDER BY file_path, synced_at DESC",
        user=user,
    )
    remote_map = {r[0]: {"hash": r[1], "size": r[2], "type": r[3], "machine": r[4]} for r in remote_rows}
    conn.close()

    to_push = []
    to_pull = []
    in_sync = 0

    # Files that exist locally
    for path, local in local_map.items():
        remote = remote_map.get(path)
        if not remote:
            to_push.append(("new", path, local))
        elif remote["hash"] != local["hash"]:
            to_push.append(("modified", path, local))
        else:
            in_sync += 1

    # Files that exist remotely but not locally
    for path, remote in remote_map.items():
        if path not in local_map:
            to_pull.append(("missing", path, remote))

    # Summary
    type_counts = {}
    for f in local_files:
        type_counts[f["type"]] = type_counts.get(f["type"], 0) + 1

    print(f"\nLocal: {len(local_files)} files ({', '.join(f'{v} {k}' for k, v in sorted(type_counts.items()))})")
    print(f"Remote: {len(remote_map)} files")
    print(f"In sync: {in_sync}")
    print(f"To push: {len(to_push)}")
    print(f"To pull: {len(to_pull)}")

    if verbose and to_push:
        print("\nFiles to push:")
        for status, path, info in to_push:
            print(f"  [{status}] {path} ({info.get('type', '?')}, {info.get('size', 0)} bytes)")

    if verbose and to_pull:
        print("\nFiles to pull:")
        for status, path, info in to_pull:
            print(f"  [{status}] {path} (from {info.get('machine', '?')})")


def cmd_share(config: dict, file_path: str, visibility: str = "team"):
    """Set visibility on a memory."""
    user = config["db_user"]
    conn = get_connection(config)

    result = conn.run(
        "UPDATE memroach_files SET visibility = :vis "
        "WHERE user_name = :user AND file_path = :path",
        vis=visibility,
        user=user,
        path=file_path,
    )
    conn.close()
    print(f"Set {file_path} to '{visibility}'")


def cmd_search(config: dict, query: str, limit: int = 10):
    """Search memories using hybrid vector + keyword search."""
    user = config["db_user"]
    conn = get_connection(config)

    # Try hybrid search if embeddings are available
    if HAS_EMBED and config.get("embed_api_key"):
        try:
            query_embedding = embed_texts([query], config)[0]
            results = hybrid_search(conn, user, query_embedding, query, limit)
            if results:
                print(f"Results for '{query}' (hybrid search):")
                for r in results:
                    vis_tag = " [team]" if r["visibility"] == "team" else ""
                    snippet = f" — {r['snippet'][:80]}..." if r.get("snippet") else ""
                    print(f"  [{r['score']:.2f}] {r['path']} ({r['type']}, {_human_size(r['size'])}){vis_tag}{snippet}")
                conn.close()
                return
        except Exception as e:
            print(f"  (vector search unavailable: {e}, falling back to keyword)")

    # Fallback: keyword search
    results = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility "
        "FROM memroach_files f "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_path ILIKE :pattern "
        "ORDER BY f.synced_at DESC LIMIT :lim",
        user=user,
        pattern=f"%{query}%",
        lim=limit,
    )

    if not results:
        print(f"No results for '{query}'")
    else:
        print(f"Results for '{query}' (keyword search):")
        for path, ftype, size, vis in results:
            vis_tag = " [team]" if vis == "team" else ""
            print(f"  {path} ({ftype}, {_human_size(size)}){vis_tag}")

    conn.close()


def _update_state_cache(files: list[dict]):
    """Update the local state cache with current file info."""
    state = {f["path"]: {"hash": f["hash"], "mtime": f["mtime"], "size": f["size"]} for f in files}
    save_state(state)


def _human_size(size: int) -> str:
    """Format bytes as human-readable."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _log(msg: str):
    """Append to log file."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except OSError:
        pass


def handle_hook():
    """Handle Claude Code hook events from stdin.

    Called globally for all Claude Code sessions. Must NEVER crash or block —
    any unhandled exception would disrupt the user's session.
    """
    try:
        raw = sys.stdin.read()
        if not raw:
            return

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return  # Not JSON, not a hook event

        event = data.get("hook_event_name", "")

        if event not in ("Stop", "SessionEnd"):
            return

        # Check if config exists before attempting push
        if not CONFIG_FILE.exists():
            _log("Hook skipped: memroach_config.json not found")
            return

        # Validate config is loadable
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
            if not config.get("db_host"):
                _log("Hook skipped: db_host not configured")
                return
        except (json.JSONDecodeError, OSError) as e:
            _log(f"Hook skipped: config error: {e}")
            return

        # Check auto-push settings
        if event == "Stop" and not config.get("auto_push_on_stop", True):
            return
        if event == "SessionEnd" and not config.get("auto_push_on_session_end", True):
            return

        _log(f"Hook triggered: {event}")

        # Fork to background so we don't block the hook timeout.
        # The subprocess runs independently — even if it fails, the hook returns cleanly.
        try:
            log_fh = open(LOG_FILE, "a")
        except OSError:
            log_fh = subprocess.DEVNULL

        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "push", "--quiet"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(SCRIPT_DIR),  # Ensure we're in the right directory
            )
        except OSError as e:
            _log(f"Hook: failed to spawn push subprocess: {e}")

    except Exception as e:
        # Absolute last-resort catch — log and return, never crash
        try:
            _log(f"Hook error (caught): {e}")
        except Exception:
            pass  # Even logging failed, just exit cleanly


def main():
    # Check if running as a hook (stdin has JSON with hook_event_name).
    # Wrapped in try/except so hook detection itself never crashes.
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.buffer.peek(1)
            if raw and raw[0:1] == b"{":
                handle_hook()
                return
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        prog="memroach",
        description="MemRoach — Unkillable memory for AI agents",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    sub.add_parser("init", help="Test DB connectivity")

    # push
    p_push = sub.add_parser("push", help="Push local files to DB")
    p_push.add_argument("--force", action="store_true", help="Override version checks")
    p_push.add_argument("--dry-run", action="store_true", help="Show what would be pushed")
    p_push.add_argument("--verbose", "-v", action="store_true")
    p_push.add_argument("--quiet", "-q", action="store_true")

    # pull
    p_pull = sub.add_parser("pull", help="Pull latest files from DB")
    p_pull.add_argument("--target", help="Pull to a different directory (default: ~/.claude/)")
    p_pull.add_argument("--force", action="store_true", help="Overwrite even if local is newer")
    p_pull.add_argument("--dry-run", action="store_true", help="Show what would be pulled")
    p_pull.add_argument("--verbose", "-v", action="store_true")

    # status
    p_status = sub.add_parser("status", help="Show sync status")
    p_status.add_argument("--verbose", "-v", action="store_true")

    # search
    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)

    # share
    p_share = sub.add_parser("share", help="Set memory visibility")
    p_share.add_argument("path", help="File path (relative to ~/.claude/)")
    p_share.add_argument("--team", action="store_const", const="team", dest="visibility", default="team")
    p_share.add_argument("--private", action="store_const", const="private", dest="visibility")

    # diff
    sub.add_parser("diff", help="Show detailed differences (alias for status -v)")

    args = parser.parse_args()
    config = load_config()

    if args.command == "init":
        cmd_init(config)
    elif args.command == "push":
        if not args.quiet:
            cmd_push(config, force=args.force, dry_run=args.dry_run, verbose=args.verbose)
        else:
            try:
                cmd_push(config, force=True, verbose=False)
            except Exception as e:
                _log(f"Push error: {e}")
    elif args.command == "pull":
        cmd_pull(config, target=args.target, force=args.force,
                 dry_run=args.dry_run, verbose=args.verbose)
    elif args.command == "status":
        cmd_status(config, verbose=args.verbose)
    elif args.command == "diff":
        cmd_status(config, verbose=True)
    elif args.command == "search":
        cmd_search(config, args.query, args.limit)
    elif args.command == "share":
        cmd_share(config, args.path, args.visibility)


if __name__ == "__main__":
    main()
