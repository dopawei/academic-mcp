"""CNKI 工具 — 搜索、详情、下载、登录、状态检查"""

import json
import re
from pathlib import Path

from fastmcp import FastMCP, Context

from .browser import BrowserPool, DOWNLOAD_DIR, delay, human_type

# ─── 常量 ─────────────────────────────────────────────────────────────────────

CNKI_BASE = "https://kns.cnki.net"

SEARCH_TYPE_MAP = {
    "主题": "SU", "topic": "SU", "subject": "SU", "su": "SU",
    "篇名": "TI", "title": "TI", "ti": "TI",
    "关键词": "KY", "keyword": "KY", "keywords": "KY", "ky": "KY",
    "作者": "AU", "author": "AU", "au": "AU",
    "第一作者": "FI", "first_author": "FI", "fi": "FI",
    "通讯作者": "RP", "corresponding_author": "RP", "rp": "RP",
    "作者单位": "AF", "affiliation": "AF", "institution": "AF", "af": "AF",
    "基金": "FU", "fund": "FU", "fu": "FU",
    "摘要": "AB", "abstract": "AB", "ab": "AB",
    "全文": "FT", "fulltext": "FT", "ft": "FT",
    "DOI": "DOI", "doi": "DOI",
    "参考文献": "RF", "reference": "RF", "rf": "RF",
}

SEARCH_TYPE_VALUES = {
    "SU": "SU$%=|", "KY": "KY$=|", "TI": "TI$%=|", "AU": "AU$=|",
    "FI": "FI$=|", "RP": "RP$%=|", "AF": "AF$%", "FU": "FU$%|",
    "AB": "AB$%=|", "FT": "FT$%=|", "RF": "RF$%=|", "DOI": "DOI$=|?",
}

SORT_TYPE_MAP = {
    "相关度": "FFD", "relevance": "FFD",
    "发表时间": "PT", "date": "PT", "time": "PT",
    "被引": "CF", "cited": "CF", "citation": "CF",
    "下载": "DFR", "download": "DFR",
}


def _resolve_search_type(raw: str) -> str:
    key = raw.strip().lower()
    return SEARCH_TYPE_MAP.get(key, SEARCH_TYPE_MAP.get(raw.strip(), "SU"))


def _resolve_sort_type(raw: str) -> str:
    key = raw.strip().lower()
    return SORT_TYPE_MAP.get(key, SORT_TYPE_MAP.get(raw.strip(), "FFD"))


def _get_pool(ctx: Context) -> BrowserPool:
    return ctx.request_context.lifespan_context["browser_pool"]


# ─── 工具注册 ─────────────────────────────────────────────────────────────────

