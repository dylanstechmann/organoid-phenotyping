#!/usr/bin/env python3
"""scan_secrets.py — find accidentally staged/committed API keys. Exit 1 on hits.

Usage: scan_secrets.py --staged | --worktree | PATH [PATH ...]
Prints truncated values only (first 6 chars).
"""
import argparse
import math
import os
import re
import subprocess
import sys

PATTERNS = [
    ("NCBI key", re.compile(r"(?i)ncbi[^\n]{0,24}[0-9a-f]{32,36}")),
    ("S2 key", re.compile(r"\bs2k-[A-Za-z0-9]{20,}")),
    ("Jina key", re.compile(r"\bjina_[A-Za-z0-9]{20,}")),
    ("Firecrawl key", re.compile(r"\bfc-[0-9a-f]{16,}")),
    ("NVIDIA key", re.compile(r"\bnvapi-[A-Za-z0-9\-_]{20,}")),
    ("OMTX key", re.compile(r"\bomtx_[A-Za-z0-9\-_]{16,}")),
    ("JapanFold key", re.compile(r"\bjf_(live|test)_[A-Za-z0-9]{16,}")),
    ("Modal token", re.compile(r"\b(a[ks])-[A-Za-z0-9]{20,}")),
    ("Brave key", re.compile(r"\bBSA[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\b(sk-[A-Za-z0-9_\-]{24,}|sk-ant-[A-Za-z0-9_\-]{24,})")),
    ("ORCID secret", re.compile(r"(?i)orcid[^\n]{0,24}[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
    ("PEM private key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Exa/UUID key assign", re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*[\"']?[0-9a-f]{32}[\"']?")),
]

ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,30})\s*[=:]\s*[\"']?([^\s\"']{16,})[\"']?")

DUMMY = re.compile(r"(?i)abcd|12345678|xxxx|fake|dummy|example|placeholder|your[_-]?key")


def is_dummy(val):
    return bool(DUMMY.search(val))


def entropy(s):
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def trunc(v):
    return v[:6] + "..." + ("(%d chars)" % len(v))


def scan_text(name, text):
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if "SECRETS-GUARD-IGNORE" in line:
            continue
        for label, pat in PATTERNS:
            m = pat.search(line)
            if m and not is_dummy(m.group(0)):
                hits.append((name, i, label, trunc(m.group(0))))
                break
        else:
            m = ASSIGN.match(line)
            if m and m.group(1) not in ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH"):
                val = m.group(2)
                looks_like_code = any(c in val for c in "()[]{}<>")
                if (not looks_like_code and not is_dummy(val)
                        and entropy(val) > 3.6
                        and not val.startswith(("http", "$", "/"))):
                    hits.append((name, i, "high-entropy assignment",
                                 "%s=%s" % (m.group(1), trunc(val))))
    return hits


def git_files(mode):
    if mode == "--staged":
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--worktree", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    targets = []
    if args.staged or args.worktree:
        for f in git_files("--staged" if args.staged else "--worktree"):
            if os.path.isfile(f):
                targets.append(f)
    for path in args.paths:
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
                for f in files:
                    targets.append(os.path.join(root, f))
        else:
            targets.append(path)
    if not targets:
        ap.error("nothing to scan (use --staged, --worktree, or paths)")

    hits = []
    for path in targets:
        try:
            if os.path.getsize(path) > 2_000_000:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                hits.extend(scan_text(path, fh.read()))
        except OSError:
            continue

    for name, line, label, val in hits:
        print("SECRETS-GUARD: %s:%d %s: %s" % (name, line, label, val))
    if hits:
        print("SECRETS-GUARD: %d hit(s) — rotate if committed, move to "
              "~/.config/ai-keys.env, then re-run." % len(hits))
        return 1
    print("secrets-guard: clean (%d file(s))" % len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
