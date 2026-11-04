#!/usr/bin/env python3
"""Verify that cited PMIDs, DOIs and ClinicalTrials.gov IDs exist and say what the repo says they say.

Read-only and stdlib-only. It queries NCBI E-utilities (PubMed), Crossref and the ClinicalTrials.gov
v2 API, prints what each identifier actually resolves to, and compares an expected title when you
have one. It writes nothing unless you pass --out or --cache.

A resolved identifier proves the record exists and how it is titled. It does NOT prove the paper
supports the sentence that cites it; that needs a person (or the full text) and stays a separate,
explicit status in the repos that track it.

Usage:
  refcheck.py PMID:19587680 10.1038/nprot.2013.022 NCT04472598
  refcheck.py 19587680 --expect-title "Rapamycin fed late in life extends lifespan ..."
  refcheck.py --scan README.md docs/ data/refs.csv            # find IDs in files, then verify them
  refcheck.py --records data/curated/evidence.csv --id-field document_ids --title-field source_title
  refcheck.py ... --json                                      # machine-readable

Environment: NCBI_API_KEY (optional, raises the NCBI rate limit; never printed), REFCHECK_MAILTO
(optional, added to the User-Agent for Crossref's polite pool). Nothing else is read or sent.

Exit status: 0 = everything resolved cleanly; 1 = something was not found, mismatched, retracted or
could not be reached (so a script or CI step can fail on it); 2 = usage error.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

PMID_RE = [
    re.compile(r"\bPMID[:\s#=]*([0-9]{5,9})\b", re.I),
    re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/([0-9]{5,9})"),
    re.compile(r"ncbi\.nlm\.nih\.gov/pubmed/([0-9]{5,9})"),
    re.compile(r"[\"']pmid[\"']\s*:\s*[\"']?([0-9]{5,9})\b", re.I),
]
DOI_RE = re.compile(r"\b(10\.[0-9]{4,9}/[^\s\"'<>,;\\]+)", re.I)
NCT_RE = re.compile(r"\b(NCT[0-9]{8})\b", re.I)

SKIP_DIRS = {".git", "node_modules", "dist", "__pycache__", ".venv", "build", ".pytest_cache"}
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".tsv", ".json", ".jsonl", ".py", ".yaml", ".yml", ".html", ".bib", ".tex", ".rst"}
UA = "refcheck/1.0 (read-only citation verifier for a personal research repo)"
RETRACTION_WORDS = ("retract", "withdraw", "expression of concern", "expression_of_concern")


@dataclass
class Ref:
    kind: str                      # PMID | DOI | NCT
    ident: str
    status: str = "PENDING"        # OK | NOT_FOUND | UNREACHABLE | ERROR
    title: str = ""
    authors: str = ""
    venue: str = ""
    date: str = ""
    doi: str = ""
    url: str = ""
    detail: dict = field(default_factory=dict)   # kind-specific fields (trial status, phase, ...)
    flags: list[str] = field(default_factory=list)
    expect_title: str = ""
    title_match: str = ""          # MATCH | PARTIAL | MISMATCH | "" (nothing to compare)
    title_score: float | None = None
    where: list[str] = field(default_factory=list)   # file:line occurrences when scanning
    retrieved: str = ""


# --------------------------------------------------------------------------- identifiers

def normalise(token: str) -> tuple[str, str] | None:
    t = token.strip().strip(".,;:)]}\"'")
    if not t:
        return None
    m = NCT_RE.fullmatch(t)
    if m:
        return "NCT", m.group(1).upper()
    t2 = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", t, flags=re.I)
    if t2.startswith("10.") and "/" in t2:
        return "DOI", t2.rstrip(".,;)")
    m = re.fullmatch(r"(?:PMID[:\s#=]*)?([0-9]{5,9})", t, re.I)
    if m:
        return "PMID", m.group(1)
    for rx in PMID_RE[1:3]:
        m = rx.search(t)
        if m:
            return "PMID", m.group(1)
    return None


def find_ids(text: str) -> list[tuple[str, str, int]]:
    """(kind, id, 1-based line) for every identifier in text."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rx in PMID_RE:
            for m in rx.finditer(line):
                found.append(("PMID", m.group(1), lineno))
        for m in DOI_RE.finditer(line):
            doi = m.group(1).rstrip(".,;)]}")
            found.append(("DOI", doi, lineno))
        for m in NCT_RE.finditer(line):
            found.append(("NCT", m.group(1).upper(), lineno))
    return found


