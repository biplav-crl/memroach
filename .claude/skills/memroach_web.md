---
name: memroach_web
description: Launch the MemRoach web UI dashboard in a browser
user_invocable: true
---

# Launch MemRoach Web UI

Start the MemRoach web server and open it in the browser.

## Steps

1. Kill any existing process on port 8080: `lsof -ti:8080 | xargs kill -9 2>/dev/null || true`
2. Start the server in the background: `cd /Users/biplav/code/memroach && ./venv/bin/python memroach_web.py &`
3. Wait 2 seconds for the server to start
4. Open in browser: `open http://127.0.0.1:8080`
5. Tell the user the web UI is running at http://127.0.0.1:8080 and they can stop it with `lsof -ti:8080 | xargs kill -9`
