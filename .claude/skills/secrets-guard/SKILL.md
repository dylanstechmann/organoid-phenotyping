---
name: secrets-guard
description: Scan files or git-tracked content for accidentally committed API keys and tokens (provider key formats, PEM blocks, generic high-entropy assignments) with the bundled scan_secrets.py script. Use before every git commit and git push, after pasting anything from a credentials page, and when reviewing agent-generated config files. Exit status 1 gates the commit. Never prints full secret values.
---

# Keeping keys out of repositories

Workspace rule: provider sign-ins live in `~/.config/ai-keys.env` (mode 600) or
their own apps — never in git, MCP config, images, or shared volumes. This skill
is the mechanical enforcement.

## Run it

```bash
S=.claude/skills/secrets-guard/scripts/scan_secrets.py   # path inside the repo
python3 $S --staged                 # scan what git is about to record
python3 $S --worktree               # scan all non-ignored working files
python3 $S path/to/file more/path   # scan specific paths
```

Exit 1 + a `SECRETS-GUARD:` line per hit means stop and fix. Values are printed
truncated (first 6 chars) so the report itself cannot leak the key.

## What it looks for

Provider key formats this workspace actually uses: NCBI, OpenAlex-style tokens,
Semantic Scholar (`s2k-`), CORE, ORCID client secrets, Exa UUID keys, Brave
(`BSA...`), Jina (`jina_`), Firecrawl (`fc-`), Modal (`ak-`/`as-`), OMTX,
JapanFold (`jf_live_`), NVIDIA (`nvapi-`), plus GitHub tokens (`ghp_`, `github_pat_`),
AWS (`AKIA`), private key blocks and generic `KEY=value` assignments whose value
has high token entropy.

## Rules

1. **A hit is blocking.** Rotate the key if it ever reached a commit or a chat,
   then remove it from the file and re-run until clean. Rotation beats deletion:
   git history remembers.
2. **Allowed exception**: `.env.example` placeholder names (`NCBI_API_KEY=` with
   empty value) and docs that name a variable without its value.
3. **Do not "fix" a hit by moving the secret to another file inside the repo.**
   Move it to `~/.config/ai-keys.env` and reference it by name.
4. Run `--staged` in CI or a pre-commit hook where the repo has one.
