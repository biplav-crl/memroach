# MemRoach

**Unkillable memory for AI agents.** CockroachDB-backed memory sync with hybrid search, MCP server, and team sharing.

## What is MemRoach?

AI coding agents (Claude Code, Cursor, etc.) store memory, skills, settings, and session history as local files. Switch machines and everything is gone. MemRoach solves this:

- **MCP server** (primary) — any MCP-compatible client gets full memory access via `memroach_search`, `memroach_store`, `memroach_list`, etc.
- **File sync** (Claude Code convenience) — bidirectional sync of `~/.claude/` to CockroachDB
- **Hybrid search** — vector embeddings + keyword matching for semantic recall
- **Team sharing** — per-memory visibility controls (private/team)
- **Optimistic concurrency** — version tracking prevents silent overwrites
- **Skills as first-class citizens** — auto-classified, searchable, shareable

## Architecture

```
Claude Code / Cursor / Any MCP Client
        |
        | MCP
        v
memroach_mcp_server.py  (primary interface)
        |
        | pg8000 (TLS)
        v
CockroachDB (per-user accounts + RLS)
        ^
        | pg8000 (TLS)
memroach_sync.py  (file sync + CLI + hooks)
```

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp memroach_config.json.example memroach_config.json
# Edit memroach_config.json with your CockroachDB credentials
```

### 3. Initialize schema

```bash
# Apply schema to your CockroachDB instance
cockroach sql --url "postgresql://user@host:26257/memroach?sslmode=verify-full" < schema/memroach_schema.sql
```

### 4. First sync

```bash
python memroach_sync.py init     # Test connectivity
python memroach_sync.py push     # Upload ~/.claude/ to DB
```

### 5. Register MCP server

Add to your `.mcp.json` (Claude Code) or Cursor MCP config:

```json
{
  "mcpServers": {
    "memroach": {
      "command": "./venv/bin/python",
      "args": ["memroach_mcp_server.py"],
      "cwd": "/path/to/memroach"
    }
  }
}
```

## CLI Commands

```bash
memroach push                          # Upload changed files to DB
memroach pull                          # Download latest from DB to disk
memroach status                        # Show what's changed locally vs remote
memroach diff                          # Detailed file-level differences
memroach search "auth patterns"        # Hybrid semantic + keyword search
memroach share path/to/file --team     # Make a memory team-visible
memroach init                          # First-time setup
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `memroach_search` | Hybrid vector + keyword search across memories |
| `memroach_get` | Fetch a specific memory/skill/config by path |
| `memroach_store` | Store/update a memory directly |
| `memroach_list` | List entries, filterable by type and pattern |
| `memroach_share` | Change visibility (private/team) |
| `memroach_team` | Search team-shared entries only |

## File Type Classification

Files are auto-classified by path:

| Type | Path Pattern |
|------|-------------|
| `memory` | `*/memory/` directories, `CLAUDE.md` |
| `skill` | `*/skills/` directories |
| `config` | `settings.json`, `settings.local.json`, `mcp.json` |
| `session` | UUID directories with `.jsonl` files |
| `file` | Everything else |

## Auto-Sync via Claude Code Hooks

MemRoach can automatically push changes after every Claude Code session using global hooks. This works across **all projects** — not just the one where MemRoach is installed.

### Setup (global, all projects)

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/memroach/venv/bin/python /absolute/path/to/memroach/memroach_sync.py",
            "timeout": 10
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/memroach/venv/bin/python /absolute/path/to/memroach/memroach_sync.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Replace `/absolute/path/to/memroach/` with the actual install location.

### How it works

1. Claude Code fires the `Stop` hook after each response and `SessionEnd` when the session closes
2. `memroach_sync.py` detects it's running as a hook (JSON on stdin)
3. It immediately forks a background process and returns (never blocks the 10s timeout)
4. The background process runs `push --quiet`, logging to `/tmp/memroach_sync.log`

### Safety guarantees

The hook handler is designed to **never crash or block**, even if:
- `memroach_config.json` doesn't exist (logs "skipped", exits cleanly)
- DB is unreachable (background process fails silently, logged)
- Config is invalid JSON (exits cleanly)
- Any unexpected error occurs (caught at outermost level)

### Disable auto-sync

Set in `memroach_config.json`:

```json
{
  "auto_push_on_stop": false,
  "auto_push_on_session_end": false
}
```

Or remove the hooks from `~/.claude/settings.json`.

### Project-level hooks

If you only want auto-sync for a specific project, add the same hooks to `<project>/.claude/settings.json` instead. You can use `$CLAUDE_PROJECT_DIR` in the command if memroach is installed within that project.

## User Management

MemRoach supports CockroachDB's LDAP/OIDC integration for automatic user provisioning. For environments without an IdP, use `memroach_admin.py` for manual user management.

## License

MIT
