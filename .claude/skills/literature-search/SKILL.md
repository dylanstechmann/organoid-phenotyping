---
name: literature-search
description: Find, compare and record scholarly literature across PubMed, OpenAlex, Semantic Scholar, CORE, ORCID and Crossref using the repo's scholar MCP tools (or the bundled lit.py CLI when no MCP client is available). Use whenever a task needs papers, citations, DOI/PMID resolution, author identity, OA full text, or evidence for a scientific claim in a report, README, lesson, hypothesis or benchmark table. Never cite from memory: resolve every identifier here first, then verify with the citation-check skill.
---

# Literature search across the scholarly stack

Each repo vendors `scholar` MCP tools (`.agent-tools/mcp/scholar_mcp.py`) and this
CLI fallback (`scripts/lit.py`) so every harness — Claude Code, Cline, Codex,
OpenCode, Kilo Code, Kimi Code, Grok — can run the same queries. Keys load from
`~/.config/ai-keys.env` (or `$AI_KEYS_FILE`); never paste keys anywhere.

## Which source for what

| Source | Use it for | Tool |
|---|---|---|
| PubMed | Biomedical abstracts, the canonical PMID | `pubmed_search`, `pubmed_fetch` |
| OpenAlex | Broad coverage, citation counts, OA links, year filters | `openalex_search` |
| Semantic Scholar | Fast keyword search, citation counts, OA PDF links | `semantic_scholar_search` |
| CORE | Open-access **full text** (download URLs) | `core_search` |
| ORCID | Author identity / disambiguation | `orcid_record` |
| Crossref | DOI metadata sanity check (title, journal, year) | `crossref_lookup` |

Cross-check any important paper in **two** sources: a title that resolves in only
one index is weak evidence. For verifying a stored citation against its record,
run the `citation-check` skill — this skill finds papers, `citation-check` audits
them.

## CLI fallback (harnesses without MCP)

```bash
P=.claude/skills/literature-search/scripts/lit.py   # path inside the repo
python3 $P pubmed "organoid oxygen transport" --limit 5
python3 $P openalex "cell fate optimal transport" --year-from 2022
python3 $P s2 "senolytic likeness screen"
python3 $P core "perfusion calibration protocol"
python3 $P fetch-pubmed 19587680 25560112
python3 $P doi 10.1038/nature08221
python3 $P orcid 0000-0002-1825-0097
```

## Rules (these repos treat claims as first-class risk)

1. **Never fabricate or remember an identifier.** Search, resolve, then cite the
   returned PMID/DOI exactly. If it will live in a table, run `citation-check`.
2. **Record provenance**: for every paper kept in a repo table or report, store
   the identifier, title as returned, and the retrieval date. The workbench
   writes provenance under `data/provenance/` after real fetches — copy that
   habit into whatever repo you are in.
3. **A resolved ID is not claim support.** Open the abstract (or CORE full text)
   and quote the sentence that supports the claim, as the `citation-check` skill
   requires for numeric parameters.
4. **Polite pools**: queries carry `EMAIL` from the key store on NCBI/OpenAlex —
   do not remove it, and keep result pages small (<= 25) before widening.
5. **Prefer OA copies** (`oa_url`, `download_url`) when quoting full text; link
   the DOI regardless.