# --------------------------------------------------------------------------- HTTP

class Fetcher:
    def __init__(self, cache_path: Path | None):
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        if cache_path and cache_path.is_file():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}
        self.key = os.environ.get("NCBI_API_KEY", "")
        mailto = os.environ.get("REFCHECK_MAILTO", "")
        self.ua = UA + (f" mailto:{mailto}" if mailto else "")
        self._last_ncbi = 0.0

    def get_json(self, url: str, ncbi: bool = False) -> tuple[str, dict | None]:
        """Return (state, json). state: ok | not_found | unreachable | error."""
        if ncbi:  # NCBI allows 3 requests/s without a key, 10/s with one
            wait = (0.12 if self.key else 0.38) - (time.monotonic() - self._last_ncbi)
            if wait > 0:
                time.sleep(wait)
            self._last_ncbi = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    return "ok", json.loads(resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return "not_found", None
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return "error", {"http": exc.code}
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return "unreachable", {"reason": str(getattr(exc, "reason", exc))[:120]}
            except ValueError:
                return "error", {"reason": "response was not JSON"}
        return "unreachable", None

    def save(self) -> None:
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, indent=1, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- lookups

def lookup_pmids(refs: list[Ref], fx: Fetcher) -> None:
    for i in range(0, len(refs), 100):
        batch = refs[i:i + 100]
        ids = [r.ident for r in batch]
        url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&retmode=json&id="
               + ",".join(ids) + (f"&api_key={urllib.parse.quote(fx.key)}" if fx.key else ""))
        state, data = fx.get_json(url, ncbi=True)
        for r in batch:
            r.url = f"https://pubmed.ncbi.nlm.nih.gov/{r.ident}/"
            if state != "ok" or not data:
                r.status = "UNREACHABLE" if state in ("unreachable", "error") else "NOT_FOUND"
                r.detail = data or {}
                continue
            rec = (data.get("result") or {}).get(r.ident)
            if not rec or rec.get("error"):
                r.status = "NOT_FOUND"
                continue
            authors = [a.get("name", "") for a in rec.get("authors", [])]
            r.status = "OK"
            r.title = (rec.get("title") or "").strip()
            r.authors = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
            r.venue = rec.get("fulljournalname") or rec.get("source") or ""
            r.date = rec.get("pubdate") or ""
            ids = {a.get("idtype"): a.get("value", "") for a in rec.get("articleids", [])}
            r.doi = ids.get("doi", "")
            r.detail = {"pubtype": rec.get("pubtype", []), "first_author": authors[0] if authors else "", "pmcid": ids.get("pmc", "")}
            for pt in rec.get("pubtype", []):
                if "retract" in pt.lower():
                    r.flags.append(f"PUBTYPE: {pt}")
            if any("retract" in (rec.get(k) or "").lower() for k in ("title",)):
                r.flags.append("title mentions retraction/withdrawal")


def lookup_doi(r: Ref, fx: Fetcher) -> None:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(r.ident, safe="/()")
    state, data = fx.get_json(url)
    r.url = f"https://doi.org/{r.ident}"
    if state == "not_found":
        r.status = "NOT_FOUND"
        return
    if state != "ok" or not data:
        r.status, r.detail = "UNREACHABLE", data or {}
        return
    msg = data.get("message", {})
    r.status = "OK"
    r.title = re.sub(r"<[^>]+>", "", (msg.get("title") or [""])[0]).strip()
    people = [f"{a.get('family', '')} {a.get('given', '')[:1]}".strip() for a in msg.get("author", [])]
    r.authors = ", ".join(people[:3]) + (" et al." if len(people) > 3 else "")
    r.venue = (msg.get("container-title") or [""])[0]
    parts = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0]
    r.date = "-".join(f"{p:04d}" if i == 0 else f"{p:02d}" for i, p in enumerate(q for q in parts if q))
    r.doi = r.ident
    r.detail = {"type": msg.get("type", ""), "publisher": msg.get("publisher", "")}
    for upd in msg.get("updated-by", []) or []:
        kind = str(upd.get("type", "")).lower()
        label = f"CROSSREF UPDATE: {kind or 'unspecified'}"
        r.flags.append(label.upper() if any(w in kind for w in RETRACTION_WORDS) else label)
    if msg.get("update-to"):
        r.flags.append("this record is itself a notice/update to another work")


