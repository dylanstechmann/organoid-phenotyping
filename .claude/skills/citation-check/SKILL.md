---
name: citation-check
description: Verify that cited papers, DOIs, PubMed IDs and ClinicalTrials.gov (NCT) records really exist and match what a repo, report or lesson says they are, using a read-only script against PubMed, Crossref and ClinicalTrials.gov (works in this container, no keys needed). Catches unresolvable or mistyped PMIDs, ID/title mismatches, retracted papers and trial status, phase or enrollment that differs from a stored row. Use whenever you add, edit, review or audit a citation, reference list, source table, trial row, course-lesson reference or "source" field; before writing a "reviewed on" date; and whenever a number or claim is attributed to a paper. Never cite from memory; resolve the ID first.
---

# Verifying citations and trial records

AI-assisted writing produces plausible citations that do not exist or point at a different paper. The
owner's repos already treat this as a first-class risk (`claim_support_unreviewed` rows in the compound
atlas, `UNVERIFIED` trial rows in anagen, "which sentence in the paper" in the protocol compiler, checked
PubMed titles/DOIs in the course lessons). This skill makes the first check deterministic instead of
remembered.

## Run it
```bash
R=${CLAUDE_SKILL_DIR}/scripts/refcheck.py
python3 -B $R PMID:19587680 10.1038/nature08221 NCT04472598              # lookups; bare digits = PMID
python3 -B $R 19587680 --expect-title "title exactly as the repo records it"
python3 -B $R --scan docs/ README.md                                      # find every ID in files, verify each
python3 -B $R --records table.csv --id-field document_ids --title-field source_title   # CSV/TSV/JSON tables
```
Flags: `--json`, `--out FILE` (write results where you choose; default writes nothing), `--cache FILE`
(skip repeat calls and keep the original retrieval date), `--all` (show clean results in bulk modes),
`--max-ids N` (default 300). Exit status 1 means something needs attention, so it can gate a script.
Network failure is reported as UNREACHABLE, never as "not found".

## Reading the result
| Result | Meaning | Do |
|---|---|---|
| OK, `MATCH` | the ID resolves and the recorded title agrees | note the verification date; claim support is still a separate question |
| OK, no recorded title | the identifier resolves, but nothing says whether the paper is on topic | read the title against the claim; a resolved ID alone is not a topic check |
| OK, `PARTIAL` / `MISMATCH` | resolves to a different or paraphrased title (tables often hold a label or several `;`-joined titles) | read both titles; fix the ID or the recorded title; ask the user if it is ambiguous |
| `NOT_FOUND` | the registry has no such record | confirm with a second source (Europe PMC, or efetch); then mark the entry unresolved/UNVERIFIED and tell the user |
| `UNREACHABLE` | the service could not be reached | retry later; it says nothing about the citation |
| `FLAG` retraction / withdrawn / terminated | retracted paper, or trial withdrawn/terminated/suspended | do not use it as support; surface it |

## Rules
1. **A resolved ID is not support for a claim, and not a topic check either.** Most curated tables carry no recorded title, so a clean result there only means the identifier exists. It shows the record exists and is titled as recorded.
   Whether the paper backs the sentence needs someone to read it. Use the repo's own vocabulary
   (`claim_support_unreviewed`, `UNVERIFIED`, `source_status`) and never upgrade a status because the ID resolved.
2. **Never repair a bad ID by guessing digits or picking a similar one.** Find the paper by searching,
   verify the new ID with this script, and report the correction to the user: it changes the evidence.
   Do not delete an unresolved citation silently either; keep it visibly unresolved.
3. **Cite only what you opened.** If you cannot check a source in this session, say so and leave the row
   as it was (anagen: write `UNVERIFIED` rather than upgrading a phase or status).
4. **Trial facts come from the registry fetch, with a date.** Status, phase and enrollment change; record
   "reviewed on YYYY-MM-DD" with the URL you actually opened. Sponsor press releases do not override the registry.
5. **Numeric parameters need a sentence, not a title.** To justify a value (a concentration window, a
   time), open the open-access full text (the output prints a PMC link when one exists), quote the
   shortest sentence or table cell that contains it, and say where. A PMC copy existing is not licence to
   copy text; quote briefly and credit (the course repo is link-only for non-open material).
6. **Committed provenance uses the repo's own tooling.** If a repo defines a citation-audit or snapshot
   format (the atlas plans hashed PubMed snapshots), use that for anything that gets committed; use `--out`
   to your scratchpad for working notes.
7. **Searching is a different job.** For literature search with provenance inside `regen-workbench`, use
   `regen pubmed|europepmc|openalex` (its AGENT_BRIEFING says not to improvise curl loops). This script
   only verifies identifiers already cited.
8. **Be a polite API client.** Prefer `--cache` and one batched run over repeated single calls. If the user
   has an NCBI key it is read from `NCBI_API_KEY` in the environment; never write it into a file or log.

## Typical workflows
- **Adding a reference to a lesson or report:** find the paper, run `refcheck` on its PMID/DOI, copy the
  title as the registry gives it, record the date, and leave claim support unreviewed unless you quoted the sentence.
- **Auditing a table:** run `--records`, triage the NOT_FOUND and MISMATCH rows first, confirm each
  NOT_FOUND with a second source, and give the user a short list of decisions rather than editing curated data.
- **Refreshing trial rows (anagen):** `refcheck NCT...`, compare status/phase/enrollment with the stored row,
  change only what the registry shows, add the review date and URL, and keep unverifiable rows `UNVERIFIED`.
- **Reporting:** put the command, the date, the counts (resolved / not found / mismatched) and which
  second source confirmed any NOT_FOUND in your end-of-task report.
