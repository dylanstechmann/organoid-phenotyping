#!/bin/sh
# gh_mcp.sh — launcher for the GitHub MCP server with self-resolving auth.
#
# Token lookup order (never printed, never committed):
#   1. GITHUB_PERSONAL_ACCESS_TOKEN already in the environment
#   2. `gh auth token` (the GitHub CLI's own credential store)
#
# Read-only + a narrow toolset list, mirroring the owner's existing Cline
# entry. Drop --read-only (or extend --toolsets) when write access is wanted.
set -e

BIN=$(command -v github-mcp-server || true)
[ -x "$BIN" ] || BIN="$HOME/.local/bin/github-mcp-server"
[ -x "$BIN" ] || { echo "gh_mcp: github-mcp-server not found" >&2; exit 1; }

if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
    GH_BIN=$(command -v gh || echo "$HOME/.local/bin/gh")
    TOKEN=$("$GH_BIN" auth token 2>/dev/null || true)
    [ -n "$TOKEN" ] || { echo "gh_mcp: no token (set GITHUB_PERSONAL_ACCESS_TOKEN or run: gh auth login)" >&2; exit 1; }
    export GITHUB_PERSONAL_ACCESS_TOKEN="$TOKEN"
fi

exec "$BIN" stdio --read-only --toolsets=repos,issues,pull_requests,users