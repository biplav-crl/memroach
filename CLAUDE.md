# MemRoach — Instructions for Claude Code

## What is this repo?

MemRoach is a CockroachDB-backed memory sync system for AI agents. It has two main components:

1. **`memroach_mcp_server.py`** — MCP server (primary interface). Provides `memroach_search`, `memroach_get`, `memroach_store`, `memroach_list`, `memroach_share`, `memroach_team` tools.
2. **`memroach_sync.py`** — File sync client + CLI + hook handler. Syncs `~/.claude/` to/from CockroachDB.

## Key patterns

- Single-file Python scripts using FastMCP and pg8000
- Direct CockroachDB connection (no Cloud Function intermediary)
- Content-addressable blob storage with gzip compression
- Hybrid search: vector embeddings (Voyage API) + keyword matching
- Per-memory visibility: `private` (default) or `team`
- Optimistic concurrency via version column

## Database

All tables prefixed with `memroach_`. Schema in `schema/memroach_schema.sql`.
- `memroach_blobs` — deduplicated content store
- `memroach_files` — file metadata per user+machine
- `memroach_embeddings` — vector embeddings for search
- `memroach_log` — audit trail

## Config

`memroach_config.json` (gitignored) — DB credentials, embedding API key, sync preferences.
