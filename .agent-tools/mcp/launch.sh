#!/bin/sh
# Interpreter-robust launcher for agent-toolbox MCP servers.
# Usage: sh launch.sh scholar_mcp.py   (or web_mcp.py)
# The system python3 may have a stripped stdlib in minimal images, so probe for
# a Python that can actually import json/urllib before exec'ing the server.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVER="$DIR/$(basename "${1:-scholar_mcp.py}")"

pick() {
  for py in "$AGENT_TOOLS_PYTHON" python3 "$HOME/.local/bin/python3.12" \
            /usr/local/bin/python3 python; do
    [ -n "$py" ] || continue
    if command -v "$py" >/dev/null 2>&1 \
       && "$py" -c 'import json, urllib.request' >/dev/null 2>&1; then
      echo "$py"
      return 0
    fi
  done
  return 1
}

PY=$(pick) || {
  echo "agent-toolbox: no Python with a working stdlib (json) found; set AGENT_TOOLS_PYTHON" >&2
  exit 1
}
exec "$PY" "$SERVER"
