---
name: regen-guardrails
description: Pre-commit wording and red-line check for the owner's regenerative-medicine, geroscience and organoid repos (cell-protocol-compiler, diffmedia-loop, geroscience-compound-atlas, anagen, senescence-module-score, artificial-womb-models, organoid-oxygen-lab, brightfield-colony-qc, and the others). Scans what changed for advice-to-a-person wording, clinical-release or GMP/IND language, overclaims ("proves", "validated", rejuvenation, novel), career or sole-author framing, credentials, and removal of the AI-assistance disclosure. Also flags edits to tripwire files named in each AGENTS.md, and checks that required limitation statements remain in the README. Use before committing, staging or opening a PR, and when editing README, REPORT, docs, trial rows, curated evidence, protocol windows, constraint bundles or results.
---

# Regen guardrails

The owner's repos carry explicit red lines in their AGENTS.md files: no dosing or regimens, no clinical
or GMP language, no claim that a score is biology, no claim that a paper was reproduced, no edits to
published windows or committed results to make a number look better. This skill runs a mechanical pass
for those lines plus the workspace policy on framing.

It is advisory. A hit is a prompt to reread a sentence, not a verdict, and a clean scan is not a science
review. Pair it with `citation-check` for references and `workspace-dev-env` for test runs.

## Run it
```bash
S=${CLAUDE_SKILL_DIR}/scripts/claims_scan.py
python3 -B $S --repo cell-protocol-compiler               # working tree against HEAD, plus untracked files
python3 -B $S --repo cell-protocol-compiler --staged      # exactly what you are about to commit
python3 -B $S --repo diffmedia-loop --rev HEAD~1..HEAD    # the last commit
python3 -B $S --files README.md REPORT.md                 # any text files, no git needed
python3 -B $S --repo anagen --all                         # also show hits the sentence guard hid
python3 -B ${CLAUDE_SKILL_DIR}/scripts/test_claims_scan.py   # run after editing the scanner itself
```
`--repo` takes a folder name under `/workspace` or a path. Several `--repo` flags are allowed.

## Reading the result
| Label | Meaning | Action |
|---|---|---|
| HIGH | credentials, advice to a person, clinical or GMP/IND language, Docker socket, `.env` file, removed AI disclosure | fix before committing |
| MED | overclaim: "proves", "validated", "rejuvenates", "novel", a dose word | rescope to what was measured |
| LOW | framing: career wording, sole-author claims, hype | reword |
| TRIPWIRE | the file is governed by an AGENTS.md rule (windows, gates, NIST results, EI policies, evidence ledger, trial rows) | confirm the rule is satisfied and say how in the report |
| MISSING | a required limitation sentence is gone from the README | restore it |

Sentences that negate or limit a claim ("does not establish rejuvenation", "no GMP claims") are hidden by
default, because those are the disclaimers the repos require. `--all` shows them, marked as guarded.
The exit status is 1 for HIGH or MISSING. `--strict` also fails on any visible hit.

## What to do about a hit
- Rescope the sentence to what the data show: say which data, which fixture, what was not tested. Do not
  swap a synonym to slip past the scanner.
- A quoted sentence from a paper or a rule may stay, if it is attributed.
- A tripwire file can change, but only in the way its AGENTS.md allows. Windows never widen without the
  paper sentence, gates never change in the same commit as code, NIST results are overwritten only when
  their numbers change and the report is updated to match.
- Never delete the AI-assistance disclosure or the hobby framing to get a clean scan. Removing it needs the
  owner's explicit instruction.
- Never weaken a required statement. If a README moved, put the sentence back.

## Standing rules behind the checks (AGENTS.md is authoritative)
- No doses, regimens, cycles, stacks, vendors, prices, clinics or "take this" for people or animals. A proposal or a
  simulated factor level is not a dose.
- No clinical-release, GMP, IND, karyotype or "ready for a person" text. Brightfield output is a software
  result on synthetic fields, not an iPSC or microbe result.
- A SenMayo score, a probability, a docking number, an epigenetic clock or a composite score is not
  biological age, rejuvenation, efficacy or safety. Synthetic fixtures are labelled as fixtures.
- Do not claim a paper was reproduced unless its data and method were rerun and compared.
- Do not claim a physical calibration, a hardware test or a print, unless a measurement file is committed
  with its uncertainty.
- Trial rows (anagen) keep UNVERIFIED unless a primary source was opened in this session; stamp the date and the
  URL. Curated evidence (geroscience-compound-atlas) grades change only with human-reviewed primary evidence.
- Public framing: personal hobby and learning projects, with AI-assisted development disclosed. No employment
  pitches, hiring-manager takeaways, resume bullets or solo-authorship claims unless the owner asks for them.

## Scope and limits
- The scanner reads added lines only, so pre-existing text is not re-flagged. Use `--files` to audit a whole
  document.
- Pattern matching misses paraphrases and can flag quoted text. Read the sentence before acting.
- The tripwire and required-statement lists live in `scripts/claims_scan.py`. Update them when an AGENTS.md
  rule changes, and rerun `test_claims_scan.py`.
- Teaching content in `uniStemCourseSimulators` discusses these topics critically by design, so person-directed,
  biology-claim and validation rules are exempt there. Its own review standard applies instead (see `courselab-dev`).
