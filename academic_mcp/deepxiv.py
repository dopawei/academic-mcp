"""DeepXiv adapter and MCP tools."""

from __future__ import annotations

import json
import os
from typing import Any

from fastmcp import FastMCP

from .schema import (
    PaperRecord,
    build_record_id,
    clean_text,
    parse_int,
    parse_year,
    split_authors,
)


DEEPXIV_SOURCES = ("arxiv", "biorxiv", "medrxiv")


def _load_reader():
    try:
        from deepxiv_sdk.reader import Reader
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "deepxiv-sdk is not installed in this Python environment. "
            "Install it with `uv add deepxiv-sdk` or run the server from the "
            "environment where deepxiv_sdk is available."
        ) from exc
    token = os.environ.get("DEEPXIV_TOKEN")
    if not token:
        try:
            from deepxiv_sdk.cli import ensure_token

            token = ensure_token(auto_create=True)
        except Exception:
            token = None
    return Reader(token=token)


def deepxiv_status() -> dict[str, Any]:
    try:
        import deepxiv_sdk

        token = os.environ.get("DEEPXIV_TOKEN")
        if not token:
            try:
                from deepxiv_sdk.cli import get_token

                token = get_token(None)
            except Exception:
                token = None

        return {
            "available": True,
            "module": getattr(deepxiv_sdk, "__file__", ""),
            "token_configured": bool(token),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _source_label(source: str) -> str:
    return f"deepxiv:{source}"


def _paper_url(source: str, paper_id: str, raw: dict[str, Any]) -> str:
    for key in ("url", "paper_url", "html_url", "abs_url"):
        if raw.get(key):
            return clean_text(raw[key])
    if raw.get("src_url"):
        return clean_text(raw["src_url"])
    if source == "arxiv" and paper_id:
        return f"https://arxiv.org/abs/{paper_id}"
    if source in {"biorxiv", "medrxiv"} and paper_id:
        return f"https://doi.org/{paper_id}"
    return ""


def _keywords(raw: dict[str, Any]) -> list[str]:
    value = raw.get("keywords") or raw.get("categories") or []
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    if value:
        return [clean_text(item) for item in str(value).split(",") if clean_text(item)]
    return []


def paper_from_deepxiv(raw: dict[str, Any], source: str, rank: int) -> PaperRecord:
    id_field = f"{source}_id"
    paper_id = clean_text(
        raw.get(id_field)
        or raw.get("arxiv_id")
        or raw.get("biorxiv_id")
        or raw.get("medrxiv_id")
        or raw.get("doi")
        or raw.get("id")
    )
    doi = clean_text(raw.get("doi") or (paper_id if source in {"biorxiv", "medrxiv"} else ""))
    title = clean_text(raw.get("title") or "Untitled")
    url = _paper_url(source, paper_id, raw)
    citation_count = parse_int(raw.get("citation_count", raw.get("citation", raw.get("citations"))))
    score = raw.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    return PaperRecord(
        id=build_record_id(_source_label(source), title=title, doi=doi, url=url, external_id=paper_id),
        title=title,
        authors=split_authors(raw.get("authors")),
        year=parse_year(raw.get("date"), raw.get("publish_at"), raw.get("published")),
        venue=clean_text(raw.get("journal") or raw.get("venue") or raw.get("source")),
        abstract=clean_text(raw.get("abstract") or raw.get("tldr")),
        doi=doi,
        url=url,
        source=_source_label(source),
        source_rank=rank,
        citation_count=citation_count,
        keywords=_keywords(raw),
        score=score,
        raw=raw,
    )


def search_deepxiv_sources(
    query: str,
    sources: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
    categories: list[str] | None = None,
    authors: list[str] | None = None,
    orgs: list[str] | None = None,
    min_citation: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    use_fine_rerank: bool = False,
) -> dict[str, Any]:
    if not query or not query.strip():
        raise ValueError("query cannot be empty")
    reader = _load_reader()
    requested = sources or ["arxiv"]
    records: list[PaperRecord] = []
    status: list[dict[str, Any]] = []

    for source in requested:
        source = source.strip().lower()
        if source not in DEEPXIV_SOURCES:
            status.append({"source": source, "status": "skipped", "error": "unsupported DeepXiv source"})
            continue
        try:
            data = reader.search(
                query=query,
                size=max(1, min(limit, 100)),
                offset=offset,
                source=source,
                categories=categories,
                authors=authors,
                orgs=orgs,
                min_citation=min_citation,
                date_from=date_from,
                date_to=date_to,
                use_fine_rerank=use_fine_rerank,
            )
            items = data.get("result", []) if isinstance(data, dict) else []
            for rank, item in enumerate(items):
                if isinstance(item, dict):
                    records.append(paper_from_deepxiv(item, source, rank))
            status.append(
                {
                    "source": _source_label(source),
                    "status": "ok",
                    "count": len(items),
                    "total_count": data.get("total_count", len(items)) if isinstance(data, dict) else len(items),
                }
            )
        except Exception as exc:
            status.append({"source": _source_label(source), "status": "error", "error": str(exc)})

    return {"records": records, "source_status": status}


def _csv_list(value: str) -> list[str] | None:
    items = [clean_text(item) for item in (value or "").split(",") if clean_text(item)]
    return items or None


def register_deepxiv_tools(mcp: FastMCP):
    @mcp.tool()
    def search_deepxiv(
        query: str,
        sources: str = "arxiv,biorxiv,medrxiv",
        limit: int = 10,
        offset: int = 0,
        categories: str = "",
        authors: str = "",
        orgs: str = "",
        min_citation: int | None = None,
        date_from: str = "",
        date_to: str = "",
        use_fine_rerank: bool = False,
    ) -> str:
        """Search open literature through DeepXiv and return normalized JSON."""
        result = search_deepxiv_sources(
            query=query,
            sources=_csv_list(sources) or ["arxiv"],
            limit=limit,
            offset=offset,
            categories=_csv_list(categories),
            authors=_csv_list(authors),
            orgs=_csv_list(orgs),
            min_citation=min_citation,
            date_from=date_from or None,
            date_to=date_to or None,
            use_fine_rerank=use_fine_rerank,
        )
        payload = {
            "query": query,
            "source_status": result["source_status"],
            "total_results": len(result["records"]),
            "results": [record.to_dict() for record in result["records"]],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_deepxiv_paper(arxiv_id: str, view: str = "brief") -> str:
        """Read an arXiv paper via DeepXiv: brief, metadata, preview, raw, or json."""
        reader = _load_reader()
        view = (view or "brief").lower()
        if view == "brief":
            data = reader.brief(arxiv_id)
        elif view in {"metadata", "head"}:
            data = reader.head(arxiv_id)
        elif view == "preview":
            data = reader.preview(arxiv_id)
        elif view == "raw":
            return reader.raw(arxiv_id)
        elif view == "json":
            data = reader.json(arxiv_id)
        else:
            return json.dumps({"error": "view must be brief, metadata, preview, raw, or json"}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_deepxiv_pmc(pmc_id: str, full: bool = False) -> str:
        """Read PMC metadata or full structured content via DeepXiv."""
        reader = _load_reader()
        data = reader.pmc_full(pmc_id) if full else reader.pmc_head(pmc_id)
        return json.dumps(data, ensure_ascii=False, indent=2)

    @mcp.tool()
    def check_deepxiv_status() -> str:
        """Check whether deepxiv-sdk is importable and whether a token is configured."""
        return json.dumps(deepxiv_status(), ensure_ascii=False, indent=2)
