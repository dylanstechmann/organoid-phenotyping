# Agent instructions — organoid-phenotyping

This package measures supplied whole-organoid masks across image acquisitions.
It is not a segmentation model, viability assay, maturation classifier, or
regenerative-potency test.

## Do not

- Commit third-party image datasets, masks, or model weights without checking
  and recording their exact reuse terms.
- Randomly split frames from one organoid across development and final test.
- Treat a microscope holdout that shares specimens as biological generalization.
- Infer pixel scale, cell identity, maturation, viability, or function from mask
  geometry.
- Replace a missing or failed frame with an interpolated measurement.

## Done when

The acquisition manifest, frozen specimen split, and source/license provenance
are reviewable; any performance claim is reported at the appropriate specimen
level and remains separate from morphology-only outputs.


## Agent tooling (portable across harnesses)

<!-- agent-toolbox:installed -->

This repo vendors its agent tooling (no secrets inside) so Claude Code,
Cline, Codex, OpenCode, Kilo Code, Kimi Code and Grok share one setup:

- `.mcp.json` — project MCP servers `scholar` (PubMed, OpenAlex, Semantic
  Scholar, CORE, ORCID, Crossref) and `web` (Exa, Brave, Jina, Firecrawl).
- `opencode.json` — same servers for OpenCode; `AGENT_TOOLS.md` has the
  merge snippets for Codex / Cline / Kilo Code and notes for Kimi/Grok.
- `.claude/skills/` — project skills: citation-check, literature-search, web-research, secrets-guard, regen-guardrails, fold-providers.
- Keys: `~/.config/ai-keys.env` via the `agentkeys` loader — never in git,
  MCP config or chat. Run `secrets-guard` (`--staged`) before committing.