def register_cnki_tools(mcp: FastMCP):
    """将 CNKI 工具注册到 MCP 服务器"""

    @mcp.tool()
    async def search_cnki(
        keyword: str,
        search_type: str = "主题",
        page_count: int = 1,
        sort_by: str = "相关度",
        ctx: Context = None,
    ) -> str:
        """
        搜索中国知网 (CNKI) 学术论文。

        Args:
            keyword: 搜索关键词
            search_type: 搜索类型 (主题/篇名/关键词/作者/单位/DOI/摘要/全文)
            page_count: 搜索页数 (1-5，每页约20条)
            sort_by: 排序方式 (相关度/被引/下载/发表时间)

        Returns:
            JSON 格式的搜索结果列表
        """
        pool = _get_pool(ctx)
        page_count = max(1, min(page_count, 5))
        st_code = _resolve_search_type(search_type)
        sort_id = _resolve_sort_type(sort_by)
        page = await pool.new_page()
        all_results = []

        try:
            await page.goto("https://www.cnki.net/", wait_until="domcontentloaded", timeout=30000)
            await delay(1, 2)

            if st_code != "SU":
                value = SEARCH_TYPE_VALUES.get(st_code)
                if value:
                    await page.click("#DBFieldBox")
                    await delay(0.5, 1)
                    await page.click(f'#DBFieldList a[value="{value}"]')
                    await delay(0.3, 0.5)

            await human_type(page, "#txt_SearchText", keyword)
            await page.click(".search-btn")
            await delay(2, 3)

            try:
                await page.wait_for_selector('table.result-table-list tbody tr', timeout=15000)
            except Exception:
                content = await page.content()
                if "验证" in content or "captcha" in content.lower():
                    return json.dumps({"error": "CNKI 触发了验证码，请运行 login_cnki 手动完成验证"}, ensure_ascii=False)
                return json.dumps({"error": "搜索结果加载超时，请稍后重试"}, ensure_ascii=False)

            if sort_id != "FFD":
                try:
                    await page.click(f"#{sort_id}")
                    await delay(1.5, 2.5)
                    await page.wait_for_selector('table.result-table-list tbody tr', timeout=15000)
                except Exception:
                    pass

            for pg in range(page_count):
                if pg > 0:
                    try:
                        next_btn = page.locator("#PageNext")
                        if await next_btn.count() > 0 and await next_btn.is_enabled():
                            await next_btn.click()
                            await delay(1.5, 2.5)
                            await page.wait_for_selector('table.result-table-list tbody tr', timeout=15000)
                        else:
                            break
                    except Exception:
                        break

                rows = page.locator("table.result-table-list tbody tr")
                count = await rows.count()

                for i in range(count):
                    try:
                        row = rows.nth(i)
                        paper = {}

                        title_a = row.locator('a.fz14')
                        if await title_a.count() > 0:
                            paper["title"] = (await title_a.inner_text()).strip()
                            href = await title_a.get_attribute("href") or ""
                            paper["url"] = href if href.startswith("http") else f"https:{href}" if href.startswith("//") else f"{CNKI_BASE}{href}"
                        else:
                            continue

                        authors_el = row.locator('td.author a')
                        if await authors_el.count() > 0:
                            names = []
                            for j in range(await authors_el.count()):
                                n = (await authors_el.nth(j).inner_text()).strip()
                                if n:
                                    names.append(n)
                            paper["authors"] = names
                        else:
                            paper["authors"] = []

                        src_el = row.locator('td.source a')
                        paper["source"] = (await src_el.first.inner_text()).strip() if await src_el.count() > 0 else ""

                        date_el = row.locator('td.date')
                        paper["date"] = (await date_el.inner_text()).strip() if await date_el.count() > 0 else ""

                        cite_el = row.locator('td.quote a')
                        paper["cited"] = (await cite_el.inner_text()).strip() if await cite_el.count() > 0 else "0"

                        dl_el = row.locator('td.download a')
                        paper["downloads"] = (await dl_el.inner_text()).strip() if await dl_el.count() > 0 else "0"

                        paper["page"] = pg + 1
                        all_results.append(paper)
                    except Exception:
                        continue

                if ctx:
                    await ctx.report_progress(pg + 1, page_count)

            return json.dumps({
                "keyword": keyword, "search_type": search_type, "sort_by": sort_by,
                "total_results": len(all_results), "results": all_results,
            }, ensure_ascii=False, indent=2)

        finally:
            await page.close()

    globals()["search_cnki_impl"] = search_cnki

    @mcp.tool()
    async def get_paper_detail(url: str, ctx: Context = None) -> str:
        """
        获取知网论文的详细信息。

        Args:
            url: 论文详情页 URL（来自 search_cnki 的结果）

        Returns:
            JSON 格式的论文详细信息（标题/作者/摘要/关键词/DOI 等）
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await delay(2, 3)

            try:
                await page.wait_for_selector(".wx-tit", timeout=15000)
            except Exception:
                return json.dumps({"error": "论文页面加载超时", "url": url}, ensure_ascii=False)

            detail = {"url": url}

            for sel in ['div.wx-tit h1', 'h1']:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["title"] = (await el.first.inner_text()).strip()
                    break

            el = page.locator('div.wx-tit h2')
            if await el.count() > 0:
                detail["title_en"] = (await el.first.inner_text()).strip()

            author_els = page.locator('h3.author span a')
            if await author_els.count() > 0:
                detail["authors"] = [
                    (await author_els.nth(i).inner_text()).strip()
                    for i in range(await author_els.count())
                    if (await author_els.nth(i).inner_text()).strip()
                ]

            org_els = page.locator('h3.orgn span a')
            if await org_els.count() > 0:
                detail["institutions"] = [
                    (await org_els.nth(i).inner_text()).strip()
                    for i in range(await org_els.count())
                    if (await org_els.nth(i).inner_text()).strip()
                ]

            for sel_id, key in [("ChDivSummary", "abstract"), ("EnChDivSummary", "abstract_en")]:
                el = page.locator(f"#{sel_id}")
                if await el.count() > 0:
                    detail[key] = (await el.first.inner_text()).strip()

            kw_els = page.locator('p.keywords a')
            if await kw_els.count() > 0:
                detail["keywords"] = [
                    (await kw_els.nth(i).inner_text()).strip().rstrip(";；")
                    for i in range(await kw_els.count())
                    if (await kw_els.nth(i).inner_text()).strip().rstrip(";；")
                ]

            src_el = page.locator('div.top-tip a[href*="navi.cnki.net"]')
            if await src_el.count() > 0:
                detail["journal"] = (await src_el.first.inner_text()).strip().rstrip(" .")

            span_el = page.locator('div.top-tip span')
            if await span_el.count() > 0:
                for i in range(await span_el.count()):
                    text = (await span_el.nth(i).inner_text()).strip()
                    if re.search(r'\d{4}', text):
                        detail["publication_info"] = text
                        break

            doi_el = page.locator('li.top-space:has-text("DOI") p')
            if await doi_el.count() > 0:
                detail["doi"] = (await doi_el.first.inner_text()).strip()

            cite_el = page.locator('#refs a')
            if await cite_el.count() > 0:
                detail["cited_count"] = (await cite_el.first.inner_text()).strip()

            dl_el = page.locator('#DownLoadParts a')
            if await dl_el.count() > 0:
                detail["download_count"] = (await dl_el.first.inner_text()).strip()

            fund_el = page.locator('li:has-text("基金") p')
            if await fund_el.count() > 0:
                detail["fund"] = (await fund_el.first.inner_text()).strip()

            cls_el = page.locator('li:has-text("分类号") p')
            if await cls_el.count() > 0:
                detail["classification"] = (await cls_el.first.inner_text()).strip()

            return json.dumps(detail, ensure_ascii=False, indent=2)

        finally:
            await page.close()

    @mcp.tool()
    async def download_paper(url: str, save_dir: str = "", ctx: Context = None) -> str:
        """
        下载知网论文 PDF/CAJ。需要已登录（机构 IP 认证或账号登录）。

        Args:
            url: 论文详情页 URL
            save_dir: 保存目录，默认 ~/.academic-mcp/downloads/

        Returns:
            下载结果（文件路径或错误信息）
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()
        save_path = Path(save_dir) if save_dir else DOWNLOAD_DIR
        save_path.mkdir(parents=True, exist_ok=True)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await delay(2, 3)

            btn = None
            ftype = ""
            for selector, ft in [
                ("#pdfDown", "pdf"), ("a.btn-dlpdf", "pdf"), ("a[id*='pdf' i]", "pdf"),
                ("#cajDown", "caj"), ("a.btn-dlcaj", "caj"),
            ]:
                el = page.locator(selector)
                if await el.count() > 0:
                    btn = el.first
                    ftype = ft
                    break

            if not btn:
                return json.dumps({
                    "error": "未找到下载按钮，可能需要登录",
                    "tip": "请运行 login_cnki 工具完成登录后重试", "url": url,
                }, ensure_ascii=False)

            async with page.expect_download(timeout=60000) as dl_info:
                await btn.click()

            download = await dl_info.value
            filename = download.suggested_filename or f"paper.{ftype}"
            file_path = save_path / filename
            await download.save_as(str(file_path))

            return json.dumps({
                "success": True, "file_path": str(file_path),
                "file_name": filename, "file_type": ftype,
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "error": f"下载失败: {e}",
                "tip": "可能需要登录知网，或论文不支持下载", "url": url,
            }, ensure_ascii=False)

        finally:
            await page.close()

    @mcp.tool()
    async def login_cnki(ctx: Context = None) -> str:
        """
        打开知网登录页面进行手动登录。
        首次使用时调用一次，之后登录态会自动持久化。
        会打开一个可见的浏览器窗口。

        Returns:
            登录状态信息
        """
        pool = _get_pool(ctx)
        await pool.restart(headless=False)
        page = await pool.new_page()

        try:
            await page.goto(f"{CNKI_BASE}/", wait_until="domcontentloaded", timeout=30000)
            await delay(2, 3)

            user_el = page.locator(".userName, .user-name")
            if await user_el.count() > 0:
                name = (await user_el.first.inner_text()).strip()
                if name and "登录" not in name and "注册" not in name:
                    return json.dumps({
                        "status": "already_logged_in", "user": name,
                        "message": "已登录知网，可以正常使用",
                    }, ensure_ascii=False)

            await page.goto("https://login.cnki.net/login/", wait_until="domcontentloaded", timeout=30000)

            try:
                await page.wait_for_url(
                    lambda u: "cnki.net" in u and "login" not in u, timeout=300000,
                )
                return json.dumps({
                    "status": "login_success",
                    "message": "登录成功！登录态已保存，后续使用将自动复用。",
                }, ensure_ascii=False)
            except Exception:
                return json.dumps({
                    "status": "waiting_for_login",
                    "message": "浏览器已打开登录页。请手动完成登录，登录态会自动保存。",
                    "tip": "如果你的学校使用 IP 认证，可能无需登录即可使用。",
                }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": f"打开登录页失败: {e}"}, ensure_ascii=False)

        finally:
            await page.close()
            await pool.restart(headless=True)

    globals()["login_cnki_impl"] = login_cnki

    @mcp.tool()
    async def check_cnki_status(ctx: Context = None) -> str:
        """
        检查知网连接状态和登录状态。

        Returns:
            当前连接状态、登录状态、IP 认证信息
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()

        try:
            await page.goto(f"{CNKI_BASE}/", wait_until="domcontentloaded", timeout=30000)
            await delay(1, 2)

            status = {
                "cnki_accessible": True,
                "browser_data_dir": str(DOWNLOAD_DIR.parent / "browser-data"),
                "download_dir": str(DOWNLOAD_DIR),
            }

            user_el = page.locator(".userName, .user-name")
            if await user_el.count() > 0:
                name = (await user_el.first.inner_text()).strip()
                if name and "登录" not in name:
                    status["logged_in"] = True
                    status["user"] = name
                else:
                    status["logged_in"] = False
            else:
                status["logged_in"] = False

            ip_el = page.locator(".ip-name, .org-name, .Ename")
            if await ip_el.count() > 0:
                org = (await ip_el.first.inner_text()).strip()
                if org:
                    status["ip_auth"] = True
                    status["institution"] = org

            return json.dumps(status, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"cnki_accessible": False, "error": str(e)}, ensure_ascii=False)

        finally:
            await page.close()

    # ── 资源 ──

    globals()["check_cnki_status_impl"] = check_cnki_status

    @mcp.resource("cnki://search-types")
    async def get_search_types() -> str:
        """可用的搜索类型"""
        return json.dumps({
            "主题(SU)": "按主题搜索，覆盖标题、关键词、摘要",
            "篇名(TI)": "按论文标题搜索",
            "关键词(KY)": "按关键词搜索",
            "作者(AU)": "按作者姓名搜索",
            "第一作者(FI)": "按第一作者搜索",
            "作者单位(AF)": "按单位搜索",
            "摘要(AB)": "按摘要内容搜索",
            "全文(FT)": "全文搜索",
            "DOI": "按 DOI 搜索",
        }, ensure_ascii=False, indent=2)