def lookup_nct(r: Ref, fx: Fetcher) -> None:
    url = f"https://clinicaltrials.gov/api/v2/studies/{r.ident}"
    state, data = fx.get_json(url)
    r.url = f"https://clinicaltrials.gov/study/{r.ident}"
    if state == "not_found":
        r.status = "NOT_FOUND"
        return
    if state != "ok" or not data:
        r.status, r.detail = "UNREACHABLE", data or {}
        return
    p = data.get("protocolSection", {})
    ident, status, design = p.get("identificationModule", {}), p.get("statusModule", {}), p.get("designModule", {})
    r.status = "OK"
    r.title = ident.get("briefTitle") or ident.get("officialTitle") or ""
    r.authors = (p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}) or {}).get("name", "")
    r.venue = "ClinicalTrials.gov"
    r.date = (status.get("lastUpdatePostDateStruct") or {}).get("date", "")
    r.detail = {
        "overall_status": status.get("overallStatus", ""),
        "why_stopped": status.get("whyStopped", ""),
        "phases": design.get("phases", []),
        "enrollment": (design.get("enrollmentInfo") or {}),
        "start": (status.get("startDateStruct") or {}).get("date", ""),
        "primary_completion": (status.get("primaryCompletionDateStruct") or {}).get("date", ""),
        "last_update_posted": r.date,
        "has_results": data.get("hasResults", False),
        "conditions": (p.get("conditionsModule") or {}).get("conditions", [])[:6],
        "interventions": [i.get("name", "") for i in (p.get("armsInterventionsModule") or {}).get("interventions", [])][:6],
    }
    if status.get("overallStatus") in ("WITHDRAWN", "TERMINATED", "SUSPENDED"):
        r.flags.append(f"TRIAL STATUS: {status.get('overallStatus')}" + (f" ({status.get('whyStopped')})" if status.get("whyStopped") else ""))


# --------------------------------------------------------------------------- comparison

