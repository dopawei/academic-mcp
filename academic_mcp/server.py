"""Academic MCP Server — 组合 CNKI + WoS 工具的统一入口"""

import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from .browser import BrowserPool, DOWNLOAD_DIR
from .cnki import register_cnki_tools
from .deepxiv import register_deepxiv_tools
from .unified import register_unified_tools
from .wos import register_wos_tools


@asynccontextmanager
async def lifespan(server):
    headless = os.environ.get("ACADEMIC_HEADLESS", "true").lower() == "true"
    pool = BrowserPool(headless=headless)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        yield {"browser_pool": pool}
    finally:
        await pool.cleanup()


mcp = FastMCP("academic-mcp", lifespan=lifespan)

# 注册所有工具到同一个 MCP 实例
register_cnki_tools(mcp)
register_wos_tools(mcp)
register_deepxiv_tools(mcp)
register_unified_tools(mcp)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
