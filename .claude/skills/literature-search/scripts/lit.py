#!/usr/bin/env python3
"""lit.py — CLI fallback for the scholar MCP tools (harnesses without MCP).

Usage: lit.py pubmed|openalex|s2|core QUERY [--limit N] [--year-from Y]
       lit.py fetch-pubmed PMID [PMID ...]
       lit.py doi 10.xxxx/yyy
       lit.py orcid 0000-0002-1825-0097
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.join(_HERE, "..", "..", "..", "mcp"),               # toolbox layout
                  os.path.join(_HERE, "..", "..", "..", "..", ".agent-tools", "mcp"),  # repo-vendored
                  os.path.join(os.getcwd(), ".agent-tools", "mcp")):          # cwd fallback
    if os.path.isfile(os.path.join(candidate, "scholar_mcp.py")):
        sys.path.insert(0, os.path.abspath(candidate))
        break

import scholar_mcp  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("pubmed", "openalex", "s2", "core"):
        sp = sub.add_parser(name)
        sp.add_argument("query")
        sp.add_argument("--limit", type=int, default=8)
        if name == "openalex":
            sp.add_argument("--year-from", type=int, default=None)
    fp = sub.add_parser("fetch-pubmed")
    fp.add_argument("pmids", nargs="+")
    dp = sub.add_parser("doi")
    dp.add_argument("doi")
    op = sub.add_parser("orcid")
    op.add_argument("orcid_id")
    args = p.parse_args()

    if args.cmd == "pubmed":
        out = scholar_mcp.pubmed_search(args.query, args.limit)
    elif args.cmd == "openalex":
        out = scholar_mcp.openalex_search(args.query, args.limit, args.year_from)
    elif args.cmd == "s2":
        out = scholar_mcp.semantic_scholar_search(args.query, args.limit)
    elif args.cmd == "core":
        out = scholar_mcp.core_search(args.query, args.limit)
    elif args.cmd == "fetch-pubmed":
        out = scholar_mcp.pubmed_fetch(args.pmids)
    elif args.cmd == "doi":
        out = scholar_mcp.crossref_lookup(args.doi)
    else:
        out = scholar_mcp.orcid_record(args.orcid_id)
    json.dump(out, sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
