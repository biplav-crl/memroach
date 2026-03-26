#!/usr/bin/env python3
"""MemRoach Web UI — browse, search, and visualize your AI agent memories.

Usage:
    python memroach_web.py                  # http://127.0.0.1:8080
    python memroach_web.py --port 9090      # custom port
    python memroach_web.py --host 0.0.0.0   # LAN access
"""

import gzip
import json
import os
import random
import re
import ssl
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pg8000.native
from decimal import Decimal

try:
    from memroach_crypto import encrypt_blob, decrypt_blob
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route


class _Encoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return int(o)
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return json.dumps(content, cls=_Encoder, separators=(",", ":")).encode("utf-8")

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "memroach_config.json"

# ---------------------------------------------------------------------------
# DB helpers (reused from memroach_mcp_server.py)
# ---------------------------------------------------------------------------
_conn = None


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"memroach_config.json not found at {CONFIG_FILE}")
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
    return _load_config()["db_user"]


def _human_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _iso(dt) -> str:
    if dt is None:
        return ""
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

async def api_stats(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()

    # File counts by type
    type_rows = conn.run(
        "SELECT file_type, COUNT(*), SUM(file_size) "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "GROUP BY file_type ORDER BY file_type",
        user=user,
    )
    by_type = {}
    total_files = 0
    total_size = 0
    for ftype, count, size in type_rows:
        by_type[ftype] = {"count": count, "size": size or 0}
        total_files += count
        total_size += size or 0

    # Machines
    machines = conn.run(
        "SELECT machine_id, COUNT(*), MAX(synced_at) "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "GROUP BY machine_id ORDER BY MAX(synced_at) DESC",
        user=user,
    )
    machine_list = [
        {"machine_id": m, "file_count": c, "last_sync": _iso(s)}
        for m, c, s in machines
    ]

    # Recent activity from history
    activity = conn.run(
        "SELECT file_path, operation, created_at, machine_id "
        "FROM memroach_history "
        "WHERE user_name = :user "
        "ORDER BY created_at DESC LIMIT 10",
        user=user,
    )
    recent = [
        {"path": r[0], "operation": r[1], "timestamp": _iso(r[2]), "machine_id": r[3]}
        for r in activity
    ]

    # Embedding count
    embed_rows = conn.run(
        "SELECT COUNT(*) FROM memroach_embeddings WHERE user_name = :user",
        user=user,
    )
    embed_count = embed_rows[0][0] if embed_rows else 0

    # Link count
    link_rows = conn.run(
        "SELECT COUNT(*) FROM memroach_links WHERE user_name = :user",
        user=user,
    )
    link_count = link_rows[0][0] if link_rows else 0

    return SafeJSONResponse({
        "total_files": total_files,
        "total_size": total_size,
        "total_size_human": _human_size(total_size),
        "by_type": by_type,
        "machines": machine_list,
        "recent_activity": recent,
        "embedding_count": embed_count,
        "link_count": link_count,
    })


async def api_files(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()

    file_type = request.query_params.get("type")
    visibility = request.query_params.get("visibility")
    machine = request.query_params.get("machine")
    sort = request.query_params.get("sort", "synced_at")
    order = request.query_params.get("order", "desc")
    page = int(request.query_params.get("page", 1))
    per_page = int(request.query_params.get("per_page", 50))
    q = request.query_params.get("q", "")

    # Build query
    conditions = ["user_name = :user", "is_deleted = false"]
    params = {"user": user}

    if file_type:
        conditions.append("file_type = :ftype")
        params["ftype"] = file_type
    if visibility:
        conditions.append("visibility = :vis")
        params["vis"] = visibility
    if machine:
        conditions.append("machine_id = :machine")
        params["machine"] = machine
    if q:
        conditions.append("file_path ILIKE :q")
        params["q"] = f"%{q}%"

    where = " AND ".join(conditions)

    # Whitelist sort columns
    sort_col = "synced_at"
    if sort in ("file_path", "file_type", "file_size", "synced_at", "visibility"):
        sort_col = sort
    order_dir = "DESC" if order.lower() == "desc" else "ASC"

    # Count
    count_rows = conn.run(f"SELECT COUNT(*) FROM memroach_files WHERE {where}", **params)
    total = count_rows[0][0] if count_rows else 0

    offset = (page - 1) * per_page
    rows = conn.run(
        f"SELECT file_path, file_type, file_size, visibility, synced_at, machine_id, version "
        f"FROM memroach_files WHERE {where} "
        f"ORDER BY {sort_col} {order_dir} LIMIT :lim OFFSET :off",
        **params, lim=per_page, off=offset,
    )

    files = [
        {
            "path": r[0], "type": r[1], "size": r[2], "visibility": r[3],
            "synced_at": _iso(r[4]), "machine_id": r[5], "version": r[6],
        }
        for r in rows
    ]

    return SafeJSONResponse({
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "files": files,
    })


async def api_file_detail(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    file_path = request.path_params["file_path"]

    rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility, "
        "f.synced_at, f.machine_id, f.version, b.content_bytes "
        "FROM memroach_files f "
        "JOIN memroach_blobs b ON f.content_hash = b.content_hash "
        "WHERE f.user_name = :user AND f.file_path = :path AND f.is_deleted = false "
        "ORDER BY f.synced_at DESC LIMIT 1",
        user=user, path=file_path,
    )

    if not rows:
        return SafeJSONResponse({"error": "Not found"}, status_code=404)

    r = rows[0]
    config = _load_config()
    try:
        decrypted = decrypt_blob(conn, r[7], config) if HAS_CRYPTO else r[7]
        content = gzip.decompress(decrypted).decode("utf-8")
    except Exception:
        content = "[Binary content]"

    return SafeJSONResponse({
        "path": r[0], "type": r[1], "size": r[2], "visibility": r[3],
        "synced_at": _iso(r[4]), "machine_id": r[5], "version": r[6],
        "content": content,
    })


async def api_file_history(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    file_path = request.path_params["file_path"]
    limit = int(request.query_params.get("limit", 20))

    rows = conn.run(
        "SELECT version, operation, content_hash, file_size, machine_id, created_at "
        "FROM memroach_history "
        "WHERE user_name = :user AND file_path = :path "
        "ORDER BY created_at DESC LIMIT :lim",
        user=user, path=file_path, lim=limit,
    )

    versions = [
        {
            "version": r[0], "operation": r[1], "content_hash": r[2][:12],
            "size": r[3], "machine_id": r[4], "timestamp": _iso(r[5]),
        }
        for r in rows
    ]

    return SafeJSONResponse({"path": file_path, "version_count": len(versions), "versions": versions})


async def api_file_history_content(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    file_path = request.path_params["file_path"]
    content_hash = request.path_params["content_hash"]

    # Verify access
    rows = conn.run(
        "SELECT h.content_hash FROM memroach_history h "
        "WHERE h.user_name = :user AND h.file_path = :path "
        "AND h.content_hash LIKE :hash",
        user=user, path=file_path, hash=f"{content_hash}%",
    )
    if not rows:
        return SafeJSONResponse({"error": "Not found"}, status_code=404)

    full_hash = rows[0][0]
    blob = conn.run(
        "SELECT content_bytes FROM memroach_blobs WHERE content_hash = :hash",
        hash=full_hash,
    )
    if not blob:
        return SafeJSONResponse({"error": "Blob not found"}, status_code=404)

    try:
        config = _load_config()
        decrypted = decrypt_blob(conn, blob[0][0], config) if HAS_CRYPTO else blob[0][0]
        content = gzip.decompress(decrypted).decode("utf-8")
    except Exception:
        content = "[Binary content]"

    return SafeJSONResponse({"path": file_path, "content_hash": content_hash, "content": content})


async def api_file_graph(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    file_path = request.path_params["file_path"]

    outgoing = conn.run(
        "SELECT to_path, link_type, created_at FROM memroach_links "
        "WHERE user_name = :user AND from_path = :path",
        user=user, path=file_path,
    )
    incoming = conn.run(
        "SELECT from_path, link_type, created_at FROM memroach_links "
        "WHERE user_name = :user AND to_path = :path",
        user=user, path=file_path,
    )

    return SafeJSONResponse({
        "path": file_path,
        "outgoing": [{"path": r[0], "type": r[1], "created_at": _iso(r[2])} for r in outgoing],
        "incoming": [{"path": r[0], "type": r[1], "created_at": _iso(r[2])} for r in incoming],
        "total_links": len(outgoing) + len(incoming),
    })


async def api_search(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    config = _load_config()
    query = request.query_params.get("q", "")
    limit = int(request.query_params.get("limit", 10))

    if not query:
        return SafeJSONResponse({"query": "", "count": 0, "results": []})

    # Try hybrid search
    try:
        from memroach_embed import embed_texts, hybrid_search
        if config.get("embed_api_key"):
            query_embedding = embed_texts([query], config)[0]
            results = hybrid_search(conn, user, query_embedding, query, limit)
            return SafeJSONResponse({
                "query": query,
                "search_type": "hybrid",
                "count": len(results),
                "results": [
                    {
                        "path": r["path"], "type": r.get("type", "file"),
                        "size": r.get("size", 0), "visibility": r.get("visibility", "private"),
                        "score": round(r["score"], 3), "snippet": r.get("snippet", ""),
                        "synced_at": _iso(r.get("synced_at")),
                    }
                    for r in results
                ],
            })
    except Exception:
        pass

    # Keyword fallback (path-only search — content search not possible with encryption)
    rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.visibility, f.synced_at "
        "FROM memroach_files f "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_path ILIKE :q "
        "ORDER BY f.synced_at DESC LIMIT :lim",
        user=user, q=f"%{query}%", lim=limit,
    )

    return SafeJSONResponse({
        "query": query,
        "search_type": "keyword",
        "count": len(rows),
        "results": [
            {
                "path": r[0], "type": r[1], "size": r[2],
                "visibility": r[3], "score": 0.5, "snippet": "",
                "synced_at": _iso(r[4]),
            }
            for r in rows
        ],
    })


async def api_graph(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    root = request.query_params.get("root")

    if root:
        # Ego graph: links connected to root
        links = conn.run(
            "SELECT from_path, to_path, link_type FROM memroach_links "
            "WHERE user_name = :user AND (from_path = :root OR to_path = :root)",
            user=user, root=root,
        )
    else:
        # Full graph
        links = conn.run(
            "SELECT from_path, to_path, link_type FROM memroach_links "
            "WHERE user_name = :user",
            user=user,
        )

    # Build node set
    node_ids = set()
    edges = []
    for from_p, to_p, ltype in links:
        node_ids.add(from_p)
        node_ids.add(to_p)
        edges.append({"source": from_p, "target": to_p, "link_type": ltype})

    # Get file metadata for nodes
    nodes = []
    for nid in node_ids:
        rows = conn.run(
            "SELECT file_type, file_size FROM memroach_files "
            "WHERE user_name = :user AND file_path = :path AND is_deleted = false "
            "LIMIT 1",
            user=user, path=nid,
        )
        if rows:
            nodes.append({"id": nid, "type": rows[0][0], "size": rows[0][1]})
        else:
            nodes.append({"id": nid, "type": "file", "size": 0})

    return SafeJSONResponse({"nodes": nodes, "links": edges})


async def api_timeline(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    limit = int(request.query_params.get("limit", 100))
    machine = request.query_params.get("machine")
    since = request.query_params.get("since")

    conditions = ["user_name = :user"]
    params = {"user": user, "lim": limit}

    if machine:
        conditions.append("machine_id = :machine")
        params["machine"] = machine
    if since:
        conditions.append("created_at >= :since::TIMESTAMPTZ")
        params["since"] = since

    where = " AND ".join(conditions)
    rows = conn.run(
        f"SELECT file_path, version, operation, machine_id, file_size, created_at "
        f"FROM memroach_history WHERE {where} "
        f"ORDER BY created_at DESC LIMIT :lim",
        **params,
    )

    entries = [
        {
            "path": r[0], "version": r[1], "operation": r[2],
            "machine_id": r[3], "size": r[4], "timestamp": _iso(r[5]),
        }
        for r in rows
    ]

    return SafeJSONResponse({"count": len(entries), "entries": entries})


async def api_team_files(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()

    rows = conn.run(
        "SELECT user_name, file_path, file_type, file_size, synced_at "
        "FROM memroach_files "
        "WHERE visibility = 'team' AND is_deleted = false "
        "ORDER BY user_name, synced_at DESC",
    )

    files = [
        {
            "owner": r[0], "path": r[1], "type": r[2],
            "size": r[3], "synced_at": _iso(r[4]),
        }
        for r in rows
    ]

    return SafeJSONResponse({"count": len(files), "files": files})


async def api_team_search(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    config = _load_config()
    query = request.query_params.get("q", "")
    limit = int(request.query_params.get("limit", 10))

    if not query:
        return SafeJSONResponse({"query": "", "count": 0, "results": []})

    try:
        from memroach_embed import embed_texts, hybrid_search
        if config.get("embed_api_key"):
            query_embedding = embed_texts([query], config)[0]
            results = hybrid_search(conn, user, query_embedding, query, limit,
                                    visibility="team")
            return SafeJSONResponse({
                "query": query, "search_type": "hybrid", "count": len(results),
                "results": [
                    {
                        "path": r["path"], "type": r.get("type", "file"),
                        "owner": r.get("owner", user), "score": round(r["score"], 3),
                        "snippet": r.get("snippet", ""), "synced_at": _iso(r.get("synced_at")),
                    }
                    for r in results
                ],
            })
    except Exception:
        pass

    rows = conn.run(
        "SELECT user_name, file_path, file_type, file_size, synced_at "
        "FROM memroach_files "
        "WHERE visibility = 'team' AND is_deleted = false "
        "AND file_path ILIKE :q "
        "ORDER BY synced_at DESC LIMIT :lim",
        q=f"%{query}%", lim=limit,
    )
    return SafeJSONResponse({
        "query": query, "search_type": "keyword", "count": len(rows),
        "results": [
            {"path": r[1], "type": r[2], "owner": r[0], "score": 0.5,
             "snippet": "", "synced_at": _iso(r[4])}
            for r in rows
        ],
    })


async def api_compact_candidates(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    max_age = int(request.query_params.get("max_age_days", 30))
    min_size = int(request.query_params.get("min_size", 2000))
    limit = int(request.query_params.get("limit", 20))

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.synced_at "
        "FROM memroach_files f "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "AND f.file_size >= :min_size "
        "AND f.synced_at <= :cutoff::TIMESTAMPTZ "
        "ORDER BY f.file_size DESC LIMIT :lim",
        user=user, min_size=min_size, cutoff=cutoff, lim=limit,
    )

    candidates = []
    for r in rows:
        # Get access info
        access = conn.run(
            "SELECT COUNT(*), MAX(accessed_at) FROM memroach_access "
            "WHERE user_name = :user AND file_path = :path",
            user=user, path=r[0],
        )
        acc_count = access[0][0] if access else 0
        last_acc = _iso(access[0][1]) if access and access[0][1] else "never"

        candidates.append({
            "path": r[0], "type": r[1], "size": r[2],
            "synced_at": _iso(r[3]), "access_count": acc_count,
            "last_accessed": last_acc,
        })

    return SafeJSONResponse({"count": len(candidates), "candidates": candidates})


async def api_access_heatmap(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    days = int(request.query_params.get("days", 30))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = conn.run(
        "SELECT file_path, COUNT(*) as cnt, MAX(accessed_at) as last_acc "
        "FROM memroach_access "
        "WHERE user_name = :user AND accessed_at >= :since::TIMESTAMPTZ "
        "GROUP BY file_path ORDER BY cnt DESC LIMIT 50",
        user=user, since=since,
    )

    entries = [
        {"path": r[0], "access_count": r[1], "last_accessed": _iso(r[2])}
        for r in rows
    ]

    return SafeJSONResponse({"days": days, "entries": entries})


async def api_sync_status(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()

    machines = conn.run(
        "SELECT machine_id, COUNT(*), MAX(synced_at) "
        "FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "GROUP BY machine_id ORDER BY MAX(synced_at) DESC",
        user=user,
    )

    logs = conn.run(
        "SELECT operation, files_changed, bytes_transferred, completed_at, machine_id "
        "FROM memroach_log WHERE user_name = :user "
        "ORDER BY completed_at DESC LIMIT 20",
        user=user,
    )

    return SafeJSONResponse({
        "machines": [
            {"machine_id": m[0], "file_count": m[1], "last_sync": _iso(m[2])}
            for m in machines
        ],
        "recent_logs": [
            {
                "operation": l[0], "files_changed": l[1],
                "bytes_transferred": l[2], "completed_at": _iso(l[3]),
                "machine_id": l[4],
            }
            for l in logs
        ],
    })


# ---------------------------------------------------------------------------
# Insight APIs
# ---------------------------------------------------------------------------

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "out", "off",
    "over", "under", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "no", "nor", "not", "only",
    "own", "same", "so", "than", "too", "very", "just", "because", "but",
    "and", "or", "if", "while", "this", "that", "these", "those", "it",
    "its", "i", "me", "my", "we", "our", "you", "your", "he", "him",
    "his", "she", "her", "they", "them", "their", "what", "which", "who",
    "whom", "use", "using", "used", "also", "e", "g", "etc", "ie",
}


async def api_insights_health(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    # Total memory/skill files
    total_rows = conn.run(
        "SELECT COUNT(*) FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false AND file_type IN ('memory', 'skill')",
        user=user,
    )
    total_mem_skill = total_rows[0][0] if total_rows else 0

    # Stale: memory/skill not accessed in 30+ days
    stale_rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.synced_at "
        "FROM memroach_files f "
        "LEFT JOIN (SELECT file_path, MAX(accessed_at) as last_acc "
        "           FROM memroach_access WHERE user_name = :user GROUP BY file_path) a "
        "  ON f.file_path = a.file_path "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "AND (a.last_acc IS NULL OR a.last_acc < :cutoff::TIMESTAMPTZ) "
        "ORDER BY f.synced_at ASC LIMIT 20",
        user=user, cutoff=cutoff_30d,
    )
    stale = [{"path": r[0], "type": r[1], "size": r[2], "synced_at": _iso(r[3])} for r in stale_rows]

    # Orphaned: memory/skill with no graph links
    orphaned_rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size "
        "FROM memroach_files f "
        "LEFT JOIN memroach_links lo ON f.user_name = lo.user_name AND f.file_path = lo.from_path "
        "LEFT JOIN memroach_links li ON f.user_name = li.user_name AND f.file_path = li.to_path "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "AND lo.id IS NULL AND li.id IS NULL "
        "ORDER BY f.file_size DESC LIMIT 20",
        user=user,
    )
    orphaned = [{"path": r[0], "type": r[1], "size": r[2]} for r in orphaned_rows]

    # Oversized: largest memory/skill files
    oversized_rows = conn.run(
        "SELECT file_path, file_type, file_size FROM memroach_files "
        "WHERE user_name = :user AND is_deleted = false "
        "AND file_type IN ('memory', 'skill') "
        "ORDER BY file_size DESC LIMIT 10",
        user=user,
    )
    oversized = [{"path": r[0], "type": r[1], "size": r[2]} for r in oversized_rows]

    # Version churn: most frequently updated
    churn_rows = conn.run(
        "SELECT file_path, COUNT(*) as changes FROM memroach_history "
        "WHERE user_name = :user GROUP BY file_path "
        "ORDER BY changes DESC LIMIT 10",
        user=user,
    )
    churn = [{"path": r[0], "changes": r[1]} for r in churn_rows]

    # Embedding coverage
    embed_rows = conn.run(
        "SELECT COUNT(DISTINCT file_path) FROM memroach_embeddings WHERE user_name = :user",
        user=user,
    )
    embedded_count = embed_rows[0][0] if embed_rows else 0
    coverage = round(embedded_count / max(1, total_mem_skill) * 100, 1)

    return SafeJSONResponse({
        "total_memories": total_mem_skill,
        "stale": {"count": len(stale), "files": stale},
        "orphaned": {"count": len(orphaned), "files": orphaned},
        "oversized": {"files": oversized},
        "churn": {"files": churn},
        "embedding_coverage": coverage,
        "embedded_count": embedded_count,
    })


async def api_insights_analytics(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    days = int(request.query_params.get("days", 90))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Daily activity
    daily_rows = conn.run(
        "SELECT DATE_TRUNC('day', created_at)::DATE as d, operation, COUNT(*) "
        "FROM memroach_history "
        "WHERE user_name = :user AND created_at >= :since::TIMESTAMPTZ "
        "GROUP BY d, operation ORDER BY d",
        user=user, since=since,
    )
    daily = {}
    for d, op, cnt in daily_rows:
        ds = str(d)
        if ds not in daily:
            daily[ds] = {"date": ds, "create": 0, "update": 0, "delete": 0, "total": 0}
        daily[ds][op] = int(cnt)
        daily[ds]["total"] += int(cnt)
    daily_list = list(daily.values())

    # Machine activity
    machine_rows = conn.run(
        "SELECT machine_id, operation, COUNT(*) FROM memroach_log "
        "WHERE user_name = :user AND completed_at >= :since::TIMESTAMPTZ "
        "GROUP BY machine_id, operation ORDER BY COUNT(*) DESC",
        user=user, since=since,
    )
    machines = {}
    for mid, op, cnt in machine_rows:
        if mid not in machines:
            machines[mid] = {"machine_id": mid, "push": 0, "pull": 0, "total": 0}
        machines[mid][op] = int(cnt)
        machines[mid]["total"] += int(cnt)
    machine_list = sorted(machines.values(), key=lambda m: m["total"], reverse=True)

    # Most churned files
    churn_rows = conn.run(
        "SELECT file_path, COUNT(*) as changes FROM memroach_history "
        "WHERE user_name = :user AND created_at >= :since::TIMESTAMPTZ "
        "GROUP BY file_path ORDER BY changes DESC LIMIT 15",
        user=user, since=since,
    )
    churned = [{"path": r[0], "changes": int(r[1])} for r in churn_rows]

    # Summary stats
    total_ops = sum(d["total"] for d in daily_list)
    busiest = max(daily_list, key=lambda d: d["total"]) if daily_list else None

    return SafeJSONResponse({
        "days": days,
        "daily_activity": daily_list,
        "machine_activity": machine_list,
        "most_churned": churned,
        "total_operations": total_ops,
        "busiest_day": busiest,
    })


async def api_insights_duplicates(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    threshold = float(request.query_params.get("threshold", 0.85))
    limit = int(request.query_params.get("limit", 20))

    # Fetch one embedding per file (chunk_index=0)
    rows = conn.run(
        "SELECT e.file_path, e.embedding, e.chunk_text "
        "FROM memroach_embeddings e "
        "JOIN memroach_files f ON e.user_name = f.user_name AND e.file_path = f.file_path "
        "WHERE e.user_name = :user AND e.chunk_index = 0 "
        "AND f.is_deleted = false AND f.file_type IN ('memory', 'skill')",
        user=user,
    )

    if len(rows) < 2:
        return SafeJSONResponse({"count": 0, "threshold": threshold, "pairs": []})

    paths = [r[0] for r in rows]
    snippets = [r[2][:200] for r in rows]

    # Parse embeddings - handle string format from CockroachDB
    vectors = []
    for r in rows:
        emb = r[1]
        if isinstance(emb, str):
            emb = [float(x) for x in emb.strip("[]").split(",")]
        vectors.append(emb)
    vectors = np.array(vectors, dtype=np.float32)

    # Normalize for cosine similarity
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    normed = vectors / norms

    # Pairwise similarity (upper triangle only)
    pairs = []
    n = len(normed)
    for i in range(n):
        if len(pairs) >= limit * 2:
            break
        sims = normed[i] @ normed[i+1:].T
        for j_offset, sim in enumerate(sims):
            if sim >= threshold:
                j = i + 1 + j_offset
                pairs.append({
                    "similarity": round(float(sim), 3),
                    "file_a": paths[i], "snippet_a": snippets[i],
                    "file_b": paths[j], "snippet_b": snippets[j],
                })

    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    return SafeJSONResponse({"count": len(pairs[:limit]), "threshold": threshold, "pairs": pairs[:limit]})


def _kmeans(vectors: np.ndarray, k: int, max_iter: int = 20):
    n = len(vectors)
    if n <= k:
        return np.arange(n), vectors.copy()
    idx = np.random.choice(n, k, replace=False)
    centroids = vectors[idx].copy()
    for _ in range(max_iter):
        # Assign: cosine similarity (vectors already normalized)
        sims = vectors @ centroids.T
        labels = np.argmax(sims, axis=1)
        new_centroids = np.zeros_like(centroids)
        for i in range(k):
            mask = labels == i
            if np.any(mask):
                c = vectors[mask].mean(axis=0)
                norm = np.linalg.norm(c)
                new_centroids[i] = c / norm if norm > 0 else centroids[i]
            else:
                new_centroids[i] = centroids[i]
        if np.allclose(centroids, new_centroids, atol=1e-6):
            break
        centroids = new_centroids
    return labels, centroids


async def api_insights_topics(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    k = int(request.query_params.get("clusters", 8))

    # Fetch embeddings with chunk text
    rows = conn.run(
        "SELECT e.file_path, e.embedding, e.chunk_text "
        "FROM memroach_embeddings e "
        "JOIN memroach_files f ON e.user_name = f.user_name AND e.file_path = f.file_path "
        "WHERE e.user_name = :user AND e.chunk_index = 0 "
        "AND f.is_deleted = false AND f.file_type IN ('memory', 'skill')",
        user=user,
    )

    if len(rows) < 2:
        return SafeJSONResponse({"count": 0, "clusters": []})

    paths = [r[0] for r in rows]
    texts = [r[2] for r in rows]

    vectors = []
    for r in rows:
        emb = r[1]
        if isinstance(emb, str):
            emb = [float(x) for x in emb.strip("[]").split(",")]
        vectors.append(emb)
    vectors = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    vectors = vectors / norms

    k = min(k, len(vectors))
    labels, centroids = _kmeans(vectors, k)

    clusters = []
    for i in range(k):
        mask = labels == i
        cluster_paths = [paths[j] for j in range(len(paths)) if mask[j]]
        cluster_texts = [texts[j] for j in range(len(texts)) if mask[j]]

        if not cluster_paths:
            continue

        # Generate label from top words
        all_words = []
        for t in cluster_texts:
            words = re.findall(r'[a-zA-Z]{3,}', t.lower())
            all_words.extend(w for w in words if w not in STOPWORDS)
        top_words = [w for w, _ in Counter(all_words).most_common(4)]
        label = ", ".join(top_words) if top_words else f"Cluster {i+1}"

        # Find representatives (closest to centroid)
        cluster_vecs = vectors[mask]
        sims = cluster_vecs @ centroids[i]
        rep_idx = np.argsort(sims)[-3:][::-1]
        reps = [cluster_paths[int(ri)] for ri in rep_idx if ri < len(cluster_paths)]

        clusters.append({
            "label": label,
            "file_count": len(cluster_paths),
            "files": cluster_paths,
            "representatives": reps,
        })

    clusters.sort(key=lambda c: c["file_count"], reverse=True)
    return SafeJSONResponse({"count": len(clusters), "clusters": clusters})


async def api_insights_discover(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()

    # Fetch metadata only (no content blobs) for weighting
    rows = conn.run(
        "SELECT f.file_path, f.file_type, f.file_size, f.file_mtime, f.content_hash "
        "FROM memroach_files f "
        "WHERE f.user_name = :user AND f.is_deleted = false "
        "AND f.file_type IN ('memory', 'skill') "
        "ORDER BY RANDOM() LIMIT 200",
        user=user,
    )

    if not rows:
        return SafeJSONResponse({"found": False})

    # Batch fetch access counts in one query
    access_rows = conn.run(
        "SELECT file_path, COUNT(*) FROM memroach_access "
        "WHERE user_name = :user GROUP BY file_path",
        user=user,
    )
    access_map = {r[0]: int(r[1]) for r in access_rows}

    # Weight: older + less accessed = more interesting
    now = datetime.now(timezone.utc)
    candidates = []
    for r in rows:
        try:
            mtime = r[3]
            if hasattr(mtime, 'tzinfo') and mtime.tzinfo is None:
                mtime = mtime.replace(tzinfo=timezone.utc)
            days_old = max(1, (now - mtime).days)
        except Exception:
            days_old = 30

        acc_count = access_map.get(r[0], 0)
        type_bonus = 2.0 if r[1] == "memory" else 1.5
        weight = (days_old / 30.0) * (1.0 / (acc_count + 1)) * type_bonus
        candidates.append((r, weight, acc_count, days_old))

    # Weighted random selection
    weights = np.array([c[1] for c in candidates])
    weights = weights / weights.sum()
    idx = np.random.choice(len(candidates), p=weights)
    chosen = candidates[idx]
    r, _, acc_count, days_old = chosen

    # Fetch content only for the chosen file
    config = _load_config()
    blob = conn.run(
        "SELECT content_bytes FROM memroach_blobs WHERE content_hash = :hash",
        hash=r[4],
    )
    try:
        if blob:
            decrypted = decrypt_blob(conn, blob[0][0], config) if HAS_CRYPTO else blob[0][0]
            content = gzip.decompress(decrypted).decode("utf-8")
        else:
            content = "[Content not found]"
    except Exception:
        content = "[Binary content]"

    return SafeJSONResponse({
        "found": True,
        "path": r[0], "type": r[1], "size": r[2],
        "synced_at": _iso(r[3]), "days_old": days_old,
        "access_count": acc_count, "content": content,
    })


# Write operations
async def api_file_share(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    file_path = request.path_params["file_path"]
    body = await request.json()
    visibility = body.get("visibility", "team")

    if visibility not in ("private", "team"):
        return SafeJSONResponse({"error": "visibility must be 'private' or 'team'"}, status_code=400)

    conn.run(
        "UPDATE memroach_files SET visibility = :vis "
        "WHERE user_name = :user AND file_path = :path AND is_deleted = false",
        user=user, path=file_path, vis=visibility,
    )

    return SafeJSONResponse({"path": file_path, "visibility": visibility})


async def api_merge(request: Request) -> JSONResponse:
    """Merge two memories: combine content into file_a, mark file_b as superseded."""
    conn = _get_conn()
    user = _get_user()
    body = await request.json()
    path_a = body.get("file_a", "")
    path_b = body.get("file_b", "")

    if not path_a or not path_b:
        return SafeJSONResponse({"error": "file_a and file_b required"}, status_code=400)

    # Fetch both files' content
    rows_a = conn.run(
        "SELECT b.content_bytes, f.file_type, f.visibility, f.version "
        "FROM memroach_files f JOIN memroach_blobs b ON f.content_hash = b.content_hash "
        "WHERE f.user_name = :user AND f.file_path = :path AND f.is_deleted = false LIMIT 1",
        user=user, path=path_a,
    )
    rows_b = conn.run(
        "SELECT b.content_bytes, f.file_type, f.visibility, f.version "
        "FROM memroach_files f JOIN memroach_blobs b ON f.content_hash = b.content_hash "
        "WHERE f.user_name = :user AND f.file_path = :path AND f.is_deleted = false LIMIT 1",
        user=user, path=path_b,
    )

    if not rows_a or not rows_b:
        return SafeJSONResponse({"error": "One or both files not found"}, status_code=404)

    config = _load_config()
    try:
        dec_a = decrypt_blob(conn, rows_a[0][0], config) if HAS_CRYPTO else rows_a[0][0]
        dec_b = decrypt_blob(conn, rows_b[0][0], config) if HAS_CRYPTO else rows_b[0][0]
        content_a = gzip.decompress(dec_a).decode("utf-8")
        content_b = gzip.decompress(dec_b).decode("utf-8")
    except Exception:
        return SafeJSONResponse({"error": "Cannot decode file contents"}, status_code=500)

    # Merge: append B's content under a separator
    merged = content_a.rstrip() + "\n\n---\n\n" + f"*Merged from: {path_b}*\n\n" + content_b

    # Store merged content
    import hashlib
    compressed = gzip.compress(merged.encode("utf-8"))
    blob_data = encrypt_blob(conn, compressed, config) if HAS_CRYPTO else compressed
    content_hash = hashlib.sha256(merged.encode("utf-8")).hexdigest()

    # Insert blob
    conn.run(
        "INSERT INTO memroach_blobs (content_hash, content_bytes, original_size) "
        "VALUES (:hash, :data, :size) "
        "ON CONFLICT (content_hash) DO NOTHING",
        hash=content_hash, data=blob_data, size=len(merged),
    )
    machine_id = config.get("machine_id", "web-ui")
    conn.run(
        "UPDATE memroach_files SET content_hash = :hash, file_size = :size, "
        "version = version + 1, synced_at = now() "
        "WHERE user_name = :user AND file_path = :path AND is_deleted = false",
        user=user, path=path_a, hash=content_hash, size=len(merged),
    )

    # Mark file_b as deleted
    conn.run(
        "UPDATE memroach_files SET is_deleted = true "
        "WHERE user_name = :user AND file_path = :path",
        user=user, path=path_b,
    )

    # Create supersedes link
    conn.run(
        "INSERT INTO memroach_links (user_name, from_path, to_path, link_type) "
        "VALUES (:user, :from, :to, 'supersedes') "
        "ON CONFLICT (user_name, from_path, to_path, link_type) DO NOTHING",
        user=user, **{"from": path_a, "to": path_b},
    )

    # Record history
    new_ver = rows_a[0][3] + 1
    conn.run(
        "INSERT INTO memroach_history "
        "(user_name, machine_id, file_path, content_hash, file_size, version, operation) "
        "VALUES (:user, :machine, :path, :hash, :size, :ver, 'update')",
        user=user, machine=machine_id, path=path_a, hash=content_hash,
        size=len(merged), ver=new_ver,
    )

    return SafeJSONResponse({
        "merged": True, "merged_into": path_a,
        "superseded": path_b, "new_size": len(merged),
    })


async def api_links_create(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    body = await request.json()

    from_path = body.get("from_path", "")
    to_path = body.get("to_path", "")
    link_type = body.get("link_type", "relates_to")

    if not from_path or not to_path:
        return SafeJSONResponse({"error": "from_path and to_path required"}, status_code=400)

    conn.run(
        "INSERT INTO memroach_links (user_name, from_path, to_path, link_type) "
        "VALUES (:user, :from, :to, :type) "
        "ON CONFLICT (user_name, from_path, to_path, link_type) DO NOTHING",
        user=user, **{"from": from_path, "to": to_path, "type": link_type},
    )

    # Bidirectional for relates_to
    if link_type == "relates_to":
        conn.run(
            "INSERT INTO memroach_links (user_name, from_path, to_path, link_type) "
            "VALUES (:user, :from, :to, 'relates_to') "
            "ON CONFLICT (user_name, from_path, to_path, link_type) DO NOTHING",
            user=user, **{"from": to_path, "to": from_path},
        )

    return SafeJSONResponse({"linked": True, "from": from_path, "to": to_path, "type": link_type})


async def api_links_delete(request: Request) -> JSONResponse:
    conn = _get_conn()
    user = _get_user()
    body = await request.json()

    from_path = body.get("from_path", "")
    to_path = body.get("to_path", "")
    link_type = body.get("link_type")

    if link_type:
        conn.run(
            "DELETE FROM memroach_links "
            "WHERE user_name = :user AND from_path = :from AND to_path = :to AND link_type = :type",
            user=user, **{"from": from_path, "to": to_path, "type": link_type},
        )
    else:
        conn.run(
            "DELETE FROM memroach_links "
            "WHERE user_name = :user AND from_path = :from AND to_path = :to",
            user=user, **{"from": from_path, "to": to_path},
        )

    return SafeJSONResponse({"unlinked": True, "from": from_path, "to": to_path})


# ---------------------------------------------------------------------------
# Frontend — Single Page Application
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemRoach</title>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2129;
  --border: #30363d; --text: #e6edf3; --text2: #8b949e;
  --accent: #58a6ff; --green: #3fb950; --orange: #f78166;
  --red: #f85149; --purple: #bc8cff; --amber: #d29922;
  --radius: 8px; --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: var(--font); background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }

/* Sidebar */
.sidebar {
  width: 220px; background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; flex-shrink: 0;
}
.sidebar-logo {
  padding: 20px 16px; font-size: 18px; font-weight: 700;
  border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px;
}
.sidebar-logo span { color: var(--green); }
.sidebar nav { flex: 1; padding: 8px 0; }
.sidebar nav a {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px; color: var(--text2); text-decoration: none;
  font-size: 14px; border-left: 3px solid transparent; transition: all 0.15s;
}
.sidebar nav a:hover { background: var(--surface2); color: var(--text); }
.sidebar nav a.active { color: var(--accent); border-left-color: var(--accent); background: var(--surface2); }
.sidebar nav a .icon { width: 18px; text-align: center; font-size: 15px; }

/* Main content */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.topbar {
  padding: 12px 24px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 16px; background: var(--surface);
}
.topbar h1 { font-size: 16px; font-weight: 600; }
.content { flex: 1; overflow-y: auto; padding: 24px; }

/* Cards */
.stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; text-align: center;
}
.stat-card .value { font-size: 28px; font-weight: 700; color: var(--accent); }
.stat-card .label { font-size: 12px; color: var(--text2); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }

/* Tables */
.table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; padding: 12px 16px; background: var(--surface2); color: var(--text2); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); cursor: pointer; }
td { padding: 10px 16px; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--surface2); }
tr.clickable { cursor: pointer; }

/* Badges */
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
}
.badge-memory { background: #1f3a5f; color: var(--accent); }
.badge-skill { background: #2d1f4e; color: var(--purple); }
.badge-config { background: #3d2e0a; color: var(--amber); }
.badge-session { background: #1c2129; color: var(--text2); }
.badge-file { background: #1c2129; color: var(--text2); }
.badge-create { background: #0f2d1a; color: var(--green); }
.badge-update { background: #1f3a5f; color: var(--accent); }
.badge-delete { background: #3d1418; color: var(--red); }
.badge-private { background: #1c2129; color: var(--text2); }
.badge-team { background: #0f2d1a; color: var(--green); }

/* Filters */
.filters {
  display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; align-items: center;
}
.filters select, .filters input {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 8px 12px; border-radius: var(--radius); font-size: 13px; outline: none;
}
.filters select:focus, .filters input:focus { border-color: var(--accent); }

/* Pagination */
.pagination { display: flex; gap: 8px; margin-top: 16px; justify-content: center; align-items: center; }
.pagination button {
  background: var(--surface); border: 1px solid var(--border); color: var(--text);
  padding: 6px 14px; border-radius: var(--radius); cursor: pointer; font-size: 13px;
}
.pagination button:hover { border-color: var(--accent); }
.pagination button:disabled { opacity: 0.4; cursor: default; }
.pagination span { color: var(--text2); font-size: 13px; }

/* Memory Viewer */
.viewer-meta {
  display: flex; gap: 16px; flex-wrap: wrap; padding: 16px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-bottom: 16px; font-size: 13px;
}
.viewer-meta .meta-item { color: var(--text2); }
.viewer-meta .meta-item strong { color: var(--text); }
.viewer-content {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px; line-height: 1.7;
}
.viewer-content h1, .viewer-content h2, .viewer-content h3 { margin: 20px 0 10px; color: var(--text); }
.viewer-content h1 { font-size: 22px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
.viewer-content h2 { font-size: 18px; }
.viewer-content h3 { font-size: 15px; }
.viewer-content p { margin: 8px 0; }
.viewer-content code { background: var(--surface2); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.viewer-content pre { background: var(--surface2); padding: 16px; border-radius: var(--radius); overflow-x: auto; margin: 12px 0; }
.viewer-content pre code { background: none; padding: 0; }
.viewer-content ul, .viewer-content ol { padding-left: 24px; margin: 8px 0; }
.viewer-content table { border: 1px solid var(--border); margin: 12px 0; }
.viewer-content table th, .viewer-content table td { border: 1px solid var(--border); padding: 8px 12px; }
.viewer-content a { color: var(--accent); }
.viewer-content blockquote { border-left: 3px solid var(--border); padding-left: 16px; color: var(--text2); margin: 12px 0; }

.section-header {
  display: flex; align-items: center; gap: 8px; cursor: pointer;
  padding: 12px 16px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); margin-top: 16px; font-weight: 600; font-size: 14px;
}
.section-header:hover { background: var(--surface2); }
.section-body {
  border: 1px solid var(--border); border-top: none;
  border-radius: 0 0 var(--radius) var(--radius);
  padding: 16px; background: var(--surface);
}

/* Search */
.search-box {
  display: flex; gap: 12px; margin-bottom: 20px;
}
.search-box input {
  flex: 1; background: var(--surface); border: 1px solid var(--border);
  color: var(--text); padding: 12px 16px; border-radius: var(--radius);
  font-size: 15px; outline: none;
}
.search-box input:focus { border-color: var(--accent); }
.search-box button {
  background: var(--accent); color: #fff; border: none;
  padding: 12px 24px; border-radius: var(--radius); cursor: pointer;
  font-size: 14px; font-weight: 600;
}
.search-box button:hover { opacity: 0.9; }

.result-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; margin-bottom: 12px;
  cursor: pointer; transition: border-color 0.15s;
}
.result-card:hover { border-color: var(--accent); }
.result-card .result-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.result-card .score {
  background: var(--surface2); padding: 2px 8px; border-radius: 4px;
  font-size: 12px; font-weight: 700; color: var(--green);
}
.result-card .path { font-size: 14px; font-weight: 600; color: var(--accent); }
.result-card .snippet { font-size: 13px; color: var(--text2); line-height: 1.5; }

/* Graph */
#graph-container { width: 100%; height: calc(100vh - 180px); background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
#graph-container svg { width: 100%; height: 100%; }
.graph-tooltip {
  position: absolute; background: var(--surface2); border: 1px solid var(--border);
  padding: 8px 12px; border-radius: var(--radius); font-size: 12px;
  pointer-events: none; z-index: 100; display: none;
}
.graph-legend {
  display: flex; gap: 16px; margin-bottom: 12px; flex-wrap: wrap; font-size: 12px; color: var(--text2);
}
.graph-legend .legend-item { display: flex; align-items: center; gap: 4px; }
.graph-legend .legend-dot { width: 10px; height: 10px; border-radius: 50%; }

/* Timeline */
.timeline-entry {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px; border-left: 2px solid var(--border); margin-left: 8px;
}
.timeline-entry:hover { background: var(--surface); border-radius: 0 var(--radius) var(--radius) 0; }
.timeline-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; margin-left: -13px; }
.timeline-date { font-size: 13px; font-weight: 600; padding: 16px 0 8px; color: var(--text2); }

/* Heatmap bars */
.heatmap-bar-wrap { margin-bottom: 8px; }
.heatmap-label { font-size: 12px; color: var(--text2); margin-bottom: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 400px; }
.heatmap-bar { height: 20px; border-radius: 4px; background: var(--accent); min-width: 4px; transition: width 0.3s; }
.heatmap-count { font-size: 11px; color: var(--text2); margin-top: 1px; }

/* Sync */
.machine-card {
  display: flex; align-items: center; gap: 16px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; margin-bottom: 12px;
}
.machine-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.machine-info { flex: 1; }
.machine-info .name { font-weight: 600; font-size: 14px; }
.machine-info .detail { font-size: 12px; color: var(--text2); }

/* Utility */
.back-btn {
  display: inline-flex; align-items: center; gap: 6px;
  color: var(--accent); text-decoration: none; font-size: 13px;
  cursor: pointer; margin-bottom: 16px;
}
.back-btn:hover { text-decoration: underline; }
.empty { text-align: center; padding: 48px; color: var(--text2); font-size: 14px; }
.loading { text-align: center; padding: 48px; color: var(--text2); }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
@media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
.section-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; }
.action-btn {
  background: var(--surface2); border: 1px solid var(--border); color: var(--text);
  padding: 6px 14px; border-radius: var(--radius); cursor: pointer; font-size: 12px;
}
.action-btn:hover { border-color: var(--accent); }

/* Insights */
.health-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin-bottom: 24px; }
.health-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px;
}
.health-card .hc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.health-card .hc-title { font-weight: 600; font-size: 14px; }
.health-card .hc-value { font-size: 24px; font-weight: 700; }
.health-card .hc-list { font-size: 13px; max-height: 200px; overflow-y: auto; }
.health-card .hc-list a { color: var(--accent); text-decoration: none; }
.health-card .hc-list a:hover { text-decoration: underline; }
.health-card .hc-item { padding: 4px 0; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; }
.health-card .hc-item:last-child { border-bottom: none; }
.color-green { color: var(--green); }
.color-amber { color: var(--amber); }
.color-red { color: var(--red); }

/* Activity chart */
.chart-container { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 24px; }
.bar-chart { display: flex; align-items: flex-end; gap: 2px; height: 160px; }
.bar-chart .bar-group { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 0; height: 100%; justify-content: flex-end; }
.bar-chart .bar { min-width: 4px; border-radius: 2px 2px 0 0; position: relative; cursor: pointer; }
.bar-chart .bar:hover::after {
  content: attr(data-tooltip); position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
  background: var(--surface2); border: 1px solid var(--border); padding: 4px 8px; border-radius: 4px;
  font-size: 11px; white-space: nowrap; z-index: 10;
}
.bar-chart .bar-label { font-size: 9px; color: var(--text2); margin-top: 4px; writing-mode: vertical-rl; text-orientation: mixed; max-height: 40px; overflow: hidden; }

/* Topic cards */
.topic-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
.topic-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; cursor: pointer; transition: border-color 0.15s;
}
.topic-card:hover { border-color: var(--accent); }
.topic-card .tc-label { font-weight: 600; font-size: 15px; margin-bottom: 8px; color: var(--accent); text-transform: capitalize; }
.topic-card .tc-count { font-size: 12px; color: var(--text2); margin-bottom: 12px; }
.topic-card .tc-files { font-size: 12px; color: var(--text2); }
.topic-card .tc-files a { color: var(--text); text-decoration: none; display: block; padding: 2px 0; }
.topic-card .tc-files a:hover { color: var(--accent); }

/* Duplicate pairs */
.dup-pair {
  display: grid; grid-template-columns: 1fr auto 1fr; gap: 16px; align-items: start;
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 20px; margin-bottom: 16px;
}
.dup-file { cursor: pointer; }
.dup-file:hover .dup-path { color: var(--accent); }
.dup-path { font-size: 13px; font-weight: 600; margin-bottom: 6px; transition: color 0.15s; }
.dup-snippet { font-size: 12px; color: var(--text2); line-height: 1.5; max-height: 100px; overflow: hidden; }
.dup-sim { text-align: center; padding: 8px 0; }
.dup-sim .sim-value { font-size: 20px; font-weight: 700; color: var(--orange); }
.dup-sim .sim-label { font-size: 11px; color: var(--text2); }
.dup-merge-btn {
  margin-top: 10px; background: var(--green); color: #fff; border: none;
  padding: 6px 16px; border-radius: 16px; cursor: pointer; font-size: 12px; font-weight: 600;
}
.dup-merge-btn:hover { opacity: 0.9; }
.dup-merge-btn:disabled { opacity: 0.4; cursor: default; }

/* Discover */
.discover-container { display: flex; flex-direction: column; align-items: center; max-width: 700px; margin: 0 auto; }
.discover-tagline { font-size: 13px; color: var(--text2); margin-bottom: 20px; letter-spacing: 1px; text-transform: uppercase; }
.discover-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  width: 100%; overflow: hidden; animation: fadeIn 0.3s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.discover-meta {
  display: flex; gap: 16px; padding: 16px 24px; border-bottom: 1px solid var(--border);
  font-size: 12px; color: var(--text2); flex-wrap: wrap; align-items: center;
}
.discover-meta .dm-path { font-weight: 600; color: var(--text); font-size: 14px; }
.discover-content { padding: 24px; max-height: 50vh; overflow-y: auto; line-height: 1.7; }
.discover-content h1, .discover-content h2, .discover-content h3 { margin: 16px 0 8px; }
.discover-content h1 { font-size: 20px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
.discover-content code { background: var(--surface2); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.discover-content pre { background: var(--surface2); padding: 16px; border-radius: var(--radius); overflow-x: auto; margin: 12px 0; }
.discover-content pre code { background: none; padding: 0; }
.discover-content ul, .discover-content ol { padding-left: 24px; }
.discover-content a { color: var(--accent); }
.discover-actions {
  display: flex; gap: 12px; margin-top: 20px; justify-content: center;
}
.discover-btn {
  padding: 12px 32px; border-radius: 24px; border: none; cursor: pointer;
  font-size: 14px; font-weight: 600; transition: all 0.15s;
}
.discover-btn-next { background: var(--accent); color: #fff; }
.discover-btn-next:hover { opacity: 0.9; transform: scale(1.02); }
.discover-btn-view { background: var(--surface); border: 1px solid var(--border); color: var(--text); }
.discover-btn-view:hover { border-color: var(--accent); }

/* Tabs */
.tabs { display: flex; gap: 0; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
.tab {
  padding: 10px 20px; cursor: pointer; font-size: 13px; font-weight: 600;
  color: var(--text2); border-bottom: 2px solid transparent; transition: all 0.15s;
}
.tab:hover { color: var(--text); }
.tab.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-logo"><span>&#x1F9A8;</span> MemRoach</div>
  <nav>
    <a href="#/dashboard" data-view="dashboard"><span class="icon">&#9632;</span> Dashboard</a>
    <a href="#/browse" data-view="browse"><span class="icon">&#128196;</span> Browse</a>
    <a href="#/search" data-view="search"><span class="icon">&#128269;</span> Search</a>
    <a href="#/graph" data-view="graph"><span class="icon">&#128328;</span> Graph</a>
    <a href="#/timeline" data-view="timeline"><span class="icon">&#128337;</span> Timeline</a>
    <a href="#/team" data-view="team"><span class="icon">&#128101;</span> Team</a>
    <a href="#/compact" data-view="compact"><span class="icon">&#128230;</span> Compaction</a>
    <a href="#/sync" data-view="sync"><span class="icon">&#128260;</span> Sync</a>
    <a href="#/insights" data-view="insights"><span class="icon">&#128161;</span> Insights</a>
    <a href="#/discover" data-view="discover"><span class="icon">&#10024;</span> Discover</a>
  </nav>
</div>

<div class="main">
  <div class="topbar"><h1 id="page-title">Dashboard</h1></div>
  <div class="content" id="content"></div>
</div>

<div class="graph-tooltip" id="graph-tooltip"></div>

<script>
// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
const content = document.getElementById('content');
const pageTitle = document.getElementById('page-title');
const navLinks = document.querySelectorAll('.sidebar nav a');

const views = {
  dashboard: { title: 'Dashboard', render: renderDashboard },
  browse: { title: 'Browse Memories', render: renderBrowse },
  search: { title: 'Search', render: renderSearch },
  graph: { title: 'Knowledge Graph', render: renderGraph },
  timeline: { title: 'Timeline', render: renderTimeline },
  team: { title: 'Team', render: renderTeam },
  compact: { title: 'Compaction', render: renderCompact },
  sync: { title: 'Sync Status', render: renderSync },
  view: { title: 'Memory Viewer', render: renderViewer },
  insights: { title: 'Insights', render: renderInsights },
  discover: { title: 'Discover', render: renderDiscover },
};

function navigate() {
  const hash = location.hash || '#/dashboard';
  const parts = hash.slice(2).split('/');
  const viewName = parts[0];
  const viewPath = parts.slice(1).join('/');

  navLinks.forEach(a => a.classList.toggle('active', a.dataset.view === viewName));

  const view = views[viewName];
  if (view) {
    pageTitle.textContent = view.title;
    content.innerHTML = '<div class="loading">Loading...</div>';
    view.render(viewPath);
  }
}

window.addEventListener('hashchange', navigate);
navigate();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
async function api(path) {
  const r = await fetch(path);
  return r.json();
}

function typeBadge(t) { return `<span class="badge badge-${t}">${t}</span>`; }
function opBadge(op) { return `<span class="badge badge-${op}">${op}</span>`; }
function visBadge(v) { return `<span class="badge badge-${v}">${v}</span>`; }

function humanSize(bytes) {
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + 'KB';
  if (bytes < 1024*1024*1024) return (bytes/1024/1024).toFixed(1) + 'MB';
  return (bytes/1024/1024/1024).toFixed(1) + 'GB';
}

function timeAgo(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

function shortPath(p) {
  const parts = p.split('/');
  return parts.length > 3 ? '.../' + parts.slice(-2).join('/') : p;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
async function renderDashboard() {
  const data = await api('/api/stats');
  const bt = data.by_type;
  const memCount = (bt.memory||{}).count||0;
  const skillCount = (bt.skill||{}).count||0;
  const configCount = (bt.config||{}).count||0;

  content.innerHTML = `
    <div class="stats-grid">
      <div class="stat-card"><div class="value">${memCount}</div><div class="label">Memories</div></div>
      <div class="stat-card"><div class="value">${skillCount}</div><div class="label">Skills</div></div>
      <div class="stat-card"><div class="value">${configCount}</div><div class="label">Configs</div></div>
      <div class="stat-card"><div class="value">${data.total_files}</div><div class="label">Total Files</div></div>
      <div class="stat-card"><div class="value">${data.total_size_human}</div><div class="label">Storage</div></div>
      <div class="stat-card"><div class="value">${data.embedding_count}</div><div class="label">Embeddings</div></div>
      <div class="stat-card"><div class="value">${data.link_count}</div><div class="label">Graph Links</div></div>
      <div class="stat-card"><div class="value">${data.machines.length}</div><div class="label">Machines</div></div>
    </div>
    <div class="two-col">
      <div>
        <div class="section-title">Recent Activity</div>
        <div class="table-wrap"><table>
          <tr><th>File</th><th>Op</th><th>When</th><th>Machine</th></tr>
          ${data.recent_activity.map(a => `
            <tr class="clickable" onclick="location.hash='#/view/${a.path}'">
              <td title="${esc(a.path)}">${esc(shortPath(a.path))}</td>
              <td>${opBadge(a.operation)}</td>
              <td>${timeAgo(a.timestamp)}</td>
              <td style="font-size:12px;color:var(--text2)">${esc(a.machine_id||'').slice(-8)}</td>
            </tr>
          `).join('')}
        </table></div>
      </div>
      <div>
        <div class="section-title">Synced Machines</div>
        ${data.machines.map(m => `
          <div class="machine-card">
            <div class="machine-dot" style="background:var(--green)"></div>
            <div class="machine-info">
              <div class="name">${esc(m.machine_id)}</div>
              <div class="detail">${m.file_count} files &middot; last sync: ${timeAgo(m.last_sync)}</div>
            </div>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Browse
// ---------------------------------------------------------------------------
let browseState = { page: 1, type: '', visibility: '', machine: '', sort: 'synced_at', order: 'desc', q: '' };

async function renderBrowse() {
  const s = browseState;
  const params = new URLSearchParams({
    page: s.page, per_page: 50, sort: s.sort, order: s.order,
    ...(s.type && {type: s.type}),
    ...(s.visibility && {visibility: s.visibility}),
    ...(s.machine && {machine: s.machine}),
    ...(s.q && {q: s.q}),
  });
  const data = await api('/api/files?' + params);

  // Get machines for filter
  const stats = await api('/api/stats');
  const machines = stats.machines.map(m => m.machine_id);

  content.innerHTML = `
    <div class="filters">
      <select id="f-type" onchange="browseFilter()">
        <option value="">All Types</option>
        <option value="memory" ${s.type==='memory'?'selected':''}>Memory</option>
        <option value="skill" ${s.type==='skill'?'selected':''}>Skill</option>
        <option value="config" ${s.type==='config'?'selected':''}>Config</option>
        <option value="session" ${s.type==='session'?'selected':''}>Session</option>
        <option value="file" ${s.type==='file'?'selected':''}>File</option>
      </select>
      <select id="f-vis" onchange="browseFilter()">
        <option value="">All Visibility</option>
        <option value="private" ${s.visibility==='private'?'selected':''}>Private</option>
        <option value="team" ${s.visibility==='team'?'selected':''}>Team</option>
      </select>
      <select id="f-machine" onchange="browseFilter()">
        <option value="">All Machines</option>
        ${machines.map(m => `<option value="${esc(m)}" ${s.machine===m?'selected':''}>${esc(m)}</option>`).join('')}
      </select>
      <input type="text" id="f-q" placeholder="Filter by path..." value="${esc(s.q)}" onkeyup="if(event.key==='Enter')browseFilter()">
    </div>
    <div class="table-wrap"><table>
      <tr>
        <th onclick="browseSort('file_path')">Path</th>
        <th onclick="browseSort('file_type')">Type</th>
        <th onclick="browseSort('file_size')">Size</th>
        <th onclick="browseSort('visibility')">Vis</th>
        <th onclick="browseSort('synced_at')">Synced</th>
      </tr>
      ${data.files.length ? data.files.map(f => `
        <tr class="clickable" onclick="location.hash='#/view/${f.path}'">
          <td title="${esc(f.path)}">${esc(shortPath(f.path))}</td>
          <td>${typeBadge(f.type)}</td>
          <td>${humanSize(f.size)}</td>
          <td>${visBadge(f.visibility)}</td>
          <td>${timeAgo(f.synced_at)}</td>
        </tr>
      `).join('') : '<tr><td colspan="5" class="empty">No files found</td></tr>'}
    </table></div>
    <div class="pagination">
      <button onclick="browsePage(${s.page-1})" ${s.page<=1?'disabled':''}>Prev</button>
      <span>Page ${data.page} of ${data.total_pages} (${data.total} files)</span>
      <button onclick="browsePage(${s.page+1})" ${s.page>=data.total_pages?'disabled':''}>Next</button>
    </div>
  `;
}

window.browseFilter = function() {
  browseState.type = document.getElementById('f-type').value;
  browseState.visibility = document.getElementById('f-vis').value;
  browseState.machine = document.getElementById('f-machine').value;
  browseState.q = document.getElementById('f-q').value;
  browseState.page = 1;
  renderBrowse();
};
window.browseSort = function(col) {
  if (browseState.sort === col) browseState.order = browseState.order === 'asc' ? 'desc' : 'asc';
  else { browseState.sort = col; browseState.order = 'asc'; }
  renderBrowse();
};
window.browsePage = function(p) { browseState.page = p; renderBrowse(); };

// ---------------------------------------------------------------------------
// Viewer
// ---------------------------------------------------------------------------
async function renderViewer(path) {
  if (!path) { content.innerHTML = '<div class="empty">No file selected</div>'; return; }

  const [file, hist, graph] = await Promise.all([
    api('/api/files/' + path),
    api('/api/files/' + path + '/history'),
    api('/api/files/' + path + '/graph'),
  ]);

  if (file.error) { content.innerHTML = `<div class="empty">${file.error}</div>`; return; }

  pageTitle.textContent = shortPath(path);

  const rendered = marked.parse(file.content || '');

  content.innerHTML = `
    <div class="back-btn" onclick="history.back()">&#8592; Back</div>
    <div class="viewer-meta">
      <div class="meta-item"><strong>Path:</strong> ${esc(file.path)}</div>
      <div class="meta-item"><strong>Type:</strong> ${typeBadge(file.type)}</div>
      <div class="meta-item"><strong>Size:</strong> ${humanSize(file.size)}</div>
      <div class="meta-item"><strong>Version:</strong> ${file.version}</div>
      <div class="meta-item"><strong>Visibility:</strong> ${visBadge(file.visibility)}
        <button class="action-btn" style="margin-left:8px" onclick="toggleShare('${esc(file.path)}','${file.visibility}')">
          ${file.visibility==='private'?'Share with team':'Make private'}
        </button>
      </div>
      <div class="meta-item"><strong>Machine:</strong> ${esc(file.machine_id||'')}</div>
      <div class="meta-item"><strong>Synced:</strong> ${timeAgo(file.synced_at)}</div>
    </div>
    <div class="viewer-content">${rendered}</div>

    <div class="section-header" onclick="toggleSection('hist-body')">
      &#128337; Version History (${hist.version_count})
    </div>
    <div class="section-body" id="hist-body" style="display:none">
      ${hist.versions.length ? hist.versions.map(v => `
        <div class="timeline-entry">
          <div class="timeline-dot" style="background:${v.operation==='create'?'var(--green)':v.operation==='delete'?'var(--red)':'var(--accent)'}"></div>
          <div>
            <strong>v${v.version}</strong> ${opBadge(v.operation)}
            <span style="color:var(--text2);font-size:12px">${humanSize(v.size)} &middot; ${esc(v.machine_id||'').slice(-8)} &middot; ${timeAgo(v.timestamp)}</span>
          </div>
        </div>
      `).join('') : '<div class="empty">No history</div>'}
    </div>

    <div class="section-header" onclick="toggleSection('graph-body')">
      &#128328; Graph Links (${graph.total_links})
    </div>
    <div class="section-body" id="graph-body" style="display:none">
      ${graph.total_links ? `
        ${graph.outgoing.length ? '<div style="margin-bottom:8px;font-size:12px;color:var(--text2)">OUTGOING</div>' + graph.outgoing.map(l => `
          <div style="padding:4px 0">
            <span style="color:var(--text2)">&#8594; ${l.type}:</span>
            <a href="#/view/${l.path}" style="color:var(--accent);text-decoration:none">${esc(shortPath(l.path))}</a>
          </div>
        `).join('') : ''}
        ${graph.incoming.length ? '<div style="margin:12px 0 8px;font-size:12px;color:var(--text2)">INCOMING</div>' + graph.incoming.map(l => `
          <div style="padding:4px 0">
            <span style="color:var(--text2)">&#8592; ${l.type}:</span>
            <a href="#/view/${l.path}" style="color:var(--accent);text-decoration:none">${esc(shortPath(l.path))}</a>
          </div>
        `).join('') : ''}
      ` : '<div class="empty">No links</div>'}
    </div>
  `;
}

window.toggleSection = function(id) {
  const el = document.getElementById(id);
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
};

window.toggleShare = async function(path, current) {
  const newVis = current === 'private' ? 'team' : 'private';
  if (!confirm(`Change visibility to "${newVis}"?`)) return;
  await fetch('/api/files/' + path + '/share', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({visibility: newVis}),
  });
  renderViewer(path);
};

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------
async function renderSearch() {
  content.innerHTML = `
    <div class="search-box">
      <input type="text" id="search-input" placeholder="Search memories, skills, configs..." autofocus
        onkeyup="if(event.key==='Enter')doSearch()">
      <button onclick="doSearch()">Search</button>
    </div>
    <div id="search-results"></div>
  `;
}

window.doSearch = async function() {
  const q = document.getElementById('search-input').value.trim();
  if (!q) return;
  const res = document.getElementById('search-results');
  res.innerHTML = '<div class="loading">Searching...</div>';
  const data = await api('/api/search?q=' + encodeURIComponent(q) + '&limit=20');
  res.innerHTML = `
    <div style="font-size:12px;color:var(--text2);margin-bottom:12px">
      ${data.count} results (${data.search_type} search)
    </div>
    ${data.results.length ? data.results.map(r => `
      <div class="result-card" onclick="location.hash='#/view/${r.path}'">
        <div class="result-header">
          <span class="score">${r.score.toFixed(2)}</span>
          ${typeBadge(r.type)}
          <span class="path">${esc(shortPath(r.path))}</span>
          ${visBadge(r.visibility||'private')}
        </div>
        ${r.snippet ? `<div class="snippet">${esc(r.snippet).slice(0,200)}</div>` : ''}
      </div>
    `).join('') : '<div class="empty">No results found</div>'}
  `;
};

// ---------------------------------------------------------------------------
// Knowledge Graph
// ---------------------------------------------------------------------------
async function renderGraph() {
  const data = await api('/api/graph');

  if (!data.nodes.length) {
    content.innerHTML = '<div class="empty">No graph links yet. Use memroach_link to create connections.</div>';
    return;
  }

  const typeColors = {memory:'#58a6ff', skill:'#bc8cff', config:'#d29922', session:'#8b949e', file:'#8b949e'};
  const linkColors = {relates_to:'#58a6ff', duplicates:'#f85149', supersedes:'#f78166', caused_by:'#d29922', refines:'#3fb950'};

  content.innerHTML = `
    <div class="graph-legend">
      <span style="font-weight:600;margin-right:8px">Nodes:</span>
      ${Object.entries(typeColors).map(([t,c]) => `<div class="legend-item"><div class="legend-dot" style="background:${c}"></div>${t}</div>`).join('')}
      <span style="font-weight:600;margin-left:16px;margin-right:8px">Links:</span>
      ${Object.entries(linkColors).map(([t,c]) => `<div class="legend-item"><div class="legend-dot" style="background:${c}"></div>${t}</div>`).join('')}
    </div>
    <div id="graph-container"></div>
  `;

  const container = document.getElementById('graph-container');
  const w = container.clientWidth, h = container.clientHeight;
  const tooltip = document.getElementById('graph-tooltip');

  const svg = d3.select('#graph-container').append('svg').attr('viewBox', [0,0,w,h]);

  // Arrow markers
  Object.entries(linkColors).forEach(([type, color]) => {
    svg.append('defs').append('marker')
      .attr('id', 'arrow-' + type).attr('viewBox','0 -5 10 10')
      .attr('refX', 20).attr('refY', 0).attr('markerWidth', 6).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d','M0,-5L10,0L0,5').attr('fill', color);
  });

  const g = svg.append('g');
  svg.call(d3.zoom().on('zoom', e => g.attr('transform', e.transform)));

  const sim = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d=>d.id).distance(120))
    .force('charge', d3.forceManyBody().strength(-300))
    .force('center', d3.forceCenter(w/2, h/2))
    .force('collision', d3.forceCollide().radius(30));

  const link = g.append('g').selectAll('line').data(data.links).join('line')
    .attr('stroke', d => linkColors[d.link_type]||'#30363d')
    .attr('stroke-width', 1.5).attr('stroke-opacity', 0.6)
    .attr('marker-end', d => 'url(#arrow-' + d.link_type + ')');

  const node = g.append('g').selectAll('circle').data(data.nodes).join('circle')
    .attr('r', d => Math.max(6, Math.min(20, Math.log2(d.size||100)*2)))
    .attr('fill', d => typeColors[d.type]||'#8b949e')
    .attr('stroke', '#0d1117').attr('stroke-width', 1.5)
    .style('cursor', 'pointer')
    .call(d3.drag().on('start', dragStart).on('drag', dragging).on('end', dragEnd));

  node.on('mouseover', (e,d) => {
    tooltip.style.display = 'block';
    tooltip.textContent = d.id;
    tooltip.style.left = e.pageX + 12 + 'px';
    tooltip.style.top = e.pageY - 12 + 'px';
  }).on('mouseout', () => { tooltip.style.display = 'none'; })
  .on('click', (e,d) => { location.hash = '#/view/' + d.id; });

  const label = g.append('g').selectAll('text').data(data.nodes).join('text')
    .text(d => d.id.split('/').pop())
    .attr('font-size', 10).attr('fill', '#8b949e')
    .attr('dx', 14).attr('dy', 4);

  sim.on('tick', () => {
    link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
    node.attr('cx',d=>d.x).attr('cy',d=>d.y);
    label.attr('x',d=>d.x).attr('y',d=>d.y);
  });

  function dragStart(e,d) { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }
  function dragging(e,d) { d.fx=e.x; d.fy=e.y; }
  function dragEnd(e,d) { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }
}

// ---------------------------------------------------------------------------
// Timeline
// ---------------------------------------------------------------------------
async function renderTimeline() {
  const data = await api('/api/timeline?limit=200');

  // Group by date
  const groups = {};
  data.entries.forEach(e => {
    const d = new Date(e.timestamp).toLocaleDateString('en-US', {weekday:'long', month:'short', day:'numeric'});
    (groups[d] = groups[d]||[]).push(e);
  });

  const dotColor = op => op==='create'?'var(--green)':op==='delete'?'var(--red)':'var(--accent)';

  content.innerHTML = Object.entries(groups).map(([date, entries]) => `
    <div class="timeline-date">${date}</div>
    ${entries.map(e => `
      <div class="timeline-entry">
        <div class="timeline-dot" style="background:${dotColor(e.operation)}"></div>
        <div style="flex:1">
          ${opBadge(e.operation)}
          <a href="#/view/${e.path}" style="color:var(--accent);text-decoration:none;margin-left:8px">${esc(shortPath(e.path))}</a>
          <span style="color:var(--text2);font-size:12px;margin-left:8px">${humanSize(e.size)}</span>
        </div>
        <div style="font-size:12px;color:var(--text2)">${esc((e.machine_id||'').slice(-8))} &middot; ${timeAgo(e.timestamp)}</div>
      </div>
    `).join('')}
  `).join('') || '<div class="empty">No history entries</div>';
}

// ---------------------------------------------------------------------------
// Team
// ---------------------------------------------------------------------------
async function renderTeam() {
  const data = await api('/api/team/files');

  if (!data.files.length) {
    content.innerHTML = '<div class="empty">No team-shared memories yet. Use memroach_share to share.</div>';
    return;
  }

  // Group by owner
  const byOwner = {};
  data.files.forEach(f => { (byOwner[f.owner] = byOwner[f.owner]||[]).push(f); });

  content.innerHTML = `
    <div class="search-box" style="margin-bottom:20px">
      <input type="text" id="team-search-input" placeholder="Search team memories..."
        onkeyup="if(event.key==='Enter')doTeamSearch()">
      <button onclick="doTeamSearch()">Search</button>
    </div>
    <div id="team-results">
      ${Object.entries(byOwner).map(([owner, files]) => `
        <div class="section-title" style="margin-top:20px">Shared by ${esc(owner)} (${files.length})</div>
        <div class="table-wrap"><table>
          ${files.map(f => `
            <tr class="clickable" onclick="location.hash='#/view/${f.path}'">
              <td>${typeBadge(f.type)}</td>
              <td>${esc(shortPath(f.path))}</td>
              <td>${humanSize(f.size)}</td>
              <td>${timeAgo(f.synced_at)}</td>
            </tr>
          `).join('')}
        </table></div>
      `).join('')}
    </div>
  `;
}

window.doTeamSearch = async function() {
  const q = document.getElementById('team-search-input').value.trim();
  if (!q) return;
  const res = document.getElementById('team-results');
  res.innerHTML = '<div class="loading">Searching...</div>';
  const data = await api('/api/team/search?q=' + encodeURIComponent(q));
  res.innerHTML = data.results.length ? data.results.map(r => `
    <div class="result-card" onclick="location.hash='#/view/${r.path}'">
      <div class="result-header">
        <span class="score">${r.score.toFixed(2)}</span>
        ${typeBadge(r.type)}
        <span class="path">${esc(shortPath(r.path))}</span>
        <span style="color:var(--text2);font-size:12px">by ${esc(r.owner)}</span>
      </div>
      ${r.snippet ? `<div class="snippet">${esc(r.snippet).slice(0,200)}</div>` : ''}
    </div>
  `).join('') : '<div class="empty">No team results</div>';
};

// ---------------------------------------------------------------------------
// Compaction
// ---------------------------------------------------------------------------
async function renderCompact() {
  const [heatmap, candidates] = await Promise.all([
    api('/api/access/heatmap?days=30'),
    api('/api/compact/candidates?max_age_days=30&min_size=2000&limit=20'),
  ]);

  const maxCount = Math.max(1, ...heatmap.entries.map(e => e.access_count));

  content.innerHTML = `
    <div class="section-title">Access Heatmap (last 30 days)</div>
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:24px">
      ${heatmap.entries.length ? heatmap.entries.map(e => `
        <div class="heatmap-bar-wrap">
          <div class="heatmap-label" title="${esc(e.path)}">${esc(shortPath(e.path))}</div>
          <div style="display:flex;align-items:center;gap:8px">
            <div class="heatmap-bar" style="width:${Math.max(4, (e.access_count/maxCount)*100)}%"></div>
            <div class="heatmap-count">${e.access_count} reads</div>
          </div>
        </div>
      `).join('') : '<div class="empty">No access data yet</div>'}
    </div>

    <div class="section-title">Compaction Candidates</div>
    ${candidates.candidates.length ? `
      <div class="table-wrap"><table>
        <tr><th>File</th><th>Type</th><th>Size</th><th>Access Count</th><th>Last Accessed</th><th>Last Modified</th></tr>
        ${candidates.candidates.map(c => `
          <tr class="clickable" onclick="location.hash='#/view/${c.path}'">
            <td title="${esc(c.path)}">${esc(shortPath(c.path))}</td>
            <td>${typeBadge(c.type)}</td>
            <td>${humanSize(c.size)}</td>
            <td>${c.access_count}</td>
            <td>${c.last_accessed==='never'?'<span style="color:var(--orange)">never</span>':timeAgo(c.last_accessed)}</td>
            <td>${timeAgo(c.synced_at)}</td>
          </tr>
        `).join('')}
      </table></div>
    ` : '<div class="empty">No compaction candidates found</div>'}
  `;
}

// ---------------------------------------------------------------------------
// Sync Status
// ---------------------------------------------------------------------------
async function renderSync() {
  const data = await api('/api/sync/status');

  content.innerHTML = `
    <div class="section-title">Machines</div>
    ${data.machines.map(m => {
      const ago = timeAgo(m.last_sync);
      const isRecent = ago.includes('m ago') || ago.includes('s ago');
      return `
        <div class="machine-card">
          <div class="machine-dot" style="background:${isRecent?'var(--green)':'var(--orange)'}"></div>
          <div class="machine-info">
            <div class="name">${esc(m.machine_id)}</div>
            <div class="detail">${m.file_count} files &middot; last sync: ${ago}</div>
          </div>
        </div>
      `;
    }).join('')}

    <div class="section-title" style="margin-top:24px">Recent Sync Log</div>
    <div class="table-wrap"><table>
      <tr><th>Operation</th><th>Files</th><th>Bytes</th><th>Machine</th><th>When</th></tr>
      ${data.recent_logs.length ? data.recent_logs.map(l => `
        <tr>
          <td>${opBadge(l.operation)}</td>
          <td>${l.files_changed}</td>
          <td>${humanSize(l.bytes_transferred||0)}</td>
          <td style="font-size:12px;color:var(--text2)">${esc((l.machine_id||'').slice(-8))}</td>
          <td>${timeAgo(l.completed_at)}</td>
        </tr>
      `).join('') : '<tr><td colspan="5" class="empty">No sync logs yet</td></tr>'}
    </table></div>
  `;
}

// ---------------------------------------------------------------------------
// Insights Hub
// ---------------------------------------------------------------------------
async function renderInsights() {
  content.innerHTML = `
    <div class="tabs">
      <div class="tab active" onclick="switchInsightTab('health')">Health</div>
      <div class="tab" onclick="switchInsightTab('analytics')">Analytics</div>
      <div class="tab" onclick="switchInsightTab('topics')">Topics</div>
      <div class="tab" onclick="switchInsightTab('duplicates')">Duplicates</div>
    </div>
    <div id="insight-content"><div class="loading">Loading...</div></div>
  `;
  loadInsightTab('health');
}

window.switchInsightTab = function(tab) {
  document.querySelectorAll('.tabs .tab').forEach((t,i) => {
    t.classList.toggle('active', t.textContent.toLowerCase() === tab);
  });
  loadInsightTab(tab);
};

async function loadInsightTab(tab) {
  const el = document.getElementById('insight-content');
  el.innerHTML = '<div class="loading">Loading...</div>';
  if (tab === 'health') await renderHealth(el);
  else if (tab === 'analytics') await renderAnalytics(el);
  else if (tab === 'topics') await renderTopics(el);
  else if (tab === 'duplicates') await renderDuplicates(el);
}

async function renderHealth(el) {
  const d = await api('/api/insights/health');
  const staleColor = d.stale.count > 10 ? 'color-red' : d.stale.count > 3 ? 'color-amber' : 'color-green';
  const orphColor = d.orphaned.count > 15 ? 'color-red' : d.orphaned.count > 5 ? 'color-amber' : 'color-green';
  const covColor = d.embedding_coverage >= 80 ? 'color-green' : d.embedding_coverage >= 50 ? 'color-amber' : 'color-red';

  el.innerHTML = `
    <div class="health-grid">
      <div class="health-card">
        <div class="hc-header">
          <div class="hc-title">Stale Memories</div>
          <div class="hc-value ${staleColor}">${d.stale.count}</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">Not accessed in 30+ days</div>
        <div class="hc-list">
          ${d.stale.files.map(f => `
            <div class="hc-item">
              <a href="#/view/${f.path}" title="${esc(f.path)}">${esc(shortPath(f.path))}</a>
              <span style="color:var(--text2)">${humanSize(f.size)}</span>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="health-card">
        <div class="hc-header">
          <div class="hc-title">Orphaned Memories</div>
          <div class="hc-value ${orphColor}">${d.orphaned.count}</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">No graph links (isolated knowledge)</div>
        <div class="hc-list">
          ${d.orphaned.files.map(f => `
            <div class="hc-item">
              <a href="#/view/${f.path}" title="${esc(f.path)}">${esc(shortPath(f.path))}</a>
              <span style="color:var(--text2)">${humanSize(f.size)}</span>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="health-card">
        <div class="hc-header">
          <div class="hc-title">Embedding Coverage</div>
          <div class="hc-value ${covColor}">${d.embedding_coverage}%</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">${d.embedded_count} of ${d.total_memories} files embedded</div>
        <div style="background:var(--surface2);border-radius:4px;height:8px;margin-top:8px">
          <div style="background:var(--accent);border-radius:4px;height:100%;width:${Math.min(100,d.embedding_coverage)}%"></div>
        </div>
      </div>

      <div class="health-card">
        <div class="hc-header">
          <div class="hc-title">Most Churned</div>
          <div class="hc-value" style="color:var(--text)">${d.churn.files.length}</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">Files with the most version changes</div>
        <div class="hc-list">
          ${d.churn.files.map(f => `
            <div class="hc-item">
              <a href="#/view/${f.path}" title="${esc(f.path)}">${esc(shortPath(f.path))}</a>
              <span style="color:var(--text2)">${f.changes} changes</span>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="health-card">
        <div class="hc-header">
          <div class="hc-title">Largest Files</div>
        </div>
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">Top 10 by size</div>
        <div class="hc-list">
          ${d.oversized.files.map(f => `
            <div class="hc-item">
              <a href="#/view/${f.path}" title="${esc(f.path)}">${esc(shortPath(f.path))}</a>
              <span style="color:var(--text2)">${humanSize(f.size)}</span>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
}

async function renderAnalytics(el) {
  const d = await api('/api/insights/analytics?days=90');
  const maxTotal = Math.max(1, ...d.daily_activity.map(x => x.total));
  const maxChurn = d.most_churned.length ? d.most_churned[0].changes : 1;

  el.innerHTML = `
    <div class="stats-grid" style="margin-bottom:20px">
      <div class="stat-card"><div class="value">${d.total_operations}</div><div class="label">Total Operations (${d.days}d)</div></div>
      ${d.busiest_day ? `<div class="stat-card"><div class="value">${d.busiest_day.total}</div><div class="label">Busiest Day: ${d.busiest_day.date}</div></div>` : ''}
      <div class="stat-card"><div class="value">${d.machine_activity.length}</div><div class="label">Active Machines</div></div>
    </div>

    <div class="section-title">Daily Activity (last ${d.days} days)</div>
    <div class="chart-container">
      <div class="bar-chart">
        ${d.daily_activity.map(day => {
          const ch = 140; // max chart height
          const createH = Math.max(1, (day.create/maxTotal)*ch);
          const updateH = Math.max(day.update?1:0, (day.update/maxTotal)*ch);
          const deleteH = Math.max(day.delete?1:0, (day.delete/maxTotal)*ch);
          const label = day.date.slice(5); // MM-DD
          return `<div class="bar-group">
            ${day.delete?`<div class="bar" style="height:${deleteH}px;background:var(--red);width:100%" data-tooltip="${day.date}: ${day.delete} deletes"></div>`:''}
            ${day.update?`<div class="bar" style="height:${updateH}px;background:var(--accent);width:100%" data-tooltip="${day.date}: ${day.update} updates"></div>`:''}
            ${day.create?`<div class="bar" style="height:${createH}px;background:var(--green);width:100%" data-tooltip="${day.date}: ${day.create} creates"></div>`:''}
            <div class="bar-label">${label}</div>
          </div>`;
        }).join('')}
      </div>
      <div style="display:flex;gap:16px;margin-top:12px;font-size:11px;color:var(--text2)">
        <span><span style="color:var(--green)">&#9632;</span> Create</span>
        <span><span style="color:var(--accent)">&#9632;</span> Update</span>
        <span><span style="color:var(--red)">&#9632;</span> Delete</span>
      </div>
    </div>

    <div class="two-col">
      <div>
        <div class="section-title">Machine Activity</div>
        <div class="table-wrap"><table>
          <tr><th>Machine</th><th>Push</th><th>Pull</th><th>Total</th></tr>
          ${d.machine_activity.map(m => `
            <tr>
              <td style="font-size:12px">${esc(m.machine_id)}</td>
              <td>${m.push}</td><td>${m.pull}</td><td><strong>${m.total}</strong></td>
            </tr>
          `).join('')}
        </table></div>
      </div>
      <div>
        <div class="section-title">Most Churned Files</div>
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px">
          ${d.most_churned.map(f => `
            <div class="heatmap-bar-wrap">
              <div class="heatmap-label"><a href="#/view/${f.path}" style="color:var(--text);text-decoration:none">${esc(shortPath(f.path))}</a></div>
              <div style="display:flex;align-items:center;gap:8px">
                <div class="heatmap-bar" style="width:${Math.max(4,(f.changes/maxChurn)*100)}%;background:var(--purple)"></div>
                <div class="heatmap-count">${f.changes}</div>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    </div>
  `;
}

async function renderTopics(el) {
  const d = await api('/api/insights/topics?clusters=8');
  if (!d.clusters.length) {
    el.innerHTML = '<div class="empty">Not enough embedded memories for topic clustering.</div>';
    return;
  }
  el.innerHTML = `
    <div style="font-size:13px;color:var(--text2);margin-bottom:16px">${d.count} topic clusters from ${d.clusters.reduce((s,c)=>s+c.file_count,0)} embedded memories</div>
    <div class="topic-grid">
      ${d.clusters.map((c,i) => `
        <div class="topic-card" onclick="this.querySelector('.tc-all').style.display=this.querySelector('.tc-all').style.display==='none'?'block':'none'">
          <div class="tc-label">${esc(c.label)}</div>
          <div class="tc-count">${c.file_count} memories</div>
          <div class="tc-files">
            ${c.representatives.map(r => `<a href="#/view/${r}" onclick="event.stopPropagation()">${esc(shortPath(r))}</a>`).join('')}
            <div class="tc-all" style="display:none;margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
              ${c.files.filter(f => !c.representatives.includes(f)).map(f => `<a href="#/view/${f}" onclick="event.stopPropagation()">${esc(shortPath(f))}</a>`).join('')}
            </div>
          </div>
        </div>
      `).join('')}
    </div>
  `;
}

async function renderDuplicates(el) {
  const d = await api('/api/insights/duplicates?threshold=0.85&limit=20');
  if (!d.pairs.length) {
    el.innerHTML = '<div class="empty">No near-duplicates found above 85% similarity.</div>';
    return;
  }
  el.innerHTML = `
    <div style="font-size:13px;color:var(--text2);margin-bottom:16px">${d.count} potential duplicate pairs (threshold: ${(d.threshold*100).toFixed(0)}%)</div>
    ${d.pairs.map(p => `
      <div class="dup-pair">
        <div class="dup-file" onclick="location.hash='#/view/${p.file_a}'">
          <div class="dup-path">${esc(shortPath(p.file_a))}</div>
          <div class="dup-snippet">${esc(p.snippet_a)}</div>
        </div>
        <div class="dup-sim">
          <div class="sim-value">${(p.similarity*100).toFixed(0)}%</div>
          <div class="sim-label">similar</div>
          <button class="dup-merge-btn" onclick="event.stopPropagation();mergePair('${esc(p.file_a)}','${esc(p.file_b)}',this)">Merge</button>
        </div>
        <div class="dup-file" onclick="location.hash='#/view/${p.file_b}'">
          <div class="dup-path">${esc(shortPath(p.file_b))}</div>
          <div class="dup-snippet">${esc(p.snippet_b)}</div>
        </div>
      </div>
    `).join('')}
  `;
}

window.mergePair = async function(fileA, fileB, btn) {
  const keepFile = prompt(
    `Merge duplicates:\\n\\n` +
    `A: ${fileA}\\n` +
    `B: ${fileB}\\n\\n` +
    `Which file should be the primary (content of the other will be appended)?\\n` +
    `Type "A" or "B":`,
    'A'
  );
  if (!keepFile) return;
  const primary = keepFile.trim().toUpperCase() === 'B' ? fileB : fileA;
  const secondary = primary === fileA ? fileB : fileA;

  if (!confirm(`Merge into "${primary}"?\\n\\nContent from "${secondary}" will be appended, and it will be marked as superseded.`)) return;

  btn.disabled = true;
  btn.textContent = 'Merging...';

  const res = await fetch('/api/merge', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({file_a: primary, file_b: secondary}),
  });
  const data = await res.json();

  if (data.merged) {
    btn.textContent = 'Merged!';
    btn.style.background = 'var(--text2)';
    // Fade out the pair card
    btn.closest('.dup-pair').style.opacity = '0.4';
  } else {
    btn.textContent = 'Error';
    btn.disabled = false;
    alert(data.error || 'Merge failed');
  }
};

// ---------------------------------------------------------------------------
// Discover — Rediscover old memories
// ---------------------------------------------------------------------------
async function renderDiscover() {
  content.innerHTML = `
    <div class="discover-container">
      <div class="discover-tagline">Rediscover your memories</div>
      <div id="discover-card"><div class="loading">Loading...</div></div>
      <div class="discover-actions">
        <button class="discover-btn discover-btn-view" id="discover-view-btn" style="display:none" onclick="viewDiscoveredMemory()">Open in Viewer</button>
        <button class="discover-btn discover-btn-next" onclick="loadDiscoverCard()">Next Memory</button>
      </div>
    </div>
  `;
  loadDiscoverCard();
}

let currentDiscoverPath = '';

window.loadDiscoverCard = async function() {
  const el = document.getElementById('discover-card');
  el.innerHTML = '<div class="loading" style="padding:40px">Finding a memory for you...</div>';

  const d = await api('/api/insights/discover');
  if (!d.found) {
    el.innerHTML = '<div class="empty" style="padding:40px">No old memories to rediscover yet. Keep building your memory!</div>';
    document.getElementById('discover-view-btn').style.display = 'none';
    return;
  }

  currentDiscoverPath = d.path;
  const rendered = marked.parse(d.content || '');

  el.innerHTML = `
    <div class="discover-card">
      <div class="discover-meta">
        <span class="dm-path">${esc(shortPath(d.path))}</span>
        ${typeBadge(d.type)}
        <span>${d.days_old} days old</span>
        <span>${d.access_count} reads</span>
        <span>${humanSize(d.size)}</span>
      </div>
      <div class="discover-content">${rendered}</div>
    </div>
  `;
  document.getElementById('discover-view-btn').style.display = 'inline-block';
};

window.viewDiscoveredMemory = function() {
  if (currentDiscoverPath) location.hash = '#/view/' + currentDiscoverPath;
};
</script>
</body>
</html>"""


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

routes = [
    # API - Read
    Route("/api/stats", api_stats),
    Route("/api/files/{file_path:path}/history/{content_hash}", api_file_history_content),
    Route("/api/files/{file_path:path}/history", api_file_history),
    Route("/api/files/{file_path:path}/graph", api_file_graph),
    Route("/api/files/{file_path:path}/share", api_file_share, methods=["POST"]),
    Route("/api/files/{file_path:path}", api_file_detail),
    Route("/api/files", api_files),
    Route("/api/search", api_search),
    Route("/api/graph", api_graph),
    Route("/api/timeline", api_timeline),
    Route("/api/team/files", api_team_files),
    Route("/api/team/search", api_team_search),
    Route("/api/compact/candidates", api_compact_candidates),
    Route("/api/access/heatmap", api_access_heatmap),
    Route("/api/sync/status", api_sync_status),
    # API - Insights
    Route("/api/insights/health", api_insights_health),
    Route("/api/insights/analytics", api_insights_analytics),
    Route("/api/insights/duplicates", api_insights_duplicates),
    Route("/api/insights/topics", api_insights_topics),
    Route("/api/insights/discover", api_insights_discover),
    # API - Write
    Route("/api/merge", api_merge, methods=["POST"]),
    Route("/api/links", api_links_create, methods=["POST"]),
    Route("/api/links", api_links_delete, methods=["DELETE"]),
    # Frontend
    Route("/{path:path}", index),
    Route("/", index),
]

app = Starlette(routes=routes)


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="memroach-web", description="MemRoach Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    import uvicorn
    print(f"MemRoach Web UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
