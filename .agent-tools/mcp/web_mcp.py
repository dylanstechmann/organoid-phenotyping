#!/usr/bin/env python3
"""web_mcp.py — stdio MCP server: web search + page reading (read-only).

Tools: web_search, web_read, exa_search, exa_find_similar, brave_search,
jina_search, jina_read, firecrawl_search, firecrawl_scrape.

Keys load via agentkeys (process env -> repo .env -> ~/.config/ai-keys.env).
Only GET/POST against fixed provider endpoints; no writes, no arbitrary shell.
"""
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentkeys import get_key  # noqa: E402

SSL_CTX = ssl.create_default_context()
TIMEOUT = 45
UA = {"User-Agent": "agent-toolbox-web/1.0"}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _get_json(url, headers=None):
    return json.loads(_get(url, headers))


def _post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**UA, "Content-Type": "application/json", **(headers or {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clip(text, max_chars=20000):
    text = text or ""
    return text[:max(500, min(int(max_chars), 100000))]


def _norm_url(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http(s) URLs are allowed: %r" % url)
    return url


# ---------------------------------------------------------------- providers

def exa_search(query, limit=8, get_contents=True):
    payload = {"query": query, "numResults": max(1, min(int(limit), 25)), "type": "auto"}
    if get_contents:
        payload["contents"] = {"text": {"maxCharacters": 600}}
    data = _post_json("https://api.exa.ai/search", payload,
                      {"x-api-key": get_key("EXA_API_KEY")})
    return {"provider": "exa", "results": [
        {"title": r.get("title"), "url": r.get("url"),
         "published": r.get("publishedDate"),
         "text": (r.get("text") or "")[:600] or None,
         "score": r.get("score")}
        for r in data.get("results", [])]}


def exa_find_similar(url, limit=8):
    payload = {"url": _norm_url(url), "numResults": max(1, min(int(limit), 25)),
               "contents": {"text": {"maxCharacters": 600}}}
    data = _post_json("https://api.exa.ai/findSimilar", payload,
                      {"x-api-key": get_key("EXA_API_KEY")})
    return {"provider": "exa", "results": [
        {"title": r.get("title"), "url": r.get("url"),
         "text": (r.get("text") or "")[:600] or None}
        for r in data.get("results", [])]}


def brave_search(query, count=8):
    params = urllib.parse.urlencode({"q": query, "count": max(1, min(int(count), 20))})
    data = _get_json("https://api.search.brave.com/res/v1/web/search?" + params,
                     {"X-Subscription-Token": get_key("BRAVE_SEARCH_API_KEY"),
                      "Accept": "application/json"})
    return {"provider": "brave", "results": [
        {"title": r.get("title"), "url": r.get("url"),
         "description": r.get("description"),
         "age": r.get("age")}
        for r in (data.get("web") or {}).get("results", [])]}


def jina_search(query, max_chars=12000):
    url = "https://s.jina.ai/?" + urllib.parse.urlencode({"q": query})
    text = _get(url, {"Authorization": "Bearer " + get_key("JINA_API_KEY")})
    return {"provider": "jina", "markdown": _clip(text, max_chars)}


def jina_read(url, max_chars=20000):
    text = _get("https://r.jina.ai/" + _norm_url(url),
                {"Authorization": "Bearer " + get_key("JINA_API_KEY")})
    return {"provider": "jina", "url": url, "markdown": _clip(text, max_chars)}


def firecrawl_search(query, limit=6):
    data = _post_json("https://api.firecrawl.dev/v1/search",
                      {"query": query, "limit": max(1, min(int(limit), 10))},
                      {"Authorization": "Bearer " + get_key("FIRECRAWL_API_KEY")})
    return {"provider": "firecrawl", "results": [
        {"title": r.get("title") or (r.get("metadata") or {}).get("title"),
         "url": r.get("url") or (r.get("metadata") or {}).get("sourceURL"),
         "description": r.get("description")}
        for r in (data.get("data") or [])]}


def firecrawl_scrape(url, max_chars=20000):
    data = _post_json("https://api.firecrawl.dev/v1/scrape",
                      {"url": _norm_url(url), "formats": ["markdown"]},
                      {"Authorization": "Bearer " + get_key("FIRECRAWL_API_KEY")})
    body = data.get("data") or {}
    return {"provider": "firecrawl", "url": url,
            "markdown": _clip(body.get("markdown"), max_chars)}


def web_search(query, provider="auto", limit=8):
    """Fallback search: exa -> brave -> firecrawl."""
    order = {"exa": ["exa"], "brave": ["brave"], "firecrawl": ["firecrawl"],
             "auto": ["exa", "brave", "firecrawl"]}.get(provider, ["exa"])
    errors = []
    for p in order:
        try:
            fn = {"exa": exa_search, "brave": brave_search,
                  "firecrawl": firecrawl_search}[p]
            out = fn(query, limit)
            if out.get("results"):
                return out
            errors.append("%s: 0 results" % p)
        except Exception as exc:
            errors.append("%s: %s" % (p, type(exc).__name__))
    return {"provider": None, "results": [], "errors": errors}


def web_read(url, provider="auto", max_chars=20000):
    """Fallback page read (markdown): jina -> firecrawl."""
    order = {"jina": ["jina"], "firecrawl": ["firecrawl"],
             "auto": ["jina", "firecrawl"]}.get(provider, ["jina"])
    errors = []
    for p in order:
        try:
            fn = {"jina": jina_read, "firecrawl": firecrawl_scrape}[p]
            out = fn(url, max_chars)
            if out.get("markdown"):
                return out
            errors.append("%s: empty page" % p)
        except Exception as exc:
            errors.append("%s: %s" % (p, type(exc).__name__))
    return {"provider": None, "url": url, "markdown": "", "errors": errors}


# ---------------------------------------------------------------- MCP layer

TOOLS = [
    {"name": "web_search",
     "description": "Search the web with automatic provider fallback (exa -> brave -> firecrawl).",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "provider": {"type": "string", "enum": ["auto", "exa", "brave", "firecrawl"]},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "web_read",
     "description": "Fetch a web page as clean markdown (jina -> firecrawl fallback).",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "max_chars": {"type": "integer"},
         "provider": {"type": "string", "enum": ["auto", "jina", "firecrawl"]}},
         "required": ["url"]}},
    {"name": "exa_search",
     "description": "Neural/semantic web search via Exa, with short text snippets.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"},
         "get_contents": {"type": "boolean"}}, "required": ["query"]}},
    {"name": "exa_find_similar",
     "description": "Find pages similar to a URL via Exa.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["url"]}},
    {"name": "brave_search",
     "description": "Keyword web search via Brave Search API.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "count": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "jina_search",
     "description": "Search via Jina s.jina.ai; returns grounded markdown results.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "max_chars": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "jina_read",
     "description": "Read a URL as markdown via Jina reader (r.jina.ai).",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "max_chars": {"type": "integer"}},
         "required": ["url"]}},
    {"name": "firecrawl_search",
     "description": "Search via Firecrawl v1 search endpoint.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "firecrawl_scrape",
     "description": "Scrape a URL to markdown via Firecrawl.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "max_chars": {"type": "integer"}},
         "required": ["url"]}},
]

