# academic-mcp

Unified academic search MCP server for open literature, CNKI, and Web of Science.

The project combines three workflows:

- Open literature search through `deepxiv-sdk` for arXiv, bioRxiv, medRxiv, PMC, and paper reading.
- Browser-backed CNKI search, detail lookup, and download with persistent Playwright login state.
- Browser-backed Web of Science advanced search, detail lookup, and export with persistent institutional authentication.

It also adds a local paper library, cross-source deduplication, search cache, named collections, and export to BibTeX, RIS, CSV, JSONL, JSON, or Markdown.

## Why MCP First

MCP is the best first interface for this project because CNKI and Web of Science need user authentication, browser sessions, and occasional manual verification. MCP lets an AI assistant call the tools, ask the user to log in only when needed, and reuse the saved browser profile.

The reusable logic is kept outside the MCP tool functions in modules such as `schema.py`, `deepxiv.py`, `library.py`, and `unified.py`. That keeps the path open for a future Python SDK or web app without rewriting the search and collection logic.

## Install

```powershell
cd C:\Users\WeiZh\academic-mcp
uv sync --extra dev
uv run playwright install chromium
```

If you already use a separate scientific Conda environment, make sure the environment that runs `academic-mcp` can import `deepxiv_sdk`.

## Run

```powershell
uv run academic-mcp
```

For visible browser login:

```powershell
$env:ACADEMIC_HEADLESS = "false"
uv run academic-mcp
```

The browser profile and downloads are stored under:

```text
%USERPROFILE%\.academic-mcp\
```

## Main Tools

`search_literature` is the unified entry point.

Fast open search:

```json
{
  "query": "structural health monitoring transformer",
  "sources": "open",
  "limit": 10,
  "save_as": "shm-transformer"
}
```

Full search after CNKI/WoS login:

```json
{
  "query": "structural health monitoring transformer",
  "sources": "all",
  "limit": 20,
  "mode": "balanced",
  "save_as": "shm-transformer-all"
}
```

Web of Science advanced query:

```json
{
  "query": "structural health monitoring transformer",
  "sources": "wos",
  "wos_query": "TS=(structural health monitoring AND transformer) AND PY=(2020-2026)",
  "limit": 20
}
```

Collection tools:

- `list_paper_collections`
- `get_paper_collection`
- `export_paper_collection`
- `save_papers_to_collection`

DeepXiv tools:

- `search_deepxiv`
- `get_deepxiv_paper`
- `get_deepxiv_pmc`
- `check_deepxiv_status`

Existing authenticated tools are still available:

- CNKI: `search_cnki`, `get_paper_detail`, `download_paper`, `login_cnki`, `check_cnki_status`
- WoS: `search_wos`, `get_wos_detail`, `export_wos`, `login_wos`, `check_wos_status`, `debug_wos`

## Source Strategy

For convenience, start with `sources="open"` because it is fast and does not require browser authentication.

For accuracy, use `sources="all"` after login. The unified search deduplicates by DOI, arXiv ID, and normalized title. It ranks results with a balanced score that considers source rank, citation count, source authority, and recency.

Suggested workflow:

1. Use `search_literature(..., sources="open")` for discovery.
2. Run `login_wos` and `login_cnki` once when authenticated sources are needed.
3. Use `search_literature(..., sources="all", save_as="...")`.
4. Export the collection with `export_paper_collection`.

## Local Data

The local SQLite library is stored at:

```text
%USERPROFILE%\.academic-mcp\library.sqlite3
```

Exports are written to:

```text
%USERPROFILE%\.academic-mcp\exports\
```

Override paths with:

```powershell
$env:ACADEMIC_LIBRARY_DB = "D:\papers\academic.sqlite3"
$env:ACADEMIC_EXPORT_DIR = "D:\papers\exports"
```

## Tests

```powershell
uv run pytest
```