def _norm_title(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compare_title(r: Ref) -> None:
    """Score the recorded title against the registry title.

    Curated tables often hold one recorded title for several papers ("A; B") or a shortened one, so the
    recorded text is scored whole and per ';'-separated segment, and the best score counts.
    """
    if not r.expect_title or r.status != "OK":
        return
    actual = _norm_title(r.title)
    candidates = [r.expect_title] + [seg for seg in r.expect_title.split(";") if len(seg.strip()) >= 15]
    best, contained = 0.0, False
    for cand in candidates:
        a = _norm_title(cand)
        if not a or not actual:
            continue
        best = max(best, difflib.SequenceMatcher(None, a, actual).ratio())
        contained = contained or (len(a) >= 25 and (a in actual or actual in a))   # truncated / subtitle-less titles
    if not actual:
        return
    r.title_score = round(best, 3)
    r.title_match = "MATCH" if best >= 0.9 or contained else "PARTIAL" if best >= 0.6 else "MISMATCH"


# --------------------------------------------------------------------------- input modes

def iter_files(paths: list[str]):
    for p in paths:
        path = Path(p)
        if path.is_file():
            yield path
        elif path.is_dir():
            for f in sorted(path.rglob("*")):
                if f.is_file() and not (set(f.parts) & SKIP_DIRS) and f.suffix.lower() in TEXT_SUFFIXES:
                    yield f


def scan_paths(paths: list[str]) -> dict[tuple[str, str], list[str]]:
    seen: dict[tuple[str, str], list[str]] = {}
    for f in iter_files(paths):
        try:
            if f.stat().st_size > 5_000_000:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for kind, ident, line in find_ids(text):
            seen.setdefault((kind, ident), []).append(f"{f}:{line}")
    return seen


def _ids_in(value: object) -> list[tuple[str, str]]:
    """Identifiers inside one field value; accepts 'PMID:123', bare '123', a DOI, an NCT id or a URL."""
    text = str(value)
    found = [(k, i) for k, i, _ in find_ids(text)]
    if not found:
        n = normalise(text)
        found = [n] if n else []
    return list(dict.fromkeys(found))


def load_records(path: Path, id_field: str, title_field: str) -> list[tuple[str, str, str, str]]:
    """(kind, id, recorded title, locator) for every record that carries an identifier."""
    out: list[tuple[str, str, str, str]] = []
    if path.suffix.lower() in (".csv", ".tsv"):
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            if id_field not in (reader.fieldnames or []):
                raise ValueError(f"no column {id_field!r}; columns are {', '.join(reader.fieldnames or [])}")
            for n, row in enumerate(reader, 2):   # line 1 is the header
                for kind, ident in _ids_in(row.get(id_field, "")):
                    out.append((kind, ident, (row.get(title_field) or "").strip(), f"{path}:row{n}"))
        return out

    count = [0]

    def walk(node):
        if isinstance(node, dict):
            if id_field in node:
                count[0] += 1
                for kind, ident in _ids_in(node[id_field]):
                    out.append((kind, ident, str(node.get(title_field) or "").strip(), f"{path}:record{count[0]}"))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(json.loads(path.read_text(encoding="utf-8")))
    return out


# --------------------------------------------------------------------------- output

def render(r: Ref) -> str:
    lines = [f"{r.kind} {r.ident}  {r.status}" + (f"  [{r.title_match} {r.title_score}]" if r.title_match else "")]
    if r.status == "OK":
        lines.append(f"  title:   {r.title}")
        if not r.expect_title:
            lines.append("  topic:   NOT checked (no recorded title given; read the title against the claim)")
        if r.authors or r.venue or r.date:
            lines.append(f"  where:   {r.venue} {r.date} | {r.authors}".rstrip(" |"))
        if r.kind == "NCT":
            d = r.detail
            enr = d.get("enrollment") or {}
            lines.append(f"  trial:   {d.get('overall_status')} | phase {','.join(d.get('phases') or []) or 'n/a'} | "
                         f"enrollment {enr.get('count', 'n/a')} ({enr.get('type', '?')}) | start {d.get('start') or 'n/a'} | "
                         f"last update {d.get('last_update_posted') or 'n/a'} | results posted: {d.get('has_results')}")
            if d.get("conditions"):
                lines.append(f"  about:   {'; '.join(d['conditions'])}  |  interventions: {'; '.join(d.get('interventions') or [])}")
        elif r.kind == "PMID":
            if r.doi:
                lines.append(f"  doi:     {r.doi}")
            if r.detail.get("pmcid"):
                lines.append(f"  fulltext: https://pmc.ncbi.nlm.nih.gov/articles/{r.detail['pmcid']}/  (PMC copy exists; check the reuse terms before quoting)")
        if r.expect_title and r.title_match in ("PARTIAL", "MISMATCH"):
            lines.append(f"  recorded title: {r.expect_title}")
    elif r.status == "NOT_FOUND":
        lines.append("  no such record at the registry. Check the digits/prefix; do not delete the citation until you have.")
    elif r.status == "UNREACHABLE":
        lines.append(f"  could not reach the service ({r.detail}); this says nothing about the citation. Retry later.")
    for f in r.flags:
        lines.append(f"  FLAG:    {f}")
    if r.url:
        lines.append(f"  link:    {r.url}" + (f"  (registry data retrieved {r.retrieved})" if r.retrieved else ""))
    if r.where:
        shown = ", ".join(r.where[:4]) + (f" (+{len(r.where) - 4} more)" if len(r.where) > 4 else "")
        lines.append(f"  cited:   {shown}")
    return "\n".join(lines)


def needs_attention(r: Ref) -> bool:
    return r.status != "OK" or r.title_match in ("MISMATCH", "PARTIAL") or any(
        any(w in f.lower() for w in RETRACTION_WORDS) or f.startswith("TRIAL STATUS") for f in r.flags)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="PMID:123, bare digits, 10.xxxx/..., doi:..., NCT01234567, or a PubMed/doi.org URL")
    ap.add_argument("--expect-title", help="title the repo records for the (single) identifier given")
    ap.add_argument("--scan", nargs="+", metavar="PATH", help="find identifiers in these files/dirs, then verify them")
    ap.add_argument("--records", metavar="FILE", help="CSV/TSV/JSON of records; compare recorded titles to the registry")
    ap.add_argument("--id-field", default="document_ids", help="field holding the identifier(s) in --records")
    ap.add_argument("--title-field", default="source_title", help="field holding the recorded title in --records")
    ap.add_argument("--max-ids", type=int, default=300, help="refuse to verify more than this many unique IDs (default %(default)s)")
    ap.add_argument("--all", action="store_true", help="print clean results too (default: only those needing attention in bulk modes)")
    ap.add_argument("--json", action="store_true", help="JSON on stdout")
    ap.add_argument("--out", metavar="FILE", help="also write the JSON results here (you choose where; not inside a repo by default)")
    ap.add_argument("--cache", metavar="FILE", help="reuse/store fetched metadata in this JSON file (saves repeat calls)")
    ns = ap.parse_args()

    wanted: dict[tuple[str, str], Ref] = {}

    def want(kind: str, ident: str, where: str = "", expect: str = "") -> None:
        key = (kind, ident)
        ref = wanted.setdefault(key, Ref(kind, ident))
        if where and where not in ref.where:
            ref.where.append(where)
        if expect and not ref.expect_title:
            ref.expect_title = expect

    for tok in ns.ids:
        n = normalise(tok)
        if not n:
            print(f"cannot tell what kind of identifier this is: {tok!r}", file=sys.stderr)
            return 2
        want(n[0], n[1], expect=ns.expect_title or "")
    if ns.expect_title and len(ns.ids) != 1:
        print("--expect-title needs exactly one identifier (use --records for tables)", file=sys.stderr)
        return 2
    if ns.scan:
        for (kind, ident), where in scan_paths(ns.scan).items():
            for w in where:
                want(kind, ident, w)
    if ns.records:
        try:
            for kind, ident, title, loc in load_records(Path(ns.records), ns.id_field, ns.title_field):
                want(kind, ident, loc, title)
        except (OSError, ValueError, csv.Error) as exc:
            print(f"cannot read --records {ns.records}: {exc}", file=sys.stderr)
            return 2
    if not wanted:
        ap.print_usage(sys.stderr)
        print("nothing to verify", file=sys.stderr)
        return 2
    if len(wanted) > ns.max_ids:
        print(f"{len(wanted)} unique identifiers exceeds --max-ids {ns.max_ids}; narrow the scan or raise the limit "
              f"(each lookup is a network call to a public service)", file=sys.stderr)
        return 2

    fx = Fetcher(Path(ns.cache) if ns.cache else None)
    today = dt.date.today().isoformat()
    refs = list(wanted.values())
    pm = [r for r in refs if r.kind == "PMID"]
    pending_pm = [r for r in pm if f"PMID:{r.ident}" not in fx.cache]
    if pending_pm:
        lookup_pmids(pending_pm, fx)
        for r in pending_pm:
            r.retrieved = today          # the date the registry was actually asked, kept through the cache
            if r.status == "OK":
                fx.cache[f"PMID:{r.ident}"] = asdict(r)
    for r in refs:
        if r.kind == "PMID" and r.status == "PENDING" and f"PMID:{r.ident}" in fx.cache:
            cached = Ref(**{**fx.cache[f"PMID:{r.ident}"], "where": r.where, "expect_title": r.expect_title})
            r.__dict__.update(cached.__dict__)
        elif r.kind in ("DOI", "NCT"):
            ck = f"{r.kind}:{r.ident}"
            if ck in fx.cache:
                cached = Ref(**{**fx.cache[ck], "where": r.where, "expect_title": r.expect_title})
                r.__dict__.update(cached.__dict__)
            else:
                (lookup_doi if r.kind == "DOI" else lookup_nct)(r, fx)
                r.retrieved = today
                if r.status == "OK":
                    fx.cache[ck] = asdict(r)
        compare_title(r)
    fx.save()

    results = sorted(refs, key=lambda r: (not needs_attention(r), r.kind, r.ident))
    if ns.out:
        Path(ns.out).write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    if ns.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        bulk = bool(ns.scan or ns.records)
        shown = [r for r in results if ns.all or not bulk or needs_attention(r)]
        for r in shown:
            print(render(r), end="\n\n")
        bad = sum(needs_attention(r) for r in results)
        matched = sum(1 for r in results if r.title_match == "MATCH")
        unchecked = sum(1 for r in results if r.status == "OK" and not r.title_match and not needs_attention(r))
        print(f"checked {len(results)} unique identifier(s) on {today}: {matched} match a recorded title, "
              f"{unchecked} resolve with no recorded title to compare (topic fit NOT checked), {bad} need attention"
              + (f" ({len(results) - len(shown)} hidden; use --all)" if len(shown) < len(results) else ""))
        print("Reminder: a match shows the record exists and is titled as recorded, not that it supports the claim citing it.")
    return 1 if any(needs_attention(r) for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
