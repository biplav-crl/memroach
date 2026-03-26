# MemRoach — Instructions for Claude Code

## What is this repo?

MemRoach is a CockroachDB-backed memory sync system for AI agents. It has three main components:

1. **`memroach_mcp_server.py`** — MCP server (primary interface, 16 tools)
2. **`memroach_sync.py`** — File sync client + CLI + hook handler. Syncs `~/.claude/` to/from CockroachDB.
3. **`memroach_daemon.py`** — Background sync daemon for real-time cross-machine updates.

## Key patterns

- Single-file Python scripts using FastMCP and pg8000
- Direct CockroachDB connection (no Cloud Function intermediary)
- Content-addressable blob storage with gzip compression
- Hybrid search: vector embeddings (OpenAI text-embedding-3-small) + keyword matching
- Per-memory visibility: `private` (default) or `team`
- Optimistic concurrency via version column
- Knowledge graph with typed links (relates_to, duplicates, supersedes, caused_by, refines)
- Memory decay via access tracking

## Database

All tables prefixed with `memroach_`. Schema in `schema/memroach_schema.sql`.
- `memroach_blobs` — deduplicated content store
- `memroach_files` — file metadata per user+machine
- `memroach_embeddings` — vector embeddings for search
- `memroach_history` — version changelog
- `memroach_links` — knowledge graph edges
- `memroach_access` — read tracking for decay scoring
- `memroach_log` — audit trail

## Config

`memroach_config.json` (gitignored) — DB credentials, embedding API key, sync preferences.

## How Claude Should Use MemRoach

### Memory saving: files first, MCP for extras

**Default behavior — no change needed.** Continue saving memories to `~/.claude/memory/` files as normal. The sync hooks automatically push these to CockroachDB on Stop/SessionEnd. This is the primary path.

**Use MCP tools when you need something files can't do:**

- `memroach_store` — Write a memory directly to DB (useful from non-Claude-Code clients like Cursor)
- `memroach_share(path, "team")` — Make a memory visible to teammates
- `memroach_link(from, to, type)` — Create knowledge graph connections between related memories
- `memroach_merge(paths, content)` — Consolidate duplicate memories into one

### Memory retrieval: use MCP tools

When recalling or searching memories, prefer MCP tools over reading local files — they provide richer results:

- `memroach_search(query)` — Hybrid semantic + keyword search across all memories
- `memroach_get(path)` — Fetch a specific memory (also tracks access for decay scoring)
- `memroach_context(topic)` — Get a curated context bundle with full content for a topic
- `memroach_prime(project_hint)` — Load the most relevant memories for the current session
- `memroach_team(query)` — Search team-shared memories only

### Maintenance: call periodically

These tools keep the memory system healthy. Use them when appropriate:

- `memroach_consolidate()` — Find near-duplicate memories for merging
- `memroach_compact(max_age_days)` — Find old, rarely-accessed memories to summarize
- `memroach_graph(path)` — Explore knowledge graph connections
- `memroach_changes(since_minutes)` — Check for recent cross-machine updates

### Decision guide

| Scenario | Action |
|----------|--------|
| Saving a new memory | Write to `~/.claude/memory/` file (normal behavior) |
| Finding a relevant memory | `memroach_search(query)` |
| Starting a session | `memroach_prime(project_hint)` |
| Sharing with team | `memroach_share(path, "team")` |
| Two memories cover the same topic | `memroach_consolidate()` then `memroach_merge()` |
| Need context on a topic | `memroach_context(topic)` |
| Linking related memories | `memroach_link(from, to, "relates_to")` |
