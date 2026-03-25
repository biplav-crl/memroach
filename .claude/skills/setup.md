---
name: setup
description: Interactive setup wizard for MemRoach — walks through venv, config, schema, MCP registration, and global hooks
user_invocable: true
---

# MemRoach Setup Wizard

Walk the user through setting up MemRoach step by step. Check each prerequisite before proceeding.

## Steps

### 1. Check Python and venv
- Verify Python 3.11+ is available
- Check if `venv/` exists in the memroach directory. If not, create it: `python3 -m venv venv`
- Check if dependencies are installed: `./venv/bin/pip list | grep pg8000`. If not: `./venv/bin/pip install -r requirements.txt`

### 2. Configure CockroachDB connection
- Check if `memroach_config.json` exists. If not:
  - Tell the user to copy `memroach_config.json.example` to `memroach_config.json`
  - Ask them for their CockroachDB host, port, username, password, database name, and SSL cert path
  - Do NOT write credentials for them — tell them what to fill in and let them do it
- If it exists, read it to verify it has real values (not template placeholders)

### 3. Test connectivity
- Run `./venv/bin/python memroach_sync.py init` to verify the DB connection works
- If it fails, help troubleshoot (wrong host, SSL issues, auth errors)

### 4. Apply schema
- Ask the user if the schema has been applied to their CockroachDB instance
- If not, show them the command: `cockroach sql --url "postgresql://user@host:port/dbname?sslmode=verify-full" < schema/memroach_schema.sql`
- Or offer to apply it via the sync client's DB connection if they prefer

### 5. Initial push
- Run `./venv/bin/python memroach_sync.py push --dry-run` to show what would be pushed
- If the user approves, run `./venv/bin/python memroach_sync.py push -v`

### 6. Register MCP server
- Check if the user wants to register the MCP server for Claude Code, Cursor, or both
- For Claude Code: check `.mcp.json` in the user's project or global `~/.claude/mcp.json`
- Show them the registration JSON with the correct absolute path to the venv python and server script
- Do NOT write to their MCP config without permission

### 7. Global hooks
- Check if `~/.claude/settings.json` already has MemRoach hooks
- If not, show the user the hook config with absolute paths to their memroach install
- Ask before modifying `~/.claude/settings.json`
- Explain: hooks fire for ALL Claude Code sessions across all projects

### 8. Embedding API (optional)
- Ask if they want to enable semantic search (requires Voyage AI or Anthropic API key)
- If yes, tell them to set `embed_api_key` in `memroach_config.json`
- If no, keyword search still works without it

### 9. Verify
- Run `./venv/bin/python memroach_sync.py status` to confirm everything works
- Suggest restarting Claude Code to pick up MCP server and hooks

## Important
- Never write secrets or credentials into config files on the user's behalf
- Always show the user what you're about to do before modifying global settings files
- If any step fails, help troubleshoot before moving to the next step
