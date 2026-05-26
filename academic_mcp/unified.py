"""Unified literature search tools across open and authenticated sources."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from fastmcp import Context, FastMCP

from . import cnki as cnki_module
from . import wos as wos_module
from .deepxiv import DEEPXIV_SOURCES, deepxiv_status, search_deepxiv_sources
from .library import (
    export_collection,
    get_cached_search,
    get_collection,
    list_collections,
    make_cache_key,
    save_collection,
    save_search_cache,
    upsert_papers,
)
from .schema import (
    PaperRecord,
    build_record_id,
    clean_text,
    dedupe_records,
    parse_int,
    parse_year,
    rank_records,
    split_authors,
)


def _csv_items(value: str) -> list[str]:
    return [clean_text(item).lower() for item in (value or "").split(",") if clean_text(item)]


def _resolve_sources(sources: str, deepxiv_sources: str) -> tuple[list[str], list[str]]:
    requested = _csv_items(sources or "open")
    if not requested:
        requested = ["open"]

    auth_sources: list[str] = []
    open_sources: list[str] = []

    for source in requested:
        if source == "all":
            open_sources.extend(DEEPXIV_SOURCES)
            auth_sources.extend(["wos", "cnki"])
        elif source in {"open", "deepxiv"}:
            open_sources.extend(_csv_items(deepxiv_sources) or list(DEEPXIV_SOURCES))
        elif source in DEEPXIV_SOURCES:
            open_sources.append(source)
        elif source in {"wos", "cnki"}:
            auth_sources.append(source)
        else:
            auth_sources.append(source)

    open_sources = [source for source in dict.fromkeys(open_sources) if source in DEEPXIV_SOURCES]
    auth_sources = list(dict.fromkeys(auth_sources))
    return open_sources, auth_sources


def _looks_like_wos_query(query: str) -> bool:
    return any(token in query for token in ["=", "(", ")", " AND ", " OR ", " NOT "])


def _to_wos_query(query: str) -> str:
    return query if _looks_like_wos_query(query) else f"TS=({query})"


def _auth_error_status(source: str, error: str) -> dict[str, Any]:
    lowered = error.lower()
    if "login" in lowered or "auth" in lowered or "captcha" in lowered:
        return {"source": source, "status": "skipped_auth_required", "error": error}
    return {"source": source, "status": "error", "error": error}


def _load_json_result(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        return {"error": "source returned non-JSON output", "raw": text}


def _from_cnki(item: dict[str, Any], rank: int) -> PaperRecord | None:
    title = clean_text(item.get("title"))
    if not title:
        return None
    url = clean_text(item.get("url"))
    venue = clean_text(item.get("source") or item.get("journal"))
    doi = clean_text(item.get("doi"))
    return PaperRecord(
        id=build_record_id("cnki", title=title, doi=doi, url=url),
        title=title,
        authors=split_authors(item.get("authors")),
        year=parse_year(item.get("date"), item.get("year"), item.get("publication_info")),
        venue=venue,
        doi=doi,
        url=url,
        source="cnki",
        source_rank=rank,
        citation_count=parse_int(item.get("cited") or item.get("cited_count")),
        raw=item,
    )


def _from_wos(item: dict[str, Any], rank: int) -> PaperRecord | None:
    title = clean_text(item.get("title"))
    if not title:
        return None
    url = clean_text(item.get("url"))
    venue = clean_text(item.get("source") or item.get("journal"))
    doi = clean_text(item.get("doi"))
    return PaperRecord(
        id=build_record_id("wos", title=title, doi=doi, url=url),
        title=title,
        authors=split_authors(item.get("authors")),
        year=parse_year(item.get("year"), item.get("date"), item.get("publication_info")),
        venue=venue,
        abstract=clean_text(item.get("abstract")),
        doi=doi,
        url=url,
        source="wos",
        source_rank=rank,
        citation_count=parse_int(item.get("cited") or item.get("cited_count")),
        keywords=split_authors(item.get("keywords")),
        raw=item,
    )


async def _search_cnki(
    query: str,
    limit: int,
    search_type: str,
    sort_by: str,
    ctx: Context | None,
) -> tuple[list[PaperRecord], dict[str, Any]]:
    func = getattr(cnki_module, "search_cnki_impl", None)
    if not func:
        return [], {"source": "cnki", "status": "not_registered"}
    page_count = max(1, min(5, math.ceil(limit / 20)))
    try:
        text = await func(keyword=query, search_type=search_type, page_count=page_count, sort_by=sort_by, ctx=ctx)
        data = _load_json_result(text)
    except Exception as exc:
        return [], _auth_error_status("cnki", str(exc))
    if data.get("error"):
        return [], _auth_error_status("cnki", clean_text(data["error"]))
    items = data.get("results", [])
    records = [record for rank, item in enumerate(items) if (record := _from_cnki(item, rank))]
    return records, {"source": "cnki", "status": "ok", "count": len(records)}


async def _search_wos(
    query: str,
    limit: int,
    sort_by: str,
    ctx: Context | None,
) -> tuple[list[PaperRecord], dict[str, Any]]:
    func = getattr(wos_module, "search_wos_impl", None)
    if not func:
        return [], {"source": "wos", "status": "not_registered"}
    page_count = max(1, min(5, math.ceil(limit / 50)))
    try:
        text = await func(query=query, sort_by=sort_by, page_count=page_count, ctx=ctx)
        data = _load_json_result(text)
    except Exception as exc:
        return [], _auth_error_status("wos", str(exc))
    if data.get("error"):
        return [], _auth_error_status("wos", clean_text(data["error"]))
    items = data.get("results", [])
    records = [record for rank, item in enumerate(items) if (record := _from_wos(item, rank))]
    return records, {"source": "wos", "status": "ok", "count": len(records)}


def _records_from_payload(papers_json: str) -> list[PaperRecord]:
    payload = json.loads(papers_json)
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("papers", []))
    records: list[PaperRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        record = PaperRecord(
            id=clean_text(item.get("id")) or build_record_id(
                clean_text(item.get("source") or "manual"),
                title=clean_text(item.get("title")),
                doi=clean_text(item.get("doi")),
                url=clean_text(item.get("url")),
            ),
            title=clean_text(item.get("title") or "Untitled"),
            authors=split_authors(item.get("authors")),
            year=clean_text(item.get("year")),
            venue=clean_text(item.get("venue")),
            abstract=clean_text(item.get("abstract")),
            doi=clean_text(item.get("doi")),
            url=clean_text(item.get("url")),
            source=clean_text(item.get("source")),
            source_rank=int(item.get("source_rank") or 0),
            citation_count=parse_int(item.get("citation_count")),
            keywords=split_authors(item.get("keywords")),
            pdf_path=clean_text(item.get("pdf_path")),
            auth_required=bool(item.get("auth_required")),
            score=item.get("score"),
            raw=item.get("raw") if isinstance(item.get("raw"), dict) else {},
        )
        records.append(record)
    return records


def register_unified_tools(mcp: FastMCP):
    @mcp.tool()
    async def search_literature(
        query: str,
        sources: str = "open",
        limit: int = 10,
        mode: str = "balanced",
        save_as: str = "",
        use_cache: bool = True,
        cache_max_age_hours: int = 24,
        deepxiv_sources: str = "arxiv,biorxiv,medrxiv",
        categories: str = "",
        authors: str = "",
        orgs: str = "",
        min_citation: int | None = None,
        date_from: str = "",
        date_to: str = "",
        use_fine_rerank: bool = False,
        cnki_search_type: str = "topic",
        cnki_sort_by: str = "relevance",
        wos_query: str = "",
        wos_sort_by: str = "relevance",
        ctx: Context = None,
    ) -> str:
        """Search literature across open DeepXiv sources and optional CNKI/WoS."""
        limit = max(1, min(limit, 100))
        open_sources, auth_sources = _resolve_sources(sources, deepxiv_sources)
        params = {
            "query": query,
            "sources": sources,
            "limit": limit,
            "mode": mode,
            "deepxiv_sources": open_sources,
            "auth_sources": auth_sources,
            "categories": categories,
            "authors": authors,
            "orgs": orgs,
            "min_citation": min_citation,
            "date_from": date_from,
            "date_to": date_to,
            "use_fine_rerank": use_fine_rerank,
            "cnki_search_type": cnki_search_type,
            "cnki_sort_by": cnki_sort_by,
            "wos_query": wos_query,
            "wos_sort_by": wos_sort_by,
        }
        cache_key = make_cache_key(params)
        if use_cache:
            cached = get_cached_search(cache_key, max_age_hours=cache_max_age_hours)
            if cached:
                cached["cache_hit"] = True
                if save_as:
                    save_collection(save_as, _records_from_payload(json.dumps(cached["results"], ensure_ascii=False)))
                    cached["saved_collection"] = save_as
                return json.dumps(cached, ensure_ascii=False, indent=2)

        records: list[PaperRecord] = []
        source_status: list[dict[str, Any]] = []

        if open_sources:
            try:
                result = await asyncio.to_thread(
                    search_deepxiv_sources,
                    query=query,
                    sources=open_sources,
                    limit=limit,
                    categories=[item for item in categories.split(",") if item.strip()] or None,
                    authors=[item for item in authors.split(",") if item.strip()] or None,
                    orgs=[item for item in orgs.split(",") if item.strip()] or None,
                    min_citation=min_citation,
                    date_from=date_from or None,
                    date_to=date_to or None,
                    use_fine_rerank=use_fine_rerank,
                )
                records.extend(result["records"])
                source_status.extend(result["source_status"])
            except Exception as exc:
                source_status.append({"source": "deepxiv", "status": "error", "error": str(exc)})

        if "wos" in auth_sources:
            wos_records, status = await _search_wos(
                query=wos_query or _to_wos_query(query),
                limit=limit,
                sort_by=wos_sort_by,
                ctx=ctx,
            )
            records.extend(wos_records)
            source_status.append(status)

        if "cnki" in auth_sources:
            cnki_records, status = await _search_cnki(
                query=query,
                limit=limit,
                search_type=cnki_search_type,
                sort_by=cnki_sort_by,
                ctx=ctx,
            )
            records.extend(cnki_records)
            source_status.append(status)

        unknown = [source for source in auth_sources if source not in {"wos", "cnki"}]
        for source in unknown:
            source_status.append({"source": source, "status": "skipped", "error": "unknown source"})

        before_dedupe = len(records)
        deduped, duplicate_count = dedupe_records(records)
        ranked = rank_records(deduped, mode=mode)[:limit]
        upsert_papers(ranked)

        payload = {
            "query": query,
            "sources_requested": {"open": open_sources, "authenticated": auth_sources},
            "source_status": source_status,
            "cache_hit": False,
            "total_results_before_dedupe": before_dedupe,
            "duplicate_count": duplicate_count,
            "total_results": len(ranked),
            "results": [record.to_dict() for record in ranked],
        }

        if save_as:
            payload["saved_collection"] = save_collection(save_as, ranked)
        save_search_cache(cache_key, query, params, payload)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    def save_papers_to_collection(name: str, papers_json: str, description: str = "") -> str:
        """Save normalized papers JSON into a named local collection."""
        records = _records_from_payload(papers_json)
        result = save_collection(name, records, description=description)
        return json.dumps(result, ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_paper_collections() -> str:
        """List local paper collections."""
        return json.dumps({"collections": list_collections()}, ensure_ascii=False, indent=2)

    @mcp.tool()
    def get_paper_collection(name: str) -> str:
        """Return one local paper collection with normalized records."""
        return json.dumps(get_collection(name), ensure_ascii=False, indent=2)

    @mcp.tool()
    def export_paper_collection(name: str, format: str = "bibtex") -> str:
        """Export a collection as bibtex, ris, csv, jsonl, json, or md."""
        return json.dumps(export_collection(name, format), ensure_ascii=False, indent=2)

    @mcp.tool()
    async def check_academic_sources(check_authenticated: bool = False, ctx: Context = None) -> str:
        """Check open-source availability and optionally CNKI/WoS browser auth status."""
        status: dict[str, Any] = {"deepxiv": deepxiv_status()}
        if not check_authenticated:
            status["cnki"] = {"registered": bool(getattr(cnki_module, "check_cnki_status_impl", None))}
            status["wos"] = {"registered": bool(getattr(wos_module, "check_wos_status_impl", None))}
            return json.dumps(status, ensure_ascii=False, indent=2)

        cnki_check = getattr(cnki_module, "check_cnki_status_impl", None)
        wos_check = getattr(wos_module, "check_wos_status_impl", None)
        if cnki_check:
            status["cnki"] = _load_json_result(await cnki_check(ctx=ctx))
        if wos_check:
            status["wos"] = _load_json_result(await wos_check(ctx=ctx))
        return json.dumps(status, ensure_ascii=False, indent=2)
