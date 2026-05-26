"""SQLite-backed paper cache, collections, and exporters."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import PaperRecord, clean_text


STATE_DIR = Path.home() / ".academic-mcp"
DEFAULT_DB_PATH = STATE_DIR / "library.sqlite3"
DEFAULT_EXPORT_DIR = STATE_DIR / "exports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def library_db_path() -> Path:
    return Path(os.environ.get("ACADEMIC_LIBRARY_DB", DEFAULT_DB_PATH)).expanduser()


def export_dir() -> Path:
    return Path(os.environ.get("ACADEMIC_EXPORT_DIR", DEFAULT_EXPORT_DIR)).expanduser()


def connect() -> sqlite3.Connection:
    path = library_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_db(conn)
    return conn


def ensure_db(conn: sqlite3.Connection | None = None) -> None:
    should_close = conn is None
    if conn is None:
        path = library_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                authors_json TEXT NOT NULL DEFAULT '[]',
                year TEXT NOT NULL DEFAULT '',
                venue TEXT NOT NULL DEFAULT '',
                abstract TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                source_rank INTEGER NOT NULL DEFAULT 0,
                citation_count INTEGER,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                pdf_path TEXT NOT NULL DEFAULT '',
                auth_required INTEGER NOT NULL DEFAULT 0,
                score REAL,
                raw_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collections (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS collection_papers (
                collection_name TEXT NOT NULL,
                paper_id TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                PRIMARY KEY (collection_name, paper_id),
                FOREIGN KEY(collection_name) REFERENCES collections(name) ON DELETE CASCADE,
                FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                params_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def _paper_values(record: PaperRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.title,
        json.dumps(record.authors, ensure_ascii=False),
        record.year,
        record.venue,
        record.abstract,
        record.doi,
        record.url,
        record.source,
        record.source_rank,
        record.citation_count,
        json.dumps(record.keywords, ensure_ascii=False),
        record.pdf_path,
        1 if record.auth_required else 0,
        record.score,
        json.dumps(record.raw, ensure_ascii=False),
        _now(),
    )


def upsert_papers(records: Iterable[PaperRecord]) -> int:
    records = list(records)
    if not records:
        return 0
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO papers (
                id, title, authors_json, year, venue, abstract, doi, url, source,
                source_rank, citation_count, keywords_json, pdf_path, auth_required,
                score, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                authors_json=excluded.authors_json,
                year=excluded.year,
                venue=excluded.venue,
                abstract=excluded.abstract,
                doi=excluded.doi,
                url=excluded.url,
                source=excluded.source,
                source_rank=excluded.source_rank,
                citation_count=excluded.citation_count,
                keywords_json=excluded.keywords_json,
                pdf_path=excluded.pdf_path,
                auth_required=excluded.auth_required,
                score=excluded.score,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            [_paper_values(record) for record in records],
        )
        conn.commit()
    return len(records)


def save_collection(name: str, records: Iterable[PaperRecord], description: str = "") -> dict[str, Any]:
    name = clean_text(name)
    if not name:
        raise ValueError("collection name cannot be empty")
    records = list(records)
    upsert_papers(records)
    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO collections(name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description=COALESCE(NULLIF(excluded.description, ''), collections.description),
                updated_at=excluded.updated_at
            """,
            (name, description, now, now),
        )
        conn.executemany(
            """
            INSERT INTO collection_papers(collection_name, paper_id, position, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(collection_name, paper_id) DO UPDATE SET
                position=excluded.position
            """,
            [(name, record.id, idx, now) for idx, record in enumerate(records)],
        )
        conn.commit()
    return {"name": name, "paper_count": len(records), "db_path": str(library_db_path())}


def _row_to_paper(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "authors": json.loads(row["authors_json"] or "[]"),
        "year": row["year"],
        "venue": row["venue"],
        "abstract": row["abstract"],
        "doi": row["doi"],
        "url": row["url"],
        "source": row["source"],
        "source_rank": row["source_rank"],
        "citation_count": row["citation_count"],
        "keywords": json.loads(row["keywords_json"] or "[]"),
        "pdf_path": row["pdf_path"],
        "auth_required": bool(row["auth_required"]),
        "score": row["score"],
        "raw": json.loads(row["raw_json"] or "{}"),
    }


def list_collections() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.name, c.description, c.created_at, c.updated_at, COUNT(cp.paper_id) AS paper_count
            FROM collections c
            LEFT JOIN collection_papers cp ON cp.collection_name = c.name
            GROUP BY c.name
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_collection(name: str) -> dict[str, Any]:
    name = clean_text(name)
    with connect() as conn:
        collection = conn.execute("SELECT * FROM collections WHERE name = ?", (name,)).fetchone()
        if not collection:
            raise ValueError(f"collection not found: {name}")
        rows = conn.execute(
            """
            SELECT p.*
            FROM collection_papers cp
            JOIN papers p ON p.id = cp.paper_id
            WHERE cp.collection_name = ?
            ORDER BY cp.position ASC, cp.added_at ASC
            """,
            (name,),
        ).fetchall()
    return {**dict(collection), "papers": [_row_to_paper(row) for row in rows]}


