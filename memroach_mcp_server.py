#!/usr/bin/env python3
"""MemRoach MCP Server — Unkillable memory for AI agents.

Primary interface for all MCP-compatible clients (Claude Code, Cursor, etc.).
Provides semantic search, storage, and team sharing of memories, skills, and configs.
Backed by CockroachDB with hybrid vector + keyword search.
"""

import gzip
import hashlib
import json
import os
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pg8000.native
from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"

mcp = FastMCP("memroach")

# Module-level connection, reused across tool calls.
_conn = None

# File type classification patterns
TYPE_PATTERNS = [
    ("memory", re.compile(r".*/memory/.*\.md$|.*/CLAUDE\.md$|^CLAUDE\.md$")),
    ("skill", re.compile(r".*/skills/.*")),
    ("config", re.compile(r"(^|.*/)settings(\.local)?\.json$|(^|.*/)mcp\.json$")),
    ("session", re.compile(r".*/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/.*")),
]


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"memroach_config.json not found at {CONFIG_FILE}. "
            "Copy memroach_config.json.example and fill in your DB credentials."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _get_conn() -> pg8000.native.Connection:
    global _conn
    if _conn is not None:
        try:
            _conn.run("SELECT 1")
            return _conn
        except Exception:
            _conn = None

    config = _load_config()
    ssl_context = True
    sslrootcert = config.get("db_sslrootcert")
    if sslrootcert and os.path.exists(sslrootcert):
        ssl_context = ssl.create_default_context(cafile=sslrootcert)

    _conn = pg8000.native.Connection(
        host=config["db_host"],
        port=int(config.get("db_port", 26257)),
        user=config["db_user"],
        password=config.get("db_password", ""),
        database=config.get("db_name", "memroach"),
        ssl_context=ssl_context,
    )
    return _conn


def _get_user() -> str:
    config = _load_config()
    return config["db_user"]


def _classify_file(rel_path: str) -> str:
    for file_type, pattern in TYPE_PATTERNS:
        if pattern.match(rel_path):
            return file_type
    return "file"


def _human_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


@mcp.tool()
def memroach_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search memories, skills, and configs using keyword matching.

    Searches file paths and content for the given query.
    Returns ranked results with file paths, types, and snippets.

    Args:
        query: Search query string
        limit: Maximum number of results (default 10)
    """
    conn = _get_conn()
    user = _get_user()

    # Keyword search on file paths
    results = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.synced_at "
        "FROM memroach_files f "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_path ILIKE :pattern "
        "ORDER BY f.synced_at DESC LIMIT :lim",
        user=user,
        pattern=f"%{query}%",
        lim=limit,
    )

    # Also search in blob content for memory/skill files
    content_results = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.synced_at "
        "FROM memroach_files f "
        "JOIN memroach_blobs b ON f.content_hash = b.content_hash "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "AND f.file_path NOT ILIKE :pattern "
        "ORDER BY f.synced_at DESC LIMIT :lim",
        user=user,
        pattern=f"%{query}%",
        lim=limit,
    )

    # Combine and deduplicate
    seen = set()
    matches = []
    for row in list(results) + list(content_results):
        path = row[0]
        if path in seen:
            continue
        seen.add(path)
        matches.append({
            "path": path,
            "type": row[1],
            "size": row[2],
            "visibility": row[3],
            "synced_at": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
        })

    return {
        "query": query,
        "count": len(matches),
        "results": matches[:limit],
    }


@mcp.tool()
def memroach_get(file_path: str) -> dict[str, Any]:
    """Fetch a specific memory, skill, or config's content from the database.

    Args:
        file_path: Path relative to ~/.claude/ (e.g., "projects/.../memory/some-file.md")
    """
    conn = _get_conn()
    user = _get_user()

    rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.version, "
        "f.synced_at, f.machine_id, b.content_bytes "
        "FROM memroach_files f "
        "JOIN memroach_blobs b ON f.content_hash = b.content_hash "
        "WHERE f.user_name = :user AND f.file_path = :path AND f.is_deleted = false "
        "ORDER BY f.synced_at DESC LIMIT 1",
        user=user,
        path=file_path,
    )

    if not rows:
        return {"error": f"File not found: {file_path}"}

    row = rows[0]
    compressed = row[7]
    try:
        content = gzip.decompress(compressed).decode("utf-8", errors="replace")
    except Exception:
        content = "[binary content]"

    return {
        "path": row[0],
        "type": row[1],
        "size": row[2],
        "visibility": row[3],
        "version": row[4],
        "synced_at": row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5]),
        "machine_id": row[6],
        "content": content,
    }


@mcp.tool()
def memroach_store(file_path: str, content: str, file_type: str = "memory",
                   visibility: str = "private") -> dict[str, Any]:
    """Store or update a memory directly in the database (bypasses file sync).

    Args:
        file_path: Path relative to ~/.claude/ (e.g., "projects/.../memory/new-insight.md")
        content: The text content to store
        file_type: One of: memory, skill, config, file (default: memory)
        visibility: 'private' (default) or 'team'
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()

    raw = content.encode("utf-8")
    content_hash = hashlib.sha256(raw).hexdigest()
    compressed = gzip.compress(raw)

    # Upsert blob
    conn.run(
        "INSERT INTO memroach_blobs (content_hash, content_bytes, original_size) "
        "VALUES (:hash, :data, :size) ON CONFLICT (content_hash) DO NOTHING",
        hash=content_hash,
        data=compressed,
        size=len(raw),
    )

    # Auto-detect type if not specified
    if file_type == "memory":
        file_type = _classify_file(file_path)

    # Upsert file metadata (use "mcp" as machine_id for direct MCP writes)
    machine_id = "mcp"
    conn.run(
        "UPSERT INTO memroach_files "
        "(user_name, machine_id, file_path, file_type, content_hash, "
        "file_size, file_mtime, visibility, version, synced_at) "
        "VALUES (:user, :machine, :path, :ftype, :hash, :size, now(), :vis, "
        "COALESCE((SELECT version FROM memroach_files "
        "WHERE user_name = :user AND machine_id = :machine AND file_path = :path), 0) + 1, "
        "now())",
        user=user,
        machine=machine_id,
        path=file_path,
        ftype=file_type,
        hash=content_hash,
        size=len(raw),
        vis=visibility,
    )

    return {
        "stored": file_path,
        "type": file_type,
        "size": len(raw),
        "visibility": visibility,
        "hash": content_hash[:12],
    }


