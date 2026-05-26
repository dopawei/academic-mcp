import academic_mcp.server  # noqa: F401 - importing registers MCP tool implementations
from academic_mcp import cnki, wos


def test_cnki_tool_impls_are_exported_for_local_reuse():
    assert cnki.get_paper_detail_impl is not None
    assert cnki.download_paper_impl is not None


def test_wos_tool_impls_are_exported_for_local_reuse():
    assert wos.get_wos_detail_impl is not None
    assert wos.export_wos_impl is not None
