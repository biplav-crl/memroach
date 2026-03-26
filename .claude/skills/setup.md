---
name: setup
description: Interactive setup wizard for MemRoach — walks through venv, config, schema, MCP registration, hooks, embeddings, and encryption
user_invocable: true
---

# MemRoach Setup Wizard

Walk the user through setting up MemRoach step by step. Check each prerequisite before proceeding. If any step fails, help troubleshoot before moving on.

## Step 1: Check Python and venv

- Verify Python 3.11+ is available
- Check if `venv/` exists in the memroach directory. If not, create it: `python3 -m venv venv`
- Check if dependencies are installed: `./venv/bin/pip list | grep pg8000`. If not: `./venv/bin/pip install -r requirements.txt`

## Step 2: New or existing database?

Ask the user: **"Are you setting up a new MemRoach database, or connecting to an existing one (e.g., setting up a second machine)?"**

### Path A: New database

#### Step 3a: Configure CockroachDB connection
- Check if `memroach_config.json` exists. If not, copy from `memroach_config.json.example`
- Ask the user for their CockroachDB host, port, username, password, database name, and SSL cert path
- Do NOT write credentials for them — tell them what fields to fill in and let them do it
- If it exists, read it to verify it has real values (not template placeholders like "your-cockroachdb-host")

#### Step 4a: Apply schema
- Run `./venv/bin/python memroach_sync.py init` to test connectivity
- If connection succeeds, apply the schema:
  - Show the command: `cockroach sql --url "postgresql://user@host:port/dbname?sslmode=verify-full" < schema/memroach_schema.sql`
  - Or offer to apply it via Python if `cockroach` CLI is not installed:
    ```python
    ./venv/bin/python -c "
    from memroach_sync import load_config, get_connection
    config = load_config()
    conn = get_connection(config)
    with open('schema/memroach_schema.sql') as f:
        for stmt in f.read().split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.run(stmt)
    print('Schema applied successfully')
    "
    ```

#### Step 5a: Initial push
- Run `./venv/bin/python memroach_sync.py push --dry-run` to show what would be uploaded
- If the user approves, run `./venv/bin/python memroach_sync.py push -v`

### Path B: Existing database

#### Step 3b: Get connection details
- Check if `memroach_config.json` exists. If not, copy from `memroach_config.json.example`
- Tell the user: "Get the database credentials from your admin or copy `memroach_config.json` from your other machine (update machine_id — it must be unique per machine)"
- Do NOT write credentials for them — tell them what fields to fill in
- If it exists, read it and verify values are real (not placeholders)
- IMPORTANT: Ensure `machine_id` is empty or different from other machines — it will be auto-generated on first run

#### Step 4b: Test connectivity
- Run `./venv/bin/python memroach_sync.py init` to test connectivity and generate a new machine_id
- Verify schema exists by checking: `./venv/bin/python memroach_sync.py status`
- If status works, schema is already applied — no need to run DDL

#### Step 5b: Initial pull
- Run `./venv/bin/python memroach_sync.py pull --dry-run` to show what would be downloaded
- If the user approves, run `./venv/bin/python memroach_sync.py pull -v`
- This downloads all memories from the existing database to this machine

## Step 6: Register MCP server

- Ask if the user wants to register for Claude Code, Cursor, or both
- For Claude Code: check `.mcp.json` in the user's project or global `~/.claude/mcp.json`
- Show the registration JSON with correct absolute paths:
  ```json
  {
    "mcpServers": {
      "memroach": {
        "command": "<absolute-path>/venv/bin/python",
        "args": ["memroach_mcp_server.py"],
        "cwd": "<absolute-path>"
      }
    }
  }
  ```
- Do NOT write to their MCP config without permission

## Step 7: Auto-sync hooks (optional)

- Ask if the user wants automatic push/pull via Claude Code hooks
- Check if `~/.claude/settings.json` already has MemRoach hooks
- If not, show the hook config with absolute paths to their memroach install:
  ```json
  {
    "hooks": {
      "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "<path>/venv/bin/python <path>/memroach_sync.py", "timeout": 15 }] }],
      "Stop": [{ "hooks": [{ "type": "command", "command": "<path>/venv/bin/python <path>/memroach_sync.py", "timeout": 10 }] }],
      "SessionEnd": [{ "hooks": [{ "type": "command", "command": "<path>/venv/bin/python <path>/memroach_sync.py", "timeout": 10 }] }]
    }
  }
  ```
- Explain: hooks fire for ALL Claude Code sessions across all projects
- Ask before modifying `~/.claude/settings.json`

## Step 8: Semantic search (optional)

- Ask if they want to enable hybrid semantic search (requires an OpenAI or Voyage AI API key)
- If yes, tell them to set these fields in `memroach_config.json`:
  - `embed_model`: `"text-embedding-3-small"` (OpenAI) or `"voyage-3"` (Voyage AI)
  - `embed_api_key`: their API key
- If no, explain that keyword search still works without it

## Step 9: Encryption (optional)

- Ask if they want to enable column-level encryption for stored content
- If yes:
  - For a **new database**: generate a key: `python3 -c "import os; print(os.urandom(32).hex())"`
  - For an **existing database**: they must use the same key as other machines — tell them to get it from their admin or existing config
  - Tell them to set in `memroach_config.json`:
    - `encryption_enabled`: `true`
    - `encryption_key`: the hex key
- If no, explain that data is still protected by CockroachDB's TLS in transit and any disk-level encryption

## Step 10: Verify

- Run `./venv/bin/python memroach_sync.py status` to confirm sync works
- If MCP server was registered, suggest restarting Claude Code and running `/mcp` to verify it's connected
- If hooks were set up, explain they'll activate on the next session
- Print a summary of what was configured:
  - Database: connected to [host]
  - Machine ID: [id]
  - MCP server: registered / not registered
  - Auto-sync hooks: enabled / disabled
  - Semantic search: enabled / disabled
  - Encryption: enabled / disabled

## Important

- Never write secrets or credentials into config files on the user's behalf — tell them what to fill in
- Always show the user what you're about to do before modifying global settings files
- If any step fails, help troubleshoot before moving to the next step
- For existing DB setups, emphasize that machine_id must be unique per machine
