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

try:
    from memroach_embed import embed_texts, hybrid_search, embed_and_store, get_provider
    HAS_EMBED = True
except ImportError:
    HAS_EMBED = False

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
    """Search memories, skills, and configs using hybrid vector + keyword search.

    Uses semantic vector similarity combined with keyword matching for best results.
    Falls back to keyword-only if embeddings are not configured.

    Args:
        query: Search query string
        limit: Maximum number of results (default 10)
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()

    # Try hybrid search if embeddings are available
    if HAS_EMBED and config.get("embed_api_key"):
        try:
            query_embedding = embed_texts([query], config)[0]
            results = hybrid_search(conn, user, query_embedding, query, limit)
            if results:
                return {
                    "query": query,
                    "search_type": "hybrid",
                    "count": len(results),
                    "results": results,
                }
        except Exception:
            pass  # Fall through to keyword search

    # Fallback: keyword search on file paths
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

    matches = []
    for row in results:
        matches.append({
            "path": row[0],
            "type": row[1],
            "size": row[2],
            "visibility": row[3],
            "score": 1.0,
            "snippet": "",
            "synced_at": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
        })

    return {
        "query": query,
        "search_type": "keyword",
        "count": len(matches),
        "results": matches,
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
    Uses hybrid vector + keyword search when embeddings are configured.

    Args:
        query: Search query string
        limit: Maximum number of results (default 10)
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()

    # Try hybrid search if embeddings are available
    if HAS_EMBED and config.get("embed_api_key"):
        try:
            query_embedding = embed_texts([query], config)[0]
            results = hybrid_search(conn, user, query_embedding, query, limit,
                                    visibility="team")
            if results:
                # Add owner field from user_name for team results
                return {
                    "query": query,
                    "search_type": "hybrid",
                    "count": len(results),
                    "results": results,
                }
        except Exception:
            pass  # Fall through to keyword search

    # Fallback: keyword search
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
        "search_type": "keyword",
        "count": len(matches),
        "results": matches,
    }


@mcp.tool()
def memroach_history(file_path: str, limit: int = 20) -> dict[str, Any]:
    """Show version history/changelog for a specific memory or file.

    Returns a timeline of all versions with timestamps, sizes, and operations.

    Args:
        file_path: Path relative to ~/.claude/
        limit: Maximum versions to return (default 20)
    """
    conn = _get_conn()
    user = _get_user()

    rows = conn.run(
        "SELECT h.version, h.operation, h.content_hash, h.file_size, "
        "h.machine_id, h.created_at "
        "FROM memroach_history h "
        "WHERE h.user_name = :user AND h.file_path = :path "
        "ORDER BY h.created_at DESC LIMIT :lim",
        user=user, path=file_path, lim=limit,
    )

    versions = []
    for row in rows:
        versions.append({
            "version": row[0],
            "operation": row[1],
            "content_hash": row[2][:12],
            "size": row[3],
            "machine_id": row[4],
            "timestamp": row[5].isoformat() if hasattr(row[5], 'isoformat') else str(row[5]),
        })

    return {
        "path": file_path,
        "version_count": len(versions),
        "versions": versions,
    }


@mcp.tool()
def memroach_consolidate(threshold: float = 0.85, limit: int = 10) -> dict[str, Any]:
    """Find near-duplicate or overlapping memories and suggest consolidations.

    Uses embedding similarity to identify memory files that cover similar topics
    and could be merged into fewer, stronger memories.

    Args:
        threshold: Similarity threshold (0-1) for considering files as duplicates (default 0.85)
        limit: Maximum number of suggestions to return (default 10)
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()

    if not HAS_EMBED or not config.get("embed_api_key"):
        return {"error": "Embeddings not configured. Set embed_api_key in memroach_config.json."}

    # Get all memory embeddings
    rows = conn.run(
        "SELECT e.file_path, e.chunk_text, e.embedding "
        "FROM memroach_embeddings e "
        "JOIN memroach_files f ON e.user_name = f.user_name AND e.file_path = f.file_path "
        "WHERE e.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "AND e.chunk_index = 0 "
        "ORDER BY f.synced_at DESC",
        user=user,
    )

    if len(rows) < 2:
        return {"suggestions": [], "count": 0, "message": "Not enough memories to compare."}

    # Parse embeddings and compute pairwise similarity
    from memroach_embed import cosine_similarity
    files = []
    for row in rows:
        path, chunk_text, emb_str = row
        try:
            if isinstance(emb_str, str):
                emb = json.loads(emb_str)
            elif isinstance(emb_str, (list, tuple)):
                emb = list(emb_str)
            else:
                continue
            files.append({"path": path, "snippet": chunk_text[:150], "embedding": emb})
        except (json.JSONDecodeError, TypeError):
            continue

    # Find similar pairs
    suggestions = []
    seen = set()
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            if files[i]["path"] == files[j]["path"]:
                continue
            sim = cosine_similarity(files[i]["embedding"], files[j]["embedding"])
            if sim >= threshold:
                pair_key = tuple(sorted([files[i]["path"], files[j]["path"]]))
                if pair_key not in seen:
                    seen.add(pair_key)
                    suggestions.append({
                        "similarity": round(sim, 4),
                        "file_a": files[i]["path"],
                        "snippet_a": files[i]["snippet"],
                        "file_b": files[j]["path"],
                        "snippet_b": files[j]["snippet"],
                    })

    # Sort by similarity descending
    suggestions.sort(key=lambda x: x["similarity"], reverse=True)
    suggestions = suggestions[:limit]

    return {
        "count": len(suggestions),
        "threshold": threshold,
        "files_compared": len(files),
        "suggestions": suggestions,
    }