def make_cache_key(params: dict[str, Any]) -> str:
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_cached_search(cache_key: str, max_age_hours: int = 24) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT result_json, created_at FROM search_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    created_at = datetime.fromisoformat(row["created_at"])
    if created_at < datetime.now(timezone.utc) - timedelta(hours=max_age_hours):
        return None
    return json.loads(row["result_json"])


def save_search_cache(cache_key: str, query: str, params: dict[str, Any], result: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO search_cache(cache_key, query, params_json, result_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                result_json=excluded.result_json,
                created_at=excluded.created_at
            """,
            (
                cache_key,
                query,
                json.dumps(params, ensure_ascii=False, sort_keys=True),
                json.dumps(result, ensure_ascii=False),
                _now(),
            ),
        )
        conn.commit()


def _citation_key(paper: dict[str, Any], index: int) -> str:
    first_author = "paper"
    authors = paper.get("authors") or []
    if authors:
        first_author = re.sub(r"\W+", "", str(authors[0]).split()[-1]) or "paper"
    year = paper.get("year") or "nd"
    return f"{first_author}{year}_{index + 1}"


def _export_bibtex(papers: list[dict[str, Any]]) -> str:
    entries: list[str] = []
    for index, paper in enumerate(papers):
        fields = {
            "title": paper.get("title"),
            "author": " and ".join(paper.get("authors") or []),
            "year": paper.get("year"),
            "journal": paper.get("venue"),
            "doi": paper.get("doi"),
            "url": paper.get("url"),
        }
        body = ",\n".join(
            f"  {key} = {{{clean_text(value)}}}" for key, value in fields.items() if clean_text(value)
        )
        entries.append(f"@article{{{_citation_key(paper, index)},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _export_ris(papers: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for paper in papers:
        lines.append("TY  - JOUR")
        if paper.get("title"):
            lines.append(f"TI  - {paper['title']}")
        for author in paper.get("authors") or []:
            lines.append(f"AU  - {author}")
        if paper.get("year"):
            lines.append(f"PY  - {paper['year']}")
        if paper.get("venue"):
            lines.append(f"JO  - {paper['venue']}")
        if paper.get("doi"):
            lines.append(f"DO  - {paper['doi']}")
        if paper.get("url"):
            lines.append(f"UR  - {paper['url']}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def _export_csv(papers: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["id", "title", "authors", "year", "venue", "doi", "url", "source", "citation_count"],
    )
    writer.writeheader()
    for paper in papers:
        writer.writerow(
            {
                "id": paper.get("id"),
                "title": paper.get("title"),
                "authors": "; ".join(paper.get("authors") or []),
                "year": paper.get("year"),
                "venue": paper.get("venue"),
                "doi": paper.get("doi"),
                "url": paper.get("url"),
                "source": paper.get("source"),
                "citation_count": paper.get("citation_count"),
            }
        )
    return output.getvalue()


def _export_markdown(collection: dict[str, Any]) -> str:
    lines = [f"# {collection['name']}", ""]
    if collection.get("description"):
        lines.extend([collection["description"], ""])
    for index, paper in enumerate(collection["papers"], 1):
        authors = ", ".join(paper.get("authors") or [])
        meta = " | ".join(
            item for item in [authors, paper.get("venue"), paper.get("year"), paper.get("source")] if item
        )
        title = paper.get("title") or "Untitled"
        url = paper.get("url")
        lines.append(f"{index}. [{title}]({url})" if url else f"{index}. {title}")
        if meta:
            lines.append(f"   {meta}")
        if paper.get("doi"):
            lines.append(f"   DOI: {paper['doi']}")
        if paper.get("abstract"):
            lines.append(f"   {clean_text(paper['abstract'])[:500]}")
        lines.append("")
    return "\n".join(lines)


def export_collection(name: str, fmt: str = "bibtex") -> dict[str, Any]:
    collection = get_collection(name)
    papers = collection["papers"]
    fmt = (fmt or "bibtex").lower()
    exporters = {
        "bibtex": ("bib", lambda: _export_bibtex(papers)),
        "ris": ("ris", lambda: _export_ris(papers)),
        "csv": ("csv", lambda: _export_csv(papers)),
        "jsonl": ("jsonl", lambda: "".join(json.dumps(paper, ensure_ascii=False) + "\n" for paper in papers)),
        "json": ("json", lambda: json.dumps(collection, ensure_ascii=False, indent=2)),
        "md": ("md", lambda: _export_markdown(collection)),
        "markdown": ("md", lambda: _export_markdown(collection)),
    }
    if fmt not in exporters:
        raise ValueError("format must be bibtex, ris, csv, jsonl, json, or md")
    ext, render = exporters[fmt]
    out_dir = export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "collection"
    path = out_dir / f"{safe_name}.{ext}"
    path.write_text(render(), encoding="utf-8", newline="")
    return {"collection": name, "format": fmt, "paper_count": len(papers), "file_path": str(path)}
