from academic_mcp.schema import (
    PaperRecord,
    build_record_id,
    dedupe_records,
    normalize_title,
    parse_int,
    parse_year,
    rank_records,
    split_authors,
)


def test_parse_helpers():
    assert parse_year("Published in 2024") == "2024"
    assert parse_int("Cited by 1,234") == 1234
    assert split_authors("Alice; Bob；Chen") == ["Alice", "Bob", "Chen"]
    assert normalize_title(" A  Study: of Signals! ") == "a study of signals"


def test_record_id_prefers_doi_then_arxiv():
    assert build_record_id("wos", title="x", doi="https://doi.org/10.1/ABC") == "doi:10.1/abc"
    assert build_record_id("deepxiv", url="https://arxiv.org/abs/2409.05591") == "arxiv:2409.05591"


def test_dedupe_merges_records_by_doi():
    first = PaperRecord(
        id="doi:10.1/test",
        title="Paper",
        source="wos",
        doi="10.1/test",
        citation_count=10,
    )
    second = PaperRecord(
        id="cnki:title:paper",
        title="Paper",
        source="cnki",
        doi="10.1/test",
        abstract="Long abstract",
        authors=["A"],
    )
    records, duplicate_count = dedupe_records([first, second])
    assert duplicate_count == 1
    assert len(records) == 1
    assert records[0].authors == ["A"]
    assert records[0].abstract == "Long abstract"
    assert "cnki" in records[0].raw["matched_sources"]


def test_rank_records_supports_modes():
    old = PaperRecord(id="a", title="Old", source="deepxiv:arxiv", source_rank=0, year="2018")
    cited = PaperRecord(id="b", title="Cited", source="wos", source_rank=4, citation_count=200, year="2021")
    assert rank_records([old, cited], mode="authority")[0].id == "b"
    assert rank_records([old, cited], mode="balanced")
