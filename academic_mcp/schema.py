"""Shared paper schema, normalization, and deduplication helpers."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


_SPACE_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_ARXIV_RE = re.compile(r"(?i)(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return _SPACE_RE.sub(" ", text)


def normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", clean_text(title)).casefold()
    text = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", text)
    return _SPACE_RE.sub(" ", text).strip()


def parse_year(*values: Any) -> str:
    for value in values:
        match = _YEAR_RE.search(clean_text(value))
        if match:
            return match.group(0)
    return ""


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"\d+", str(value).replace(",", ""))
    return int(match.group(0)) if match else None


def split_authors(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = clean_text(item.get("name") or item.get("author") or item)
            else:
                name = clean_text(item)
            if name:
                names.append(name)
        return names
    text = clean_text(value)
    if not text:
        return []
    parts = re.split(r";|；|\band\b|, (?=[A-Z][a-z])", text)
    return [clean_text(part) for part in parts if clean_text(part)]


def normalize_doi(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"(?i)^doi:\s*", "", text)
    return text.strip().lower()


def extract_arxiv_id(*values: Any) -> str:
    for value in values:
        match = _ARXIV_RE.search(clean_text(value))
        if match:
            return match.group(1)
    return ""


def stable_hash(*values: Any, length: int = 16) -> str:
    payload = "\n".join(clean_text(value) for value in values if clean_text(value))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


@dataclass
class PaperRecord:
    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    abstract: str = ""
    doi: str = ""
    url: str = ""
    source: str = ""
    source_rank: int = 0
    citation_count: int | None = None
    keywords: list[str] = field(default_factory=list)
    pdf_path: str = ""
    auth_required: bool = False
    score: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_record_id(
    source: str,
    title: str = "",
    doi: str = "",
    url: str = "",
    external_id: str = "",
) -> str:
    doi = normalize_doi(doi)
    if doi:
        return f"doi:{doi}"
    arxiv_id = extract_arxiv_id(external_id, url)
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if external_id:
        return f"{source}:{clean_text(external_id)}"
    return f"{source}:title:{stable_hash(title, url)}"


def dedupe_key(record: PaperRecord) -> str:
    doi = normalize_doi(record.doi)
    if doi:
        return f"doi:{doi}"
    arxiv_id = extract_arxiv_id(record.id, record.url)
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower()}"
    normalized = normalize_title(record.title)
    if normalized:
        return f"title:{normalized}:{record.year}"
    return record.id


def _prefer_text(primary: str, candidate: str) -> str:
    primary = clean_text(primary)
    candidate = clean_text(candidate)
    if not primary:
        return candidate
    if candidate and len(candidate) > len(primary):
        return candidate
    return primary


def merge_records(primary: PaperRecord, candidate: PaperRecord) -> PaperRecord:
    primary.authors = primary.authors or candidate.authors
    primary.year = primary.year or candidate.year
    primary.venue = primary.venue or candidate.venue
    primary.abstract = _prefer_text(primary.abstract, candidate.abstract)
    primary.doi = primary.doi or candidate.doi
    primary.url = primary.url or candidate.url
    primary.citation_count = (
        primary.citation_count
        if primary.citation_count is not None
        else candidate.citation_count
    )
    primary.keywords = list(dict.fromkeys([*primary.keywords, *candidate.keywords]))
    sources = primary.raw.setdefault("matched_sources", [])
    for source in [primary.source, candidate.source]:
        if source and source not in sources:
            sources.append(source)
    primary.raw.setdefault("merged_records", []).append(candidate.to_dict())
    return primary


def dedupe_records(records: Iterable[PaperRecord]) -> tuple[list[PaperRecord], int]:
    grouped: dict[str, PaperRecord] = {}
    duplicates = 0
    for record in records:
        key = dedupe_key(record)
        if key in grouped:
            duplicates += 1
            grouped[key] = merge_records(grouped[key], record)
        else:
            grouped[key] = record
    return list(grouped.values()), duplicates


def rank_records(records: list[PaperRecord], mode: str = "balanced") -> list[PaperRecord]:
    mode = (mode or "balanced").lower()

    def rank_score(record: PaperRecord) -> float:
        source = record.source.lower()
        source_weight = 0.0
        if source.startswith("wos"):
            source_weight = 0.25
        elif source.startswith("deepxiv"):
            source_weight = 0.2
        elif source.startswith("cnki"):
            source_weight = 0.12

        rank_component = 1 / (record.source_rank + 1)
        citation_component = math.log1p(record.citation_count or 0) / 10
        year_component = 0.0
        if record.year.isdigit():
            year_component = max(0, min((int(record.year) - 2000) / 100, 0.3))

        if mode == "authority":
            return citation_component * 1.8 + source_weight + rank_component * 0.2
        if mode == "recency":
            return year_component * 2 + rank_component * 0.3 + source_weight
        return rank_component + source_weight + citation_component + year_component

    return sorted(records, key=rank_score, reverse=True)