@mcp.tool()
def memroach_context(topic: str, limit: int = 5,
                     include_team: bool = False) -> dict[str, Any]:
    """Get a curated context bundle of relevant memories, skills, and configs for a topic.

    Searches across all your stored knowledge using hybrid search and returns
    the full content of the most relevant files, formatted as a single context block
    that can be consumed directly.

    Args:
        topic: The topic or question to find context for
        limit: Maximum number of files to include (default 5)
        include_team: Also search team-shared memories (default False)
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()

    # Find relevant files via hybrid search
    results = []
    if HAS_EMBED and config.get("embed_api_key"):
        try:
            query_embedding = embed_texts([topic], config)[0]
            visibility = "team" if include_team else None
            results = hybrid_search(conn, user, query_embedding, topic, limit,
                                    visibility=visibility)
        except Exception:
            pass

    if not results:
        # Fallback to keyword search
        rows = conn.run(
            "SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.synced_at "
            "FROM memroach_files f "
            "WHERE f.user_name = :user AND f.is_deleted = false "
            "AND f.file_path ILIKE :pattern "
            "ORDER BY f.synced_at DESC LIMIT :lim",
            user=user, pattern=f"%{topic}%", lim=limit,
        )
        results = [{"path": r[0], "type": r[1], "size": r[2], "score": 1.0} for r in rows]

    if not results:
        return {"topic": topic, "count": 0, "context": "", "files": []}

    # Fetch full content for each result
    context_parts = []
    files_included = []
    for r in results:
        rows = conn.run(
            "SELECT b.content_bytes FROM memroach_files f "
            "JOIN memroach_blobs b ON f.content_hash = b.content_hash "
            "WHERE f.user_name = :user AND f.file_path = :path AND f.is_deleted = false "
            "ORDER BY f.synced_at DESC LIMIT 1",
            user=user, path=r["path"],
        )
        if not rows:
            continue

        try:
            content = gzip.decompress(rows[0][0]).decode("utf-8", errors="replace")
        except Exception:
            continue

        context_parts.append(f"--- {r['path']} (score: {r.get('score', 1.0):.2f}) ---\n{content}")
        files_included.append({
            "path": r["path"],
            "type": r.get("type", "file"),
            "score": r.get("score", 1.0),
            "size": r.get("size", len(content)),
        })

    context_block = "\n\n".join(context_parts)

    return {
        "topic": topic,
        "count": len(files_included),
        "files": files_included,
        "context": context_block,
    }


@mcp.tool()
def memroach_changes(since_minutes: int = 60, limit: int = 20) -> dict[str, Any]:
    """Check for recent changes from other machines (real-time sync awareness).

    Shows files that were modified by other machines since a given time window.
    Useful for understanding what changed while you were away or on another device.

    Args:
        since_minutes: Look back this many minutes (default 60)
        limit: Maximum results (default 20)
    """
    conn = _get_conn()
    user = _get_user()
    config = _load_config()
    machine_id = config.get("machine_id", "")

    rows = conn.run(
        "SELECT file_path, file_type, file_size, machine_id, synced_at, version "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "AND machine_id != :machine "
        "AND synced_at > now() - :interval::INTERVAL "
        "ORDER BY synced_at DESC LIMIT :lim",
        user=user,
        machine=machine_id,
        interval=f"{since_minutes} minutes",
        lim=limit,
    )

    changes = []
    for row in rows:
        changes.append({
            "path": row[0],
            "type": row[1],
            "size": row[2],
            "from_machine": row[3],
            "synced_at": row[4].isoformat() if hasattr(row[4], 'isoformat') else str(row[4]),
            "version": row[5],
        })

    return {
        "since_minutes": since_minutes,
        "current_machine": machine_id,
        "count": len(changes),
        "changes": changes,
    }


if __name__ == "__main__":
    mcp.run()
