import json

from academic_mcp.library import export_collection, get_collection, list_collections, save_collection
from academic_mcp.schema import PaperRecord


def test_collection_roundtrip_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("ACADEMIC_LIBRARY_DB", str(tmp_path / "library.sqlite3"))
    monkeypatch.setenv("ACADEMIC_EXPORT_DIR", str(tmp_path / "exports"))

    record = PaperRecord(
        id="doi:10.1/test",
        title="Signal Paper",
        authors=["Alice", "Bob"],
        year="2025",
        venue="Journal",
        doi="10.1/test",
        url="https://doi.org/10.1/test",
        source="wos",
        citation_count=5,
    )

    saved = save_collection("signals", [record], description="test collection")
    assert saved["paper_count"] == 1

    collections = list_collections()
    assert collections[0]["name"] == "signals"
    assert collections[0]["paper_count"] == 1

    collection = get_collection("signals")
    assert collection["papers"][0]["title"] == "Signal Paper"

    bib = export_collection("signals", "bibtex")
    assert bib["paper_count"] == 1
    text = (tmp_path / "exports" / "signals.bib").read_text(encoding="utf-8")
    assert "@article" in text
    assert "Signal Paper" in text

    json_export = export_collection("signals", "jsonl")
    first_line = (tmp_path / "exports" / "signals.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["id"] == "doi:10.1/test"
    assert json_export["format"] == "jsonl"
