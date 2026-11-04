#!/usr/bin/env python3
"""scholar_mcp.py — stdio MCP server: scholarly literature + author identity.

Tools (all read-only): pubmed_search, pubmed_fetch, openalex_search,
semantic_scholar_search, core_search, orcid_record, crossref_lookup.

Keys load via agentkeys (process env -> repo .env -> ~/.config/ai-keys.env).
Polite-pool identity comes from EMAIL/CONTACT_EMAIL. No third-party deps.
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
TIMEOUT = 30
UA = {"User-Agent": "agent-toolbox-scholar/1.0 (research use)"}


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_text(url, headers=None):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**UA, "Content-Type": "application/json", **(headers or {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _email():
    return get_key("EMAIL") or get_key("CONTACT_EMAIL") or ""


def _n(v, lo=1, hi=25):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return lo


# ---------------------------------------------------------------- handlers

def pubmed_search(query, limit=8):
    key = get_key("NCBI_API_KEY")
    params = {"db": "pubmed", "term": query, "retmode": "json",
              "retmax": str(_n(limit)), "email": _email()}
    if key:
        params["api_key"] = key
    es = _get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                   + urllib.parse.urlencode(params))
    res = es.get("esearchresult", {})
    ids = res.get("idlist", [])
    if not ids:
        return {"query": query, "count": res.get("count", 0), "results": []}
    sparams = {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    if key:
        sparams["api_key"] = key
    su = _get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                   + urllib.parse.urlencode(sparams))
    out = []
    for pmid in ids:
        doc = su.get("result", {}).get(pmid, {})
        out.append({
            "pmid": pmid,
            "title": doc.get("title"),
            "journal": doc.get("fulljournalname") or doc.get("source"),
            "year": (doc.get("pubdate") or "")[:4],
            "doi": next((i.get("value") for i in doc.get("articleids", [])
                         if i.get("idtype") == "doi"), None),
            "authors": [a.get("name") for a in doc.get("authors", [])][:6],
        })
    return {"query": query, "count": res.get("count"), "results": out}


def pubmed_fetch(pmids, max_chars=8000):
    if isinstance(pmids, str):
        pmids = [p.strip() for p in pmids.replace(",", " ").split() if p.strip()]
    params = {"db": "pubmed", "id": ",".join(pmids[:10]),
              "rettype": "abstract", "retmode": "text"}
    if get_key("NCBI_API_KEY"):
        params["api_key"] = get_key("NCBI_API_KEY")
    text = _get_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
                     + urllib.parse.urlencode(params))
    return {"pmids": pmids[:10], "abstract_text": text[:_n(max_chars, 500, 40000)]}


def openalex_search(query, limit=8, year_from=None):
    params = {"search": query, "per-page": str(_n(limit)), "mailto": _email()}
    if get_key("OPENALEX_API_KEY"):
        params["api_key"] = get_key("OPENALEX_API_KEY")
    if year_from:
        params["filter"] = "from_publication_date:%d-01-01" % int(year_from)
    data = _get_json("https://api.openalex.org/works?"
                     + urllib.parse.urlencode(params))
    out = []
    for w in data.get("results", []):
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        oa = w.get("open_access") or {}
        out.append({
            "openalex_id": w.get("id"), "title": w.get("title"),
            "year": w.get("publication_year"), "doi": w.get("doi"),
            "cited_by_count": w.get("cited_by_count"),
            "journal": src.get("display_name"),
            "is_oa": oa.get("is_oa"), "oa_url": oa.get("oa_url"),
        })
    return {"query": query, "count": data.get("meta", {}).get("count"),
            "results": out}


def semantic_scholar_search(query, limit=8):
    params = {"query": query, "limit": str(_n(limit)),
              "fields": "title,year,abstract,externalIds,citationCount,venue,openAccessPdf"}
    headers = {}
    if get_key("SEMANTIC_SCHOLAR_API_KEY"):
        headers["x-api-key"] = get_key("SEMANTIC_SCHOLAR_API_KEY")
    data = _get_json("https://api.semanticscholar.org/graph/v1/paper/search?"
                     + urllib.parse.urlencode(params), headers)
    out = []
    for p in data.get("data", []):
        ext = p.get("externalIds") or {}
        out.append({
            "paper_id": p.get("paperId"), "title": p.get("title"),
            "year": p.get("year"), "venue": p.get("venue"),
            "citation_count": p.get("citationCount"),
            "doi": ext.get("DOI"), "pmid": ext.get("PubMed"),
            "abstract": (p.get("abstract") or "")[:400] or None,
            "oa_pdf": (p.get("openAccessPdf") or {}).get("url"),
        })
    return {"query": query, "total": data.get("total"), "results": out}


def core_search(query, limit=8):
    headers = {}
    if get_key("CORE_API_KEY"):
        headers["Authorization"] = "Bearer " + get_key("CORE_API_KEY")
    data = _post_json("https://api.core.ac.uk/v3/search/works",
                      {"q": query, "limit": _n(limit)}, headers)
    out = []
    for w in data.get("results", []):
        out.append({
            "title": w.get("title"), "year": w.get("yearPublished"),
            "doi": w.get("doi"), "download_url": w.get("downloadUrl"),
            "authors": [a.get("name") for a in (w.get("authors") or [])][:6],
            "abstract": (w.get("abstract") or "")[:400] or None,
        })
    return {"query": query, "total": data.get("totalHits"), "results": out}


def orcid_record(orcid_id):
    orcid_id = orcid_id.strip().replace("https://orcid.org/", "")
    data = _get_json("https://pub.orcid.org/v3.0/%s/record" % orcid_id,
                     {"Accept": "application/json"})
    person = data.get("person") or {}
    name = person.get("name") or {}
    credit = (name.get("credit-name") or {}).get("value")
    given = (name.get("given-names") or {}).get("value") or ""
    family = (name.get("family-name") or {}).get("value") or ""
    works = []
    groups = (data.get("activities-summary") or {}).get("works", {}).get("group", [])
    for g in groups[:15]:
        summaries = g.get("work-summary") or [{}]
        s = summaries[0]
        title_obj = (s.get("title") or {}).get("title") or {}
        year_obj = ((s.get("publication-date") or {}).get("year") or {})
        works.append({
            "title": title_obj.get("value"),
            "year": year_obj.get("value"),
            "type": s.get("type"),
        })
    return {"orcid": orcid_id,
            "name": credit or (given + " " + family).strip(),
            "works_listed": len(works), "recent_works": works}


def crossref_lookup(doi):
    doi = doi.strip().replace("https://doi.org/", "")
    data = _get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    m = data.get("message", {})
    return {
        "doi": m.get("DOI"),
        "title": (m.get("title") or [None])[0],
        "journal": (m.get("container-title") or [None])[0],
        "type": m.get("type"),
        "year": ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0],
        "cited_by": m.get("is-referenced-by-count"),
        "publisher": m.get("publisher"),
        "url": m.get("URL"),
    }


# ---------------------------------------------------------------- MCP layer

TOOLS = [
    {"name": "pubmed_search",
     "description": "Search PubMed (NCBI eutils). Returns PMIDs with titles, journals, years, DOIs.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "PubMed query, e.g. 'CRISPR base editing iPSC'"},
         "limit": {"type": "integer", "description": "1-25 results (default 8)"}},
         "required": ["query"]}},
    {"name": "pubmed_fetch",
     "description": "Fetch abstract text for PubMed IDs (up to 10).",
     "inputSchema": {"type": "object", "properties": {
         "pmids": {"type": "array", "items": {"type": "string"}},
         "max_chars": {"type": "integer"}},
         "required": ["pmids"]}},
    {"name": "openalex_search",
     "description": "Search OpenAlex works. Returns title, year, DOI, citation counts, OA links.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer"},
         "year_from": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "semantic_scholar_search",
     "description": "Search Semantic Scholar papers. Returns titles, years, citation counts, DOIs, PMIDs, OA PDFs.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "core_search",
     "description": "Search CORE open-access full texts. Returns titles, DOIs, download URLs, abstracts.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "orcid_record",
     "description": "Fetch an ORCID public record by ORCID iD: name and recent works.",
     "inputSchema": {"type": "object", "properties": {
         "orcid_id": {"type": "string", "description": "e.g. 0000-0002-1825-0097"}},
         "required": ["orcid_id"]}},
    {"name": "crossref_lookup",
     "description": "Resolve a DOI via Crossref: title, journal, year, type, citation count.",
     "inputSchema": {"type": "object", "properties": {
         "doi": {"type": "string"}}, "required": ["doi"]}},
]

HANDLERS = {
    "pubmed_search": lambda a: pubmed_search(a["query"], a.get("limit", 8)),
    "pubmed_fetch": lambda a: pubmed_fetch(a["pmids"], a.get("max_chars", 8000)),
    "openalex_search": lambda a: openalex_search(a["query"], a.get("limit", 8), a.get("year_from")),
    "semantic_scholar_search": lambda a: semantic_scholar_search(a["query"], a.get("limit", 8)),
    "core_search": lambda a: core_search(a["query"], a.get("limit", 8)),
    "orcid_record": lambda a: orcid_record(a["orcid_id"]),
    "crossref_lookup": lambda a: crossref_lookup(a["doi"]),
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
                "serverInfo": {"name": "scholar", "version": "1.0.0"}})
        elif method == "ping":
            _reply(msg_id, {})
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = HANDLERS.get(name)
            if not handler:
                _reply_err(msg_id, -32602, "unknown tool: %s" % name)
                continue
            try:
                result = handler(args)
                _reply(msg_id, {"content": [{"type": "text",
                                             "text": json.dumps(result, indent=2, default=str)}],
                                "isError": False})
            except Exception as exc:  # surface provider errors, never crash the server
                _reply(msg_id, {"content": [{"type": "text",
                                             "text": "%s: %s" % (type(exc).__name__, exc)}],
                                "isError": True})
        elif method.startswith("notifications/"):
            pass
        elif msg_id is not None:
            _reply_err(msg_id, -32601, "method not found: %s" % method)


if __name__ == "__main__":
    main()
