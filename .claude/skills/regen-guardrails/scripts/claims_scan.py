#!/usr/bin/env python3
"""Advisory scan of a change for wording and files the owner's repos treat as red lines.

Four checks, all read-only:
  1. LANGUAGE  - lines you ADDED: advice-to-a-person/clinical wording, overclaims, career or sole-author
                 framing, secrets. Pre-existing text is not re-flagged; negated sentences ("does not
                 provide dosing") and bibliographic lines are hidden unless --all.
  2. TRIPWIRES - changed files whose AGENTS.md rules need an explicit justification or statement.
  3. REQUIRED  - limitation sentences a README must still contain after your edit ("not a physical
                 calibration", the AI-assistance disclosure, ...). Checked on the current files.
  4. DISCLOSURE- removed lines that carried the AI-assistance disclosure.

A hit is a prompt to re-read your wording, not a verdict: the rules are deliberately blunt. A secret's
value is never printed. Rules derive from the workspace and per-repo AGENTS.md files; if a rule here
disagrees with the repo's AGENTS.md, AGENTS.md wins (and fix this script).

usage:
  claims_scan.py                                 # repo of the current directory: working tree vs HEAD (+untracked)
  claims_scan.py --repo cell-protocol-compiler --staged
  claims_scan.py --repo diffmedia-loop --rev HEAD~3..HEAD
  claims_scan.py --files README.md REPORT.md     # scan whole files; no git required
  options: --all (show hidden/guarded hits), --strict (exit 1 on any visible hit)

Exit status: 1 if any HIGH hit or a REQUIRED statement is missing (or any hit with --strict), else 0.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))

DOC_EXT = {".md", ".rst", ".txt", ".html", ".htm"}
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".ino", ".sh"}
DATA_EXT = {".csv", ".tsv", ".json", ".yaml", ".yml", ".toml"}


def kind_of(path: str) -> str:
    ext = Path(path).suffix.lower()
    return "doc" if ext in DOC_EXT else "code" if ext in CODE_EXT else "data" if ext in DATA_EXT else "other"


# --------------------------------------------------------------------------- rules

@dataclass(frozen=True)
class Rule:
    rid: str
    sev: str                       # HIGH | MED | LOW
    kinds: tuple[str, ...]         # file kinds the rule applies to
    pattern: re.Pattern
    why: str
    guard: bool = True             # hide when the sentence is negated/limited
    skip_biblio: bool = True       # skip citation lines (their words come from paper titles)
    secret: bool = False           # never print the matched text


def R(rid, sev, kinds, pat, why, **kw):
    return Rule(rid, sev, tuple(kinds), re.compile(pat, re.I), why, **kw)


BIO = r"(?:biolog\w*|cell\w*|aging|ageing|senescen\w*|lifespan|efficacy|mechanism\w*|rejuvenat\w*|patient\w*|human\w*|organoid\w*)"
RULES: list[Rule] = [
    # ---- HIGH: secrets and person-directed content
    R("secret", "HIGH", ["doc", "code", "data", "other"],
      r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bAKIA[A-Z0-9]{16}\b|"
      r"\bsk-(?:proj-|ant-)[A-Za-z0-9_-]{25,}\b|postgres(?:ql)?(?:\+[a-z]+)?://[^:\s/]+:[^@\s]{3,}@|"
      r"\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b[\"']?\s*[:=]\s*[\"']?(?!your|example|xxx|changeme|<|\$|\{)[A-Za-z0-9_\-/+=]{24,}",
      "Looks like a credential (value withheld). Credentials live in host .env or the provider app, never in git, config, logs or chat.",
      guard=False, skip_biblio=False, secret=True),
    R("docker-socket", "HIGH", ["doc", "code", "data", "other"], r"docker\.sock",
      "Mounting the Docker socket into a workbench/dev container is forbidden (workspace and regen-workbench AGENTS.md)."),
    R("person-directed", "HIGH", ["doc", "code"],
      r"\b\d+(?:\.\d+)?\s*(?:mg|µg|μg|ug|mcg|g)\s*/\s*(?:kg|day|d)\b|"
      r"\b(?:you|users?|people|patients?|readers?)\s+(?:should|can|could|may|might|must)\s+(?:take|try|use|ingest|consume|inject|apply|combine)\b|"
      r"\btake (?:this|it|them)\b|\bregimens?\b|\bstack(?:ing)? (?:of )?(?:compounds?|supplements?|drugs?)\b|\bsupplement stack\b|"
      r"\bwhere to buy\b|\bbuy (?:it|them|this)\b|\bgray[- ]market (?:source|vendor)s?\b|\bself[- ]administ\w+|\bhome (?:cell )?therapy\b|"
      r"\bprotocols? for (?:humans?|people|patients?)\b",
      "Advice-to-a-person wording (take/try/regimen/stack/where to buy/mg per kg). These repos are research maps and software fixtures, not advice."),
    R("dose-word", "MED", ["doc", "code"],
      r"(?<!zero-)(?<!zero )\bdos(?:e|es|ed|ing|age)s?\b(?![- ](?:response|dependent|finding|effect))",
      "Check the sentence: fine when it describes a cited trial or a synthetic factor level; never when it turns a proposal into a dose for a person or animal."),
    R("clinical-release", "HIGH", ["doc", "code"],
      r"\bclinical[- ](?:release|grade)\b|(?-i:\bGMP\b)|\bgood manufacturing practice\b|(?-i:\bIND(?:-enabling)?\b)(?=[\s,.;)])|"
      r"\bput(?:ting)? cells? (?:in|into) (?:a )?(?:person|patient|human)\b|"
      r"\b(?:ready|suitable|safe|approved|cleared) (?:for|to) (?:a )?(?:person|people|human use|patients?|the clinic|clinical use|transplant\w*|implant\w*|infusion)\b|\bkaryotyp\w+",
      "Clinical-release, GMP/IND, karyotype or 'ready for a person' language is banned (cell-protocol-compiler, brightfield-colony-qc AGENTS.md)."),
    # ---- MED: overclaims
    R("proof-language", "MED", ["doc"],
      rf"\b(?:proves?|proven|proof)\b[^.\n]{{0,60}}{BIO}|{BIO}[^.\n]{{0,60}}\b(?:proves?|proven)\b|"
      rf"\b(?:establish(?:es|ed)?|confirm(?:s|ed)?|demonstrate[sd]?) that\b[^.\n]{{0,80}}{BIO}",
      "Software output does not prove biology. Say what was measured, on what data, and what it does not establish."),
    R("validated-claim", "MED", ["doc"],
      r"\b(?:experimentally|clinically|biologically) validated\b|\bvalidated (?:protocol|assay|biolog\w*|clinical\w*|finding|prediction|benchmark)s?\b|\bbiolog\w* validation\b",
      "'Validated' needs a scope and a measurement. A passing test or checklist is not biological validation."),
    R("novelty-hype", "MED", ["doc"],
      r"\b(?:novel|breakthrough|first[- ](?:ever|of its kind)|state[- ]of[- ]the[- ]art|cutting[- ]edge|revolutionary|groundbreaking|world[- ]class)\b",
      "Hobby/learning projects: cite prior art and describe what the code does instead of claiming novelty."),
    R("biology-leap", "MED", ["doc", "code"],
      r"\bbiological age\b|\brejuvenat\w+|\b(?:reverses?|reversal of) (?:aging|ageing)\b|\bsenolytic (?:result|candidate|hit)s?\b|"
      r"\bidentif(?:y|ies|ied) (?:senolytics?|drugs?|compounds?) that\b|\bfound biology\b|\breadiness (?:score|percent\w*)\b|\bsafety score\b|"
      r"\bpredicts? (?:efficacy|lifespan|safety)\b",
      "A score, probability or docking number is not rejuvenation, efficacy or safety (atlas, senescence, womb-models AGENTS.md)."),
    R("reproduced-claim", "MED", ["doc"],
      r"\breproduc(?:ed|es|ing)\b (?:the |their |its )?(?:paper|results? of|findings? of|study|figures? of)|\b[A-Z][a-z]+ et al\.?,? (?:was|were) reproduced\b",
      "Do not claim a paper was reproduced unless the data and method were actually rerun and compared (organoid-oxygen-lab AGENTS.md)."),
    # ---- LOW: framing (workspace AGENTS.md)
    R("career-framing", "LOW", ["doc"],
      r"\b(?:hiring managers?|recruiters?|employers?|resume bullets?|(?:your|my|his|her|the owner's) resume|résumé|curriculum vitae|job (?:application|interview)s?|looking for (?:a )?(?:job|work|role|position)|career (?:goal|path)s?)\b",
      "Workspace policy: no employment pitches, hiring-manager takeaways or resume bullets unless the owner asks for that framing."),
    R("sole-author", "LOW", ["doc"],
      r"\bsingle-?handedly\b|\bsolo (?:developer|author|project|built)\b|\bbuilt (?:entirely )?(?:by|from) (?:me|myself)\b|\bI (?:built|wrote|developed|designed|created|implemented)\b|\bwe (?:built|wrote|developed)\b",
      "Workspace policy: no claims of solo authorship; AI-assisted development stays visible and agent-run work is distinguished from the owner's own."),
    R("production-hype", "LOW", ["doc"],
      r"\b(?:production[- ](?:ready|grade|quality)|enterprise[- ]grade|industry[- ]leading|battle[- ]tested)\b",
      "These are personal hobby and learning projects; avoid maturity claims the evidence does not support."),
]

BIBLIO = re.compile(r"doi\.org|\bdoi:|\bPMID\b|pubmed|\b10\.\d{4,9}/|\bet al\b|\bPMC\d+|NCT\d{8}", re.I)
NEGATION = re.compile(
    r"\b(?:not|no|never|neither|nor|without|cannot|can't|isn't|aren't|doesn't|don't|won't|shouldn't|nothing|"
    r"none|lack(?:s|ed|ing)?|insufficient|fail(?:s|ed)? to|unassessed|undemonstrated|unsupported|"
    r"refus\w+|reject\w*|forbid\w*|prohibit\w*|avoid\w*|unverified|unvalidated|non-?clinical|out of scope|banned)\b", re.I)
DISCLOSURE = re.compile(r"AI coding tools|AI[- ]assisted|substantial assistance|assistance from AI|hobby and learning", re.I)

# --------------------------------------------------------------------------- per-repo knowledge (from AGENTS.md)

TRIPWIRES: dict[str, list[tuple[str, str]]] = {
    "cell-protocol-compiler": [
        ("src/protocolcompiler/library.py", "Encoded published windows/defaults. Never widen a window or change a default (GiWi CHIR stays 6 uM inside 2-12) without naming the sentence in the cited paper; update narrative steps AND the hash test together."),
    ],
    "geroscience-compound-atlas": [
        ("configs/bench.yaml", "Frozen benchmark thresholds/split: any change must be stated in REPORT.md."),
        ("configs/hypothesis.yaml", "Do not retune gates in the same commit as a code change; say which spam shape a gate kills."),
        ("data/curated/curated_evidence.csv", "Curated evidence: grades change only with human-reviewed primary evidence; verify every PMID with citation-check."),
    ],
    "regen-benchmark-kit": [
        ("examples/nist_ipsc/results/*", "Committed NIST results: overwrite only if the numbers changed, and update STUDY_REPORT.md to match."),
    ],
    "senescence-module-score": [
        ("src/senescore/genes.py", "Published gene lists: never edit a list to improve a synthetic effect size."),
        ("src/senescore/score.py", "Keep the 60% gene-coverage refusal and the train-only control semantics."),
    ],
    "diffmedia-loop": [
        ("src/medialoop/loop.py", "EI/UCB/Thompson/random policies must stay; the repeat-EI policy exists to show uniqueness violations."),
        ("examples/*", "Constraint bundles and candidate tables: windows must agree with cell-protocol-compiler; never edit the candidate CSV inside a reservation transaction."),
    ],
    "cell-fate-transport": [
        ("src/cellfate/*.py", "The unidentifiable-branch fixture (branch-B Brier worse than 0.25) and the balanced default must keep behaving as documented."),
    ],
    "open-perfusion-rig": [
        ("firmware/*", "Firmware flow ceiling (2000 uL/min) and command semantics. Never flash hardware from an agent; keep the README's 'not hardware-tested' line unless a real test is documented."),
        ("src/perfusion/pump.py", "Host validation must stay stricter than the sketch; integrated volume must match step count."),
    ],
    "organoid-oxygen-lab": [
        ("docs/METHODS.md", "Read before changing the solver; if a comment contradicts the mass balance, fix it and add a regression test."),
        ("docs/MODEL_CARD.md", "Parameters are illustrative; keep that label on every new figure."),
    ],
    "artificial-womb-models": [
        ("EVIDENCE.md", "Evidence ledger: keep achieved/reported/announced/prospective/unassessed separate; never upgrade an announcement or proposed trial to a measured result."),
    ],
    "anagen": [
        ("docs/data/*.json", "Trial rows: status/phase/enrollment need a primary source opened this session plus a review date; otherwise leave the row and write UNVERIFIED."),
        ("docs/atlas.js", "Trial rows live here too; stamp 'reviewed on YYYY-MM-DD' with the URL you opened and update the 'as of' sentence."),
    ],
    "regen-workbench": [
        ("docker-compose*.yml", "Never mount the Docker socket; credentials stay in host .env."),
        ("compose*.yaml", "Never mount the Docker socket; credentials stay in host .env."),
        ("docker/*", "Never mount the Docker socket; no credentials in images."),
        ("tools/regen_mcp.py", "Keep the MCP tool allowlist narrow; no arbitrary shell tool."),
    ],
}
UNIVERSAL_TRIPWIRES = [
    ("data/provenance/*", "Provenance files are written only when a real fetch happened."),
    (".github/workflows/*", "The gh token has no 'workflow' scope: a push touching this is rejected. Save a patch and tell the user."),
    ("DATA_LICENSES.md", "Licence/reuse terms: change only with the exact terms in hand."),
    ("LICENSE*", "Licence text: do not edit without the owner's instruction."),
]

# README sentences that must survive an edit (AGENTS.md 'Done when'). (path, regex, what it protects)
REQUIRED: dict[str, list[tuple[str, str, str]]] = {
    "brightfield-colony-qc": [("README.md", r"synthetic", "README says the shipped model is trained on synthetic data"),
                              ("README.md", r"not an iPSC result", "README says clean accuracy is a generator ceiling, not an iPSC result")],
    "open-perfusion-rig": [("README.md", r"has not been hardware-tested", "README says the sketch was not hardware-tested")],
    "perfusion-calibration-lab": [("README.md", r"not a physical calibration", "README says the fixture is not a physical calibration")],
    "diffmedia-loop": [("README.md", r"synthetic", "README says the response surface is synthetic")],
    "cell-fate-transport": [("README.md", r"unidentifiable", "README keeps the unidentifiable-branch demonstration")],
    "cell-protocol-compiler": [("README.md", r"protocol_sha256`? over the complete input\s+dataclass", "README says protocol_sha256 covers the full input dataclass")],
    "regen-benchmark-kit": [("README.md", r"three wells", "README keeps the three-well scope of the NIST benchmark")],
    "senescence-module-score": [("README.md", r"synthetic software results|synthetic d\b", "README says the huge demo effect size is a synthetic software result")],
    "organoid-oxygen-lab": [("README.md", r"illustrative", "README labels parameters/figures illustrative")],
    "anagen": [("README.md", r"Not medical or dental advice", "README keeps the not-medical-advice line")],
    "artificial-womb-models": [("README.md", r"outputs measure software behavior", "README says outputs measure software behaviour only")],
}
UNIVERSAL_REQUIRED = [
    ("README.md", r"personal hobby and learning", "README states these are personal hobby and learning projects"),
    ("README.md", r"AI coding tools|AI tools|AI-assisted|assistance from AI|assistance of AI", "README keeps the AI-assistance disclosure"),
]


# --------------------------------------------------------------------------- git plumbing

def git(repo: Path, *args: str, check: bool = False) -> str:
    cp = subprocess.run(["git", "-c", "safe.directory=*", "-c", "core.quotepath=false", "-C", str(repo), *args],
                        capture_output=True, text=True, errors="replace")
    if check and cp.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {cp.stderr.strip()[:200]}")
    return cp.stdout


@dataclass
class Hunk:
    added: list[tuple[int, str]] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    lines: list[tuple[int, str, bool]] = field(default_factory=list)   # new-file view: (lineno, text, was_added)


@dataclass
class FileChange:
    path: str
    hunks: list[Hunk] = field(default_factory=list)
    new_file: bool = False


def parse_diff(text: str) -> dict[str, FileChange]:
    files: dict[str, FileChange] = {}
    cur: FileChange | None = None
    hunk: Hunk | None = None
    lineno = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur, hunk = None, None
        elif raw.startswith("+++ "):
            name = raw[4:].strip()
            if name == "/dev/null":
                cur = None
                continue
            path = name[2:] if name.startswith("b/") else name
            cur = files.setdefault(path, FileChange(path))
        elif raw.startswith("new file mode") and cur is None:
            pass
        elif raw.startswith("@@") and cur is not None:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
            hunk = Hunk()
            cur.hunks.append(hunk)
        elif hunk is not None and cur is not None:
            if raw.startswith("+") and not raw.startswith("+++"):
                hunk.added.append((lineno, raw[1:].rstrip("\r")))
                hunk.lines.append((lineno, raw[1:].rstrip("\r"), True))
                lineno += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                hunk.removed.append(raw[1:].rstrip("\r"))
            elif raw.startswith(" "):
                hunk.context.append(raw[1:].rstrip("\r"))
                hunk.lines.append((lineno, raw[1:].rstrip("\r"), False))
                lineno += 1
    return files


def whole_file_change(path: str, text: str) -> FileChange:
    rows = [(i, ln.rstrip("\r")) for i, ln in enumerate(text.splitlines(), 1)]
    return FileChange(path, [Hunk(added=rows, lines=[(i, ln, True) for i, ln in rows])], new_file=True)


# --------------------------------------------------------------------------- checks

@dataclass
class Hit:
    sev: str
    rid: str
    where: str
    snippet: str
    why: str
    guarded: bool = False


def sentence_window(prev: list[str], line: str, start: int, end: int) -> str:
    """The sentence a match sits in, reaching back across wrapped lines, plus a few words after it."""
    before = " ".join(p.strip() for p in prev) + " " + line[:start]
    last = re.split(r"(?<=[.!?])\s+|\s[-*\u2022]\s+|\s\d+\.\s+", before)[-1]
    return last + line[start:end + 25]


# AGENTS.md / CLAUDE.md state the red lines themselves ("Do not add doses"); scanning them only echoes the rules.
RULE_DOCS = {"AGENTS.md", "CLAUDE.md"}
# Teaching content discusses these topics critically by design (it is reviewed under courselab-content instead).
EXEMPT_RULES = {"uniStemCourseSimulators": {"person-directed", "dose-word", "biology-leap", "proof-language", "validated-claim"}}


def scan_language(fc: FileChange, repo_name: str = "") -> list[Hit]:
    hits: list[Hit] = []
    if Path(fc.path).name in RULE_DOCS:
        return hits
    kind = kind_of(fc.path)
    exempt = EXEMPT_RULES.get(repo_name, set())
    for hunk in fc.hunks:
        for idx, (lineno, line, was_added) in enumerate(hunk.lines):
            if not was_added or not line.strip() or len(line) > 3000:
                continue
            prev = [t for _, t, _ in hunk.lines[max(0, idx - 2):idx]]
            for rule in RULES:
                if kind not in rule.kinds or rule.rid in exempt:
                    continue
                if rule.skip_biblio and BIBLIO.search(line):
                    continue
                m = rule.pattern.search(line)
                if not m:
                    continue
                guarded = rule.guard and bool(NEGATION.search(sentence_window(prev, line, m.start(), m.end())))
                snippet = "(value withheld)" if rule.secret else line.strip()[:150]
                hits.append(Hit(rule.sev, rule.rid, f"{fc.path}:{lineno}", snippet, rule.why, guarded))
    return hits


def scan_removed(fc: FileChange) -> list[Hit]:
    hits = []
    for hunk in fc.hunks:
        for line in hunk.removed:
            if DISCLOSURE.search(line):
                hits.append(Hit("HIGH", "disclosure-removed", fc.path, line.strip()[:150],
                                "Workspace policy: AI-assisted development stays visible in public overviews. Removing this needs the owner's say-so."))
    return hits


def scan_paths(paths: list[str], repo_name: str) -> list[Hit]:
    hits = []
    rules = TRIPWIRES.get(repo_name, []) + UNIVERSAL_TRIPWIRES
    for p in paths:
        base = Path(p).name
        if base == ".env" or (base.startswith(".env.") and base != ".env.example"):
            hits.append(Hit("HIGH", "env-file", p, "", "A .env file must never be committed; use .env.example for the template."))
        for pat, why in rules:
            if fnmatch.fnmatch(p, pat):
                hits.append(Hit("TRIPWIRE", "tripwire", p, "", why))
    return hits


def check_required(repo: Path) -> list[tuple[bool, str, str]]:
    out = []
    for fname, rx, what in UNIVERSAL_REQUIRED + REQUIRED.get(repo.name, []):
        f = repo / fname
        text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        if fname == "README.md":
            text = " ".join(text.splitlines()[:60]) if rx.startswith(("personal", "AI ")) else " ".join(text.split())
        out.append((bool(re.search(rx, text, re.I)), f"{fname}", what))
    return out


# --------------------------------------------------------------------------- main

def collect(repo: Path, ns) -> tuple[dict[str, FileChange], str]:
    if ns.rev:
        return parse_diff(git(repo, "diff", "--no-color", "--no-ext-diff", "-U2", ns.rev)), f"{ns.rev}"
    if ns.staged:
        return parse_diff(git(repo, "diff", "--cached", "--no-color", "--no-ext-diff", "-U2")), "staged changes"
    changes = parse_diff(git(repo, "diff", "HEAD", "--no-color", "--no-ext-diff", "-U2"))
    for rel in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        f = repo / rel
        try:
            if f.is_file() and f.stat().st_size < 1_000_000:
                changes[rel] = whole_file_change(rel, f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return changes, "working tree vs HEAD (+untracked)"


def resolve_repo(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if not p.exists() and (WORKSPACE / arg).exists():
            p = WORKSPACE / arg
        top = git(p, "rev-parse", "--show-toplevel").strip()
        if not top:
            raise SystemExit(f"{arg}: not a git repository here (worktree pointers to Windows paths do not work in this container)")
        return Path(top)
    top = git(Path.cwd(), "rev-parse", "--show-toplevel").strip()
    if not top:
        raise SystemExit("not inside a git repository; pass --repo NAME or use --files")
    return Path(top)


def report(title: str, hits: list[Hit], required: list[tuple[bool, str, str]], show_all: bool) -> tuple[int, int]:
    visible = [h for h in hits if show_all or not h.guarded]
    hidden = len(hits) - len(visible)
    order = {"HIGH": 0, "MED": 1, "LOW": 2, "TRIPWIRE": 3}
    print(f"claims_scan: {title}")
    for h in sorted(visible, key=lambda h: (order[h.sev], h.where)):
        flag = " (guarded: sentence looks negated/limited)" if h.guarded else ""
        print(f"\n{h.sev:<8} {h.rid:<20} {h.where}{flag}")
        if h.snippet:
            print(f"         > {h.snippet}")
        print(f"         why: {h.why}")
    missing = [(f, w) for ok, f, w in required if not ok]
    for f, w in missing:
        print(f"\nMISSING  required-statement   {f}\n         expected: {w}\n         why: AGENTS.md 'Done when' - limitation statements must survive edits. Restore it or tell the user why it moved.")
    counts = {s: sum(1 for h in visible if h.sev == s) for s in order}
    print(f"\nSummary: {counts['HIGH']} HIGH, {counts['MED']} MED, {counts['LOW']} LOW, {counts['TRIPWIRE']} tripwire file(s), "
          f"{len(missing)} missing required statement(s); {hidden} guarded/hidden (use --all).")
    if not visible and not missing:
        print("Nothing flagged. That is not evidence the wording is right; reread the claims against what was actually run.")
    return counts["HIGH"] + len(missing), len(visible) + len(missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", help="repo name under /workspace or a path (repeatable; default: repo of the cwd)")
    ap.add_argument("--staged", action="store_true", help="scan staged changes only")
    ap.add_argument("--rev", help="scan a commit range, e.g. HEAD~3..HEAD")
    ap.add_argument("--files", nargs="+", help="scan whole files for language only (no git)")
    ap.add_argument("--all", action="store_true", help="also show guarded (negated) hits")
    ap.add_argument("--strict", action="store_true", help="exit 1 on any visible hit")
    ns = ap.parse_args()

    if ns.files:
        hits: list[Hit] = []
        for f in ns.files:
            p = Path(f)
            hits += scan_language(whole_file_change(f, p.read_text(encoding="utf-8", errors="replace")))
        bad, anyhit = report(f"{len(ns.files)} file(s)", hits, [], ns.all)
        return 1 if bad or (ns.strict and anyhit) else 0

    status = 0
    for arg in (ns.repo or [None]):
        repo = resolve_repo(arg)
        changes, label = collect(repo, ns)
        hits = []
        for fc in changes.values():
            hits += scan_language(fc, repo.name) + scan_removed(fc)
        hits += scan_paths(sorted(changes), repo.name)
        bad, anyhit = report(f"{repo.name} - {label}, {len(changes)} file(s) changed", hits, check_required(repo), ns.all)
        status |= 1 if bad or (ns.strict and anyhit) else 0
        if arg is not None and len(ns.repo or []) > 1:
            print("\n" + "-" * 78 + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
