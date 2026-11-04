#!/usr/bin/env python3
"""web.py — CLI fallback for the web MCP tools (harnesses without MCP).

Usage: web.py search QUERY [--provider auto|exa|brave|firecrawl] [--limit N]
       web.py read URL [--provider auto|jina|firecrawl] [--max-chars N]
       web.py exa|brave|jina-search|firecrawl-scrape ARGS...
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for candidate in (os.path.join(_HERE, "..", "..", "..", "mcp"),               # toolbox layout
                  os.path.join(_HERE, "..", "..", "..", "..", ".agent-tools", "mcp"),  # repo-vendored
                  os.path.join(os.getcwd(), ".agent-tools", "mcp")):          # cwd fallback
    if os.path.isfile(os.path.join(candidate, "web_mcp.py")):
        sys.path.insert(0, os.path.abspath(candidate))
        break

import web_mcp  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--provider", default="auto")
    sp.add_argument("--limit", type=int, default=8)
    rp = sub.add_parser("read")
    rp.add_argument("url")
    rp.add_argument("--provider", default="auto")
    rp.add_argument("--max-chars", type=int, default=20000)
    ep = sub.add_parser("exa")
    ep.add_argument("query")
    ep.add_argument("--limit", type=int, default=8)
    bp = sub.add_parser("brave")
    bp.add_argument("query")
    bp.add_argument("--count", type=int, default=8)
    jp = sub.add_parser("jina-search")
    jp.add_argument("query")
    fp = sub.add_parser("firecrawl-scrape")
    fp.add_argument("url")
    args = p.parse_args()

    if args.cmd == "search":
        out = web_mcp.web_search(args.query, args.provider, args.limit)
    elif args.cmd == "read":
        out = web_mcp.web_read(args.url, args.provider, args.max_chars)
    elif args.cmd == "exa":
        out = web_mcp.exa_search(args.query, args.limit)
    elif args.cmd == "brave":
        out = web_mcp.brave_search(args.query, args.count)
    elif args.cmd == "jina-search":
        out = web_mcp.jina_search(args.query)
    else:
        out = web_mcp.firecrawl_scrape(args.url)
    json.dump(out, sys.stdout, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