HANDLERS = {
    "web_search": lambda a: web_search(a["query"], a.get("provider", "auto"), a.get("limit", 8)),
    "web_read": lambda a: web_read(a["url"], a.get("provider", "auto"), a.get("max_chars", 20000)),
    "exa_search": lambda a: exa_search(a["query"], a.get("limit", 8), a.get("get_contents", True)),
    "exa_find_similar": lambda a: exa_find_similar(a["url"], a.get("limit", 8)),
    "brave_search": lambda a: brave_search(a["query"], a.get("count", 8)),
    "jina_search": lambda a: jina_search(a["query"], a.get("max_chars", 12000)),
    "jina_read": lambda a: jina_read(a["url"], a.get("max_chars", 20000)),
    "firecrawl_search": lambda a: firecrawl_search(a["query"], a.get("limit", 6)),
    "firecrawl_scrape": lambda a: firecrawl_scrape(a["url"], a.get("max_chars", 20000)),
}


def _reply(msg_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
    sys.stdout.flush()


def _reply_err(msg_id, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id,
                                 "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if method == "initialize":
            _reply(msg_id, {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web", "version": "1.0.0"}})
        elif method == "ping":
            _reply(msg_id, {})
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            handler = HANDLERS.get(params.get("name"))
            if not handler:
                _reply_err(msg_id, -32602, "unknown tool: %s" % params.get("name"))
                continue
            try:
                result = handler(params.get("arguments") or {})
                _reply(msg_id, {"content": [{"type": "text",
                                             "text": json.dumps(result, indent=2, default=str)}],
                                "isError": False})
            except Exception as exc:
                _reply(msg_id, {"content": [{"type": "text",
                                             "text": "%s: %s" % (type(exc).__name__, exc)}],
                                "isError": True})
        elif method.startswith("notifications/"):
            pass
        elif msg_id is not None:
            _reply_err(msg_id, -32601, "method not found: %s" % method)


if __name__ == "__main__":
    main()