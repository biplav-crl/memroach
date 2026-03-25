#!/usr/bin/env python3
"""MemRoach Admin — user management for environments without LDAP/OIDC.

For environments with an IdP, use CockroachDB's LDAP/OIDC integration instead.
This script is a fallback for manual user provisioning.
"""

import argparse
import json
import os
import ssl
import sys
from pathlib import Path

import pg8000.native

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"Config not found: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_admin_conn(config: dict) -> pg8000.native.Connection:
    """Connect as admin user (needs CREATE USER privileges)."""
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


def cmd_create_user(config: dict, username: str, password: str):
    """Create a CockroachDB user with MemRoach permissions."""
    conn = _get_admin_conn(config)

    conn.run(f"CREATE USER IF NOT EXISTS {username} WITH PASSWORD :pw", pw=password)
    conn.run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON memroach_blobs TO {username}")
    conn.run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON memroach_files TO {username}")
    conn.run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON memroach_embeddings TO {username}")
    conn.run(f"GRANT SELECT, INSERT ON memroach_log TO {username}")

    conn.close()
    print(f"Created user '{username}' with MemRoach permissions.")
    print(f"They should set db_user='{username}' in their memroach_config.json.")


def cmd_list_users(config: dict):
    """List all users with MemRoach data."""
    conn = _get_admin_conn(config)

    rows = conn.run(
        "SELECT user_name, COUNT(*) as file_count, "
        "SUM(file_size) as total_size, MAX(synced_at) as last_sync "
        "FROM memroach_files WHERE is_deleted = false "
        "GROUP BY user_name ORDER BY user_name"
    )

    if not rows:
        print("No users found.")
        return

    print(f"{'User':<20} {'Files':<8} {'Size':<12} {'Last Sync'}")
    print("-" * 65)
    for user, count, size, last_sync in rows:
        size_str = f"{size / 1024 / 1024:.1f}MB" if size else "0B"
        sync_str = last_sync.strftime("%Y-%m-%d %H:%M") if last_sync else "never"
        print(f"{user:<20} {count:<8} {size_str:<12} {sync_str}")

    conn.close()


def cmd_user_stats(config: dict, username: str):
    """Show detailed stats for a user."""
    conn = _get_admin_conn(config)

    # File counts by type
    type_rows = conn.run(
        "SELECT file_type, COUNT(*), SUM(file_size) "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "GROUP BY file_type ORDER BY file_type",
        user=username,
    )

    if not type_rows:
        print(f"No data for user '{username}'.")
        conn.close()
        return

    print(f"Stats for '{username}':")
    print(f"\n{'Type':<12} {'Files':<8} {'Size'}")
    print("-" * 35)
    total_files = 0
    total_size = 0
    for ftype, count, size in type_rows:
        size_str = f"{size / 1024 / 1024:.1f}MB" if size else "0B"
        print(f"{ftype:<12} {count:<8} {size_str}")
        total_files += count
        total_size += size or 0
    print("-" * 35)
    print(f"{'Total':<12} {total_files:<8} {total_size / 1024 / 1024:.1f}MB")

    # Machine IDs
    machines = conn.run(
        "SELECT DISTINCT machine_id, MAX(synced_at) "
        "FROM memroach_files WHERE user_name = :user "
        "GROUP BY machine_id ORDER BY MAX(synced_at) DESC",
        user=username,
    )
    if machines:
        print(f"\nMachines:")
        for machine, last_sync in machines:
            sync_str = last_sync.strftime("%Y-%m-%d %H:%M") if last_sync else "never"
            print(f"  {machine} (last sync: {sync_str})")

    # Team-shared files
    shared = conn.run(
        "SELECT COUNT(*) FROM memroach_files "
        "WHERE user_name = :user AND visibility = 'team' AND is_deleted = false",
        user=username,
    )
    if shared:
        print(f"\nTeam-shared files: {shared[0][0]}")

    # Recent sync log
    logs = conn.run(
        "SELECT operation, files_changed, bytes_transferred, completed_at "
        "FROM memroach_log WHERE user_name = :user "
        "ORDER BY completed_at DESC LIMIT 5",
        user=username,
    )
    if logs:
        print(f"\nRecent syncs:")
        for op, files, bytes_xfer, completed in logs:
            size_str = f"{bytes_xfer / 1024:.0f}KB" if bytes_xfer else "0B"
            time_str = completed.strftime("%Y-%m-%d %H:%M") if completed else "?"
            print(f"  {op} {files} files ({size_str}) at {time_str}")

    conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="memroach-admin",
        description="MemRoach Admin — user management (fallback for non-IdP environments)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-user", help="Create a new MemRoach user")
    p_create.add_argument("username", help="CockroachDB username")
    p_create.add_argument("--password", required=True, help="User password")

    sub.add_parser("list-users", help="List all users with MemRoach data")

    p_stats = sub.add_parser("user-stats", help="Show detailed stats for a user")
    p_stats.add_argument("username", help="Username to inspect")

    args = parser.parse_args()
    config = _load_config()

    if args.command == "create-user":
        cmd_create_user(config, args.username, args.password)
    elif args.command == "list-users":
        cmd_list_users(config)
    elif args.command == "user-stats":
        cmd_user_stats(config, args.username)


if __name__ == "__main__":
    main()