@mcp.tool()
def memroach_list(file_type: Optional[str] = None, filter: Optional[str] = None,
                  limit: int = 50) -> dict[str, Any]:
    """List memories, skills, configs, or all entries.

    Args:
        file_type: Filter by type: memory, skill, config, session, file. None for all.
        filter: Optional path pattern filter (e.g., "*/memory/*")
        limit: Maximum results (default 50)
    """
    conn = _get_conn()
    user = _get_user()

    query = (
        "SELECT DISTINCT ON (file_path) file_path, file_type, file_size, "
        "visibility, synced_at, machine_id "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false"
    )
    params: dict[str, Any] = {"user": user, "lim": limit}

    if file_type:
        query += " AND file_type = :ftype"
        params["ftype"] = file_type

    if filter:
        query += " AND file_path ILIKE :pattern"
        params["pattern"] = filter.replace("*", "%")

    query += " ORDER BY file_path, synced_at DESC LIMIT :lim"

    rows = conn.run(query, **params)

    entries = []
    for row in rows:
        entries.append({
            "path": row[0],
            "type": row[1],
            "size": row[2],
            "visibility": row[3],
            "synced_at": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
            "machine_id": row[5],
        })

    return {
        "count": len(entries),
        "filter_type": file_type,
        "filter_pattern": filter,
        "entries": entries,
    }


@mcp.tool()
def memroach_share(file_path: str, visibility: str = "team") -> dict[str, Any]:
    """Change visibility of a memory or skill (private or team).

    Args:
        file_path: Path relative to ~/.claude/
        visibility: 'team' to share with your team, 'private' to restrict
    """
    if visibility not in ("private", "team"):
        return {"error": "visibility must be 'private' or 'team'"}

    conn = _get_conn()
    user = _get_user()

    conn.run(
        "UPDATE memroach_files SET visibility = :vis "
        "WHERE user_name = :user AND file_path = :path AND is_deleted = false",
        vis=visibility,
        user=user,
        path=file_path,
    )

    return {"path": file_path, "visibility": visibility}


@mcp.tool()
def memroach_team(query: str, limit: int = 10) -> dict[str, Any]:
    """Search team-shared memories and skills from all team members.

    Only returns entries with visibility='team'.

    Args:
        query: Search query string
        limit: Maximum number of results (default 10)
    """
    conn = _get_conn()

    results = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.user_name, f.synced_at "
        "FROM memroach_files f "
        "WHERE f.visibility = 'team' AND f.is_deleted = false "
        "AND f.file_path ILIKE :pattern "
        "ORDER BY f.synced_at DESC LIMIT :lim",
        pattern=f"%{query}%",
        lim=limit,
    )

    matches = []
    for row in results:
        matches.append({
            "path": row[0],
            "type": row[1],
            "size": row[2],
            "owner": row[3],
            "synced_at": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
        })

    return {
        "query": query,
        "count": len(matches),
        "results": matches,
    }


if __name__ == "__main__":
    mcp.run()
