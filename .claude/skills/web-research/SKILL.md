---
name: web-research
description: Search and read the web reliably using the repo's web MCP tools (or the bundled web.py CLI) across Exa (neural/semantic search), Brave (keyword search), Jina (clean page reads and grounded search) and Firecrawl (scrape/crawl as markdown). Use whenever a task needs current web information, documentation, vendor pages, datasets, protocols, news, or any URL opened and summarized. Records URLs actually fetched; keeps provider keys out of every file.
---

# Web research with provider fallback

Each repo vendors `web` MCP tools (`.agent-tools/mcp/web_mcp.py`) and this CLI
fallback (`scripts/web.py`). Keys load from `~/.config/ai-keys.env`; never paste
keys anywhere.

## Which provider for what

| Provider | Use it for | Tool |
|---|---|---|
| Exa | Conceptual/semantic queries, finding pages *similar* to a reference | `exa_search`, `exa_find_similar` |
| Brave | Keyword queries, news, product/vendor pages | `brave_search` |
| Jina | Reading one page as clean markdown; grounded search answers | `jina_read`, `jina_search` |
| Firecrawl | Pages Jina can't render; scraping to markdown | `firecrawl_scrape`, `firecrawl_search` |

`web_search` / `web_read` try providers in order and fall back automatically —
start there and only call a specific provider when you need its specialty.

## CLI fallback (harnesses without MCP)

```bash
P=.claude/skills/web-research/scripts/web.py    # path inside the repo
python3 $P search "iPSC colony QC brightfield benchmark" --limit 5
python3 $P read "https://docs.example.com/protocol" --max-chars 8000
python3 $P exa "papers about organoid oxygen transport"
```

## Rules

1. **Cite what you fetched, not what you remember.** Save the URL and retrieval
   date next to any claim that came from a page; if the page is behind a login,
   say so rather than paraphrasing from memory.
2. **Search results are leads, not sources.** Open the page (`web_read`) before
   quoting it — snippets and titles are frequently wrong or out of date.
3. **Keep reads bounded** (`max_chars`) and cache what matters into the repo's
   docs or scratch space instead of re-fetching.
4. **For scientific claims, prefer the literature stack** (`literature-search`
   skill) over web pages; vendor docs and protocol pages are fine for product
   facts, parameters and installation instructions.
5. **No writes through these tools.** They fetch and scrape only; anything that
   would mutate an external service is out of scope here.
