# Agent tooling — portable across harnesses

This repository vendors its agent tooling (no secrets inside) so the same
capabilities work in **Claude Code, Cline (VS Code), Codex, OpenCode, Kilo Code,
Kimi Code, Grok, and anything else that reads AGENTS.md**.

## What is here

| Path | What |
|---|---|
| `.mcp.json` | Project-scoped MCP servers `scholar` + `web` (Claude Code, Cline) |
| `opencode.json` | The same servers for OpenCode |
| `.agent-tools/mcp/` | The server code: `scholar_mcp.py`, `web_mcp.py`, `agentkeys.py`, `launch.sh` |
| `.claude/skills/` | Project skills (`SKILL.md` + scripts) — see below |
| `CLAUDE.md` | Points Claude Code at `AGENTS.md` |

## MCP servers

- **`scholar`** — PubMed (`pubmed_search`, `pubmed_fetch`), OpenAlex
  (`openalex_search`), Semantic Scholar (`semantic_scholar_search`), CORE
  (`core_search`), ORCID (`orcid_record`), Crossref (`crossref_lookup`).
- **`web`** — `web_search` / `web_read` with automatic fallback across Exa,
  Brave, Jina, Firecrawl; plus the per-provider tools
  (`exa_search`, `exa_find_similar`, `brave_search`, `jina_search`, `jina_read`,
  `firecrawl_search`, `firecrawl_scrape`).

Both are read-only (search/fetch/scrape) stdio servers with no third-party
dependencies. They are launched through `sh .agent-tools/mcp/launch.sh`, which
picks a Python with a working stdlib (some minimal images ship a stripped
`python3`; set `AGENT_TOOLS_PYTHON` to override).

## Keys — where they live

Keys are read from, in order: process environment → `$AI_KEYS_FILE` → repo
`.env` (gitignored) → **`~/.config/ai-keys.env`** (mode 600, outside every repo).
They are never in committed files, MCP config, or tool arguments. In a shell,
`aikeys` loads the store. On the Windows laptop host, create
`C:\Users\<you>\.config\ai-keys.env` with the same `KEY=value` lines.

For **Windows-host MCP clients** (or any client that cannot run `sh`), replace
the server `command`/`args` with your Python:

```json
"command": "py", "args": ["-3", "C:/path/to/repo/.agent-tools/mcp/scholar_mcp.py"]
```

## Per-harness wiring

- **Claude Code** — nothing to do: reads `.mcp.json`, `.claude/skills/`,
  `CLAUDE.md` → `AGENTS.md` automatically.
- **Cline (VS Code)** — reads workspace `.mcp.json` automatically. If your
  version does not, merge the `mcpServers` block from `.mcp.json` into
  **MCP Servers: Configure**.
- **OpenCode** — reads `opencode.json` at the repo root automatically.
- **Codex** — reads `AGENTS.md` automatically. Add to `~/.codex/config.toml`
  (use absolute paths):

  ```toml
  [mcp_servers.scholar]
  command = "sh"
  args = ["ABS/PATH/TO/repo/.agent-tools/mcp/launch.sh", "scholar_mcp.py"]
  startup_timeout_sec = 30
  tool_timeout_sec = 120
  enabled = true

  [mcp_servers.web]
  command = "sh"
  args = ["ABS/PATH/TO/repo/.agent-tools/mcp/launch.sh", "web_mcp.py"]
  startup_timeout_sec = 30
  tool_timeout_sec = 120
  enabled = true
  ```

- **Kilo Code** — merges `.mcp.json`'s `mcpServers` into its MCP settings
  (workspace or `~/.kilo/mcp.json`).
- **Kimi Code / Grok** — follow `AGENTS.md`; where MCP is unavailable, the
  skills carry CLI fallbacks (`scripts/lit.py`, `scripts/web.py`) that hit the
  same APIs with the same keys.

## Skills in `.claude/skills/`

Each `SKILL.md` says when to use it; harnesses that do not auto-load skills
should read the matching SKILL.md when the task fits:

- `literature-search` — PubMed/OpenAlex/S2/CORE/ORCID/Crossref queries
- `web-research` — Exa/Brave/Jina/Firecrawl search and page reads
- `secrets-guard` — run before every commit; scans for leaked keys
- `citation-check` — verify PMIDs/DOIs/NCT IDs against registries
- `regen-guardrails` (research repos) — claims/wording guardrails
- `courselab-dev` (course repos) — course-authoring rules
- `fold-providers` (research repos) — structure-prediction provider routing

## Also available (harness-level, not vendored)

- `regen-workbench` MCP (literature + docking + provenance) — see
  `regen-workbench/MCP_SETUP.md` for the Docker-based server.
- GitHub MCP server binary at `~/.local/bin/github-mcp-server` and Blender MCP
  at `~/.local/bin/mcp-for-blender` — add them at the harness level if wanted.
