# Agent tooling — portable across harnesses

This repository vendors its agent tooling (no secrets inside) so the same
capabilities work in **Claude Code, Cline (VS Code), Codex, OpenCode, Kilo Code,
Kimi Code, Grok, and anything else that reads AGENTS.md**.

## What is here

| Path | What |
|---|---|
| `.mcp.json` | Project-scoped MCP servers (Claude Code, Cline) — see below |
| `opencode.json` | The same servers for OpenCode |
| `.agent-tools/mcp/` | Server code: `scholar_mcp.py`, `web_mcp.py`, `gh_mcp.sh`, `agentkeys.py`, `launch.sh` |
| `.claude/skills/` | Project skills (`SKILL.md` + scripts) — see below |
| `CLAUDE.md` | Points Claude Code at `AGENTS.md` |

## MCP servers

- **`scholar`** — PubMed (`pubmed_search`, `pubmed_fetch`), OpenAlex
  (`openalex_search`), Semantic Scholar (`semantic_scholar_search`), CORE
  (`core_search`), ORCID (`orcid_record`), Crossref (`crossref_lookup`).
  Read-only; keys from `~/.config/ai-keys.env`.
- **`web`** — `web_search` / `web_read` with automatic fallback across Exa,
  Brave, Jina, Firecrawl; plus the per-provider tools. Read-only.
- **`github`** — official GitHub MCP server, **read-only** with a narrow
  toolset (`repos,issues,pull_requests,users`). Launched through
  `.agent-tools/mcp/gh_mcp.sh`, which resolves the token from
  `GITHUB_PERSONAL_ACCESS_TOKEN` or `gh auth token` — no token is ever
  committed. To allow writes (creating issues/PRs), remove `--read-only` in
  `gh_mcp.sh`.
- **`context7`** — up-to-date library documentation lookup
  (`npx -y @upstash/context7-mcp`; needs Node ≥ 18 on PATH).
- **`playwright`** — browser automation for web testing/scraping
  (`npx -y @playwright/mcp@latest`; needs Node).
- **`blender`** (Blender-using repos only) — MCP for Blender; connects to the
  running Blender addon (`BLENDER_HOST`/`BLENDER_PORT` env, addon must be
  enabled in Blender).

`scholar` and `web` are dependency-free stdio servers launched through
`sh .agent-tools/mcp/launch.sh`, which picks a Python with a working stdlib
(some minimal images ship a stripped `python3`; override with
`AGENT_TOOLS_PYTHON`).

## Keys — where they live

Keys are read from, in order: process environment → `$AI_KEYS_FILE` → repo
`.env` (gitignored) → **`~/.config/ai-keys.env`** (mode 600, outside every
repo). They are never in committed files, MCP config, or tool arguments. In a
shell, `aikeys` loads the store. On the Windows laptop host, mirror the store at
`C:\Users\<you>\.config\ai-keys.env` with the same `KEY=value` lines, and run
`gh auth login` so `gh_mcp.sh` can resolve the GitHub token.

For **Windows-host MCP clients** (or any client that cannot run `sh`), replace
the server `command`/`args` with your Python:

```json
"command": "py", "args": ["-3", "C:/path/to/repo/.agent-tools/mcp/scholar_mcp.py"]
```

and for `github`, point the command at the GitHub MCP server binary (or its
Docker image) directly with `GITHUB_PERSONAL_ACCESS_TOKEN` in its `env`.

## Per-harness wiring

- **Claude Code** — nothing to do: reads `.mcp.json`, `.claude/skills/`,
  `CLAUDE.md` → `AGENTS.md` automatically.
- **Cline (VS Code)** — reads workspace `.mcp.json` automatically. If your
  version does not, merge the `mcpServers` block from `.mcp.json` via
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

  (The `github`, `context7`, `playwright` and `blender` servers are best added
  once at the harness level — see the workspace `agent-toolbox/README.md`.)

- **Kilo Code** — merges `.mcp.json`'s `mcpServers` into its MCP settings
  (workspace or `~/.kilo/mcp.json`).
- **Kimi Code / Grok** — follow `AGENTS.md`; where MCP is unavailable, the
  skills carry CLI fallbacks (`scripts/lit.py`, `scripts/web.py`) that hit the
  same APIs with the same keys. For GitHub work, `gh` CLI covers everything
  the `github` MCP server does.

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
- GitHub MCP server binary at `~/.local/bin/github-mcp-server`, Blender MCP at
  `~/.local/bin/mcp-for-blender`, and the `machine-cad` MCP — wire these at the
  harness level (Cline: `~/.cline/data/settings/cline_mcp_settings.json`;
  Codex: `~/.codex/config.toml`).
