"""Web of Science 工具 — 高级检索、详情、导出、登录、状态检查

通过大学反向代理访问 WoS，复用持久化浏览器登录态。
选择器使用分层发现模式 (find_element) 应对 WoS SPA 的 DOM 变化。
"""

import json
import os
from pathlib import Path

from fastmcp import FastMCP, Context

from .browser import BrowserPool, DOWNLOAD_DIR, delay, human_type, find_element

SCREENSHOT_DIR = Path.home() / ".academic-mcp" / "screenshots"

# ─── 常量 ─────────────────────────────────────────────────────────────────────

WOS_BASE = os.environ.get(
    "WOS_BASE_URL",
    "https://webofscience-clarivate-cn.accproxy.lib.szu.edu.cn",
)
WOS_ADVANCED_SEARCH = f"{WOS_BASE}/wos/woscc/advanced-search"

# WoS 排序映射
WOS_SORT_MAP = {
    "relevance": "relevance", "相关度": "relevance",
    "date_newest": "date-descending", "最新": "date-descending", "date": "date-descending",
    "date_oldest": "date-ascending", "最早": "date-ascending",
    "cited": "times-cited-descending", "被引": "times-cited-descending",
    "usage": "usage-count-since-2013-descending", "使用量": "usage-count-since-2013-descending",
}

WOS_DOWNLOAD_DIR = DOWNLOAD_DIR / "wos"


def _get_pool(ctx: Context) -> BrowserPool:
    return ctx.request_context.lifespan_context["browser_pool"]


def _resolve_wos_sort(raw: str) -> str:
    return WOS_SORT_MAP.get(raw.strip().lower(), WOS_SORT_MAP.get(raw.strip(), "relevance"))


async def _save_debug_screenshot(page, name: str) -> str:
    """截图保存到 ~/.academic-mcp/screenshots/ 并返回路径"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"{name}_{ts}.png"
    await page.screenshot(path=str(path), full_page=True)
    return str(path)


async def _dismiss_cookie_banner(page):
    """关闭 OneTrust cookie 同意弹窗（WoS 全站通用）。

    OneTrust SDK 结构:
      #onetrust-consent-sdk        — 最外层容器
        #onetrust-banner-sdk       — 横幅本体
          #onetrust-accept-btn-handler — "Accept All Cookies" 按钮
    弹窗加载有延迟，最多等 5 秒；没弹窗则静默跳过。
    """
    try:
        btn = page.locator("#onetrust-accept-btn-handler")
        await btn.wait_for(state="visible", timeout=5000)
        await btn.click()
        # 等横幅消失，防止后续点击被遮挡
        await page.locator("#onetrust-banner-sdk").wait_for(state="hidden", timeout=3000)
        await delay(0.3, 0.5)
    except Exception:
        pass  # 无弹窗或已关闭，正常继续


# ─── 工具注册 ─────────────────────────────────────────────────────────────────

def register_wos_tools(mcp: FastMCP):
    """将 WoS 工具注册到 MCP 服务器"""

    @mcp.tool()
    async def search_wos(
        query: str,
        sort_by: str = "relevance",
        page_count: int = 1,
        ctx: Context = None,
    ) -> str:
        """
        在 Web of Science 中进行高级检索。

        使用 WoS 字段标签语法，例如:
          TS=(machine learning AND finance)
          AU=(Zhang Wei) AND PY=(2020-2025)
          TI=(neural network) AND SO=(Nature)

        常用字段标签:
          TS=主题, TI=标题, AU=作者, SO=期刊, DO=DOI,
          PY=年份, AB=摘要, AK=作者关键词, OG=机构, FU=基金

        Args:
            query: WoS 高级检索查询语句
            sort_by: 排序 (relevance/date_newest/date_oldest/cited/usage)
            page_count: 页数 (1-5，每页约10-50条)

        Returns:
            JSON 格式的搜索结果列表
        """
        pool = _get_pool(ctx)
        page_count = max(1, min(page_count, 5))
        page = await pool.new_page()
        all_results = []

        try:
            await page.goto(WOS_ADVANCED_SEARCH, wait_until="domcontentloaded", timeout=30000)
            # 等待 Angular SPA 完全加载
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # networkidle 可能超时，继续尝试
            await delay(2, 4)

            # 检查是否被重定向到登录页
            current_url = page.url
            if "login" in current_url.lower() or "auth" in current_url.lower():
                return json.dumps({
                    "error": "需要先登录深大代理，请运行 login_wos",
                    "redirect_url": current_url,
                }, ensure_ascii=False)

            # 尝试关闭 cookie 弹窗
            await _dismiss_cookie_banner(page)

            # 找到搜索输入框 — 不使用过于宽泛的 "textarea" 兜底
            search_input = None
            try:
                search_input = await find_element(page, [
                    "#advancedSearchInputArea",
                    "textarea[data-ta='advanced-search-input']",
                    "textarea.advanced-search-input",
                    "mat-form-field textarea",
                ], "WoS 高级搜索输入框", timeout=15000)
            except Exception:
                # 所有已知选择器失败 — 截图用于调试
                screenshot_path = await _save_debug_screenshot(page, "search_input_not_found")
                return json.dumps({
                    "error": "找不到 WoS 高级搜索输入框，WoS 页面 DOM 可能已更新",
                    "current_url": page.url,
                    "screenshot": screenshot_path,
                    "tip": "请运行 debug_wos 工具查看当前页面状态，然后更新选择器",
                }, ensure_ascii=False)

            # 清空并输入查询
            await search_input.click()
            await search_input.fill("")
            await delay(0.3, 0.5)
            await search_input.fill(query)
            await delay(0.5, 1)

            # 点击搜索按钮 — 不使用过于宽泛的兜底
            try:
                search_btn = await find_element(page, [
                    "button[data-ta='run-search']",
                    "button.search-button",
                    "#search-button",
                    "button:has-text('Search')",
                    "button:has-text('检索')",
                ], "WoS 搜索按钮", timeout=10000)
            except Exception:
                screenshot_path = await _save_debug_screenshot(page, "search_btn_not_found")
                return json.dumps({
                    "error": "找不到 WoS 搜索按钮，页面 DOM 可能已更新",
                    "screenshot": screenshot_path,
                    "tip": "请运行 debug_wos 工具查看当前页面状态",
                }, ensure_ascii=False)

            await search_btn.click()
            await delay(3, 5)

            # 等待结果加载
            try:
                await find_element(page, [
                    "app-records",
                    "[data-ta='search-results-list']",
                    ".search-results-list",
                    "app-record",
                    "#snSearchResults",
                ], "WoS 搜索结果", timeout=20000)
            except Exception:
                content = await page.content()
                if "no records" in content.lower() or "0 results" in content.lower() or "没有找到" in content:
                    return json.dumps({
                        "query": query, "total_results": 0, "results": [],
                        "message": "未找到匹配结果",
                    }, ensure_ascii=False)
                return json.dumps({"error": "搜索结果加载超时，请检查查询语法"}, ensure_ascii=False)

            # 逐页解析
            for pg in range(page_count):
                if pg > 0:
                    try:
                        next_btn = await find_element(page, [
                            "button[data-ta='next-page']",
                            "button:has-text('Next')",
                            "button:has-text('下一页')",
                            ".pagination-next button",
                            "button.mat-paginator-navigation-next",
                        ], "下一页按钮", timeout=5000)
                        await next_btn.click()
                        await delay(2, 4)
                    except Exception:
                        break

                # 解析结果卡片
                records = page.locator("app-record, [data-ta='search-results-item'], .search-results-item")
                count = await records.count()

                if count == 0:
                    # 备选: 尝试表格行
                    records = page.locator("tr.search-results-item, .records-list-item")
                    count = await records.count()

                for i in range(count):
                    try:
                        record = records.nth(i)
                        paper = {}

                        # 标题
                        for sel in ["a[data-ta='title']", "a.title", "h3 a", ".title a", "a.smallV110"]:
                            title_el = record.locator(sel)
                            if await title_el.count() > 0:
                                paper["title"] = (await title_el.first.inner_text()).strip()
                                href = await title_el.first.get_attribute("href") or ""
                                if href:
                                    paper["url"] = href if href.startswith("http") else f"{WOS_BASE}{href}"
                                break

                        if "title" not in paper:
                            continue

                        # 作者
                        for sel in [
                            "[data-ta='author']", ".author", ".search-results-author",
                            "span.author", ".cdx-grid-author",
                        ]:
                            auth_el = record.locator(sel)
                            if await auth_el.count() > 0:
                                text = (await auth_el.first.inner_text()).strip()
                                if text:
                                    paper["authors"] = [a.strip() for a in text.split(";") if a.strip()]
                                break
                        if "authors" not in paper:
                            paper["authors"] = []

                        # 来源/期刊
                        for sel in [
                            "[data-ta='source']", ".source", ".search-results-source",
                            "span.source", ".cdx-grid-source",
                        ]:
                            src_el = record.locator(sel)
                            if await src_el.count() > 0:
                                paper["source"] = (await src_el.first.inner_text()).strip()
                                break

                        # 年份
                        for sel in ["[data-ta='pub-year']", ".pub-year", ".search-results-date"]:
                            year_el = record.locator(sel)
                            if await year_el.count() > 0:
                                paper["year"] = (await year_el.first.inner_text()).strip()
                                break

                        # 被引次数
                        for sel in [
                            "[data-ta='times-cited']", ".times-cited", ".citation-count",
                            "a[title*='Times Cited']", "a[title*='被引']",
                        ]:
                            cited_el = record.locator(sel)
                            if await cited_el.count() > 0:
                                paper["cited"] = (await cited_el.first.inner_text()).strip()
                                break

                        paper["page"] = pg + 1
                        all_results.append(paper)
                    except Exception:
                        continue

                if ctx:
                    await ctx.report_progress(pg + 1, page_count)

            return json.dumps({
                "query": query, "sort_by": sort_by,
                "total_results": len(all_results), "results": all_results,
            }, ensure_ascii=False, indent=2)

        finally:
            await page.close()

    globals()["search_wos_impl"] = search_wos

    @mcp.tool()
    async def get_wos_detail(url: str, ctx: Context = None) -> str:
        """
        获取 Web of Science 论文的完整记录。

        Args:
            url: WoS 论文详情页 URL（来自 search_wos 的结果）

        Returns:
            JSON 格式的论文完整记录（标题/作者/摘要/关键词/DOI/被引/基金等）
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await delay(3, 5)
            await _dismiss_cookie_banner(page)

            detail = {"url": url}

            # 等待页面加载
            try:
                await find_element(page, [
                    "app-full-record", "#FullRTa-fullRecordtitle",
                    ".title", "h2.title",
                ], "WoS 论文详情页", timeout=15000)
            except Exception:
                return json.dumps({"error": "论文详情页加载超时", "url": url}, ensure_ascii=False)

            # 标题
            for sel in [
                "#FullRTa-fullRecordtitle", "h2.title", ".title--full-record",
                "h2[data-ta='title']", ".full-record-title",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["title"] = (await el.first.inner_text()).strip()
                    break

            # 作者
            for sel in [
                "[data-ta='full-record-author']", ".author-list a",
                "span.author", "#FullRTa-authorNames a",
            ]:
                els = page.locator(sel)
                if await els.count() > 0:
                    authors = []
                    for i in range(await els.count()):
                        name = (await els.nth(i).inner_text()).strip().rstrip(",;")
                        if name:
                            authors.append(name)
                    if authors:
                        detail["authors"] = authors
                        break

            # 摘要
            for sel in [
                "#FullRTa-abstract-basic p", ".abstract-text", "[data-ta='abstract']",
                "#abstract p", ".abstract--full-record",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["abstract"] = (await el.first.inner_text()).strip()
                    break

            # 作者关键词
            for sel in [
                "#FullRTa-keywords a", "[data-ta='keyword']",
                ".keywords a", ".author-keywords a",
            ]:
                els = page.locator(sel)
                if await els.count() > 0:
                    kws = []
                    for i in range(await els.count()):
                        kw = (await els.nth(i).inner_text()).strip().rstrip(";；")
                        if kw:
                            kws.append(kw)
                    if kws:
                        detail["keywords"] = kws
                        break

            # Keywords Plus
            for sel in [
                "#FullRTa-keywordsPlus a", ".keywords-plus a",
            ]:
                els = page.locator(sel)
                if await els.count() > 0:
                    kws = []
                    for i in range(await els.count()):
                        kw = (await els.nth(i).inner_text()).strip().rstrip(";；")
                        if kw:
                            kws.append(kw)
                    if kws:
                        detail["keywords_plus"] = kws
                        break

            # 期刊/来源
            for sel in [
                "#FullRTa-sourceTitle", "[data-ta='source-title']",
                ".source-title", ".journal-name",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["journal"] = (await el.first.inner_text()).strip()
                    break

            # DOI
            for sel in [
                "#FullRTa-DOI", "[data-ta='doi']", ".doi",
                "a[href*='doi.org']",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    text = (await el.first.inner_text()).strip()
                    # 从文本或链接中提取 DOI
                    if text.startswith("10."):
                        detail["doi"] = text
                    else:
                        href = await el.first.get_attribute("href") or ""
                        if "doi.org/" in href:
                            detail["doi"] = href.split("doi.org/")[-1]
                        elif text:
                            detail["doi"] = text
                    break

            # 被引次数
            for sel in [
                "[data-ta='times-cited-count']", ".times-cited",
                "#FullRTa-timesCited", "a[title*='Times Cited']",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["cited_count"] = (await el.first.inner_text()).strip()
                    break

            # 参考文献数
            for sel in [
                "[data-ta='cited-ref-count']", ".cited-references",
                "#FullRTa-citedRefCount",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["references_count"] = (await el.first.inner_text()).strip()
                    break

            # 作者地址/单位
            for sel in [
                "#FullRTa-addresses", "[data-ta='addresses']",
                ".addresses", ".author-affiliation",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["addresses"] = (await el.first.inner_text()).strip()
                    break

            # 基金信息
            for sel in [
                "#FullRTa-fundingText", "[data-ta='funding']",
                ".funding-text", ".grant-info",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["funding"] = (await el.first.inner_text()).strip()
                    break

            # 卷/期/页
            for sel in [
                "#FullRTa-pubInfoText", ".pub-info",
                "[data-ta='publication-info']",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["publication_info"] = (await el.first.inner_text()).strip()
                    break

            # 研究领域
            for sel in [
                "#FullRTa-researchArea", "[data-ta='research-areas']",
                ".research-areas",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["research_areas"] = (await el.first.inner_text()).strip()
                    break

            # WoS 分类
            for sel in [
                "#FullRTa-categories", "[data-ta='categories']",
                ".wos-categories",
            ]:
                el = page.locator(sel)
                if await el.count() > 0:
                    detail["wos_categories"] = (await el.first.inner_text()).strip()
                    break

            return json.dumps(detail, ensure_ascii=False, indent=2)

        finally:
            await page.close()

    @mcp.tool()
    async def export_wos(
        format: str = "bibtex",
        count: int = 50,
        ctx: Context = None,
    ) -> str:
        """
        导出当前 WoS 搜索结果的引文数据。需要先运行 search_wos。

        Args:
            format: 导出格式 (bibtex/ris/csv/excel/plaintext)
            count: 导出记录数 (1-1000，默认50)

        Returns:
            下载结果（文件路径或错误信息）
        """
        pool = _get_pool(ctx)
        WOS_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # 复用当前已有页面（搜索结果页）
        ctx_browser = await pool.get_context()
        pages = ctx_browser.pages
        result_page = None
        for p in pages:
            if "wos" in p.url.lower() and "search" not in p.url.lower():
                continue
            if "wos" in p.url.lower():
                result_page = p
                break

        if not result_page:
            return json.dumps({
                "error": "未找到 WoS 搜索结果页面，请先运行 search_wos",
            }, ensure_ascii=False)

        try:
            # 点击导出按钮
            export_btn = await find_element(result_page, [
                "button[data-ta='export-menu']",
                "button:has-text('Export')",
                "button:has-text('导出')",
                "button.export-button",
                "#exportTypesMenu",
            ], "WoS 导出按钮", timeout=10000)

            await export_btn.click()
            await delay(1, 2)

            # 选择格式
            format_map = {
                "bibtex": ["BibTeX", "bibtex"],
                "ris": ["RIS", "ris", "EndNote"],
                "csv": ["CSV", "csv", "Excel"],
                "excel": ["Excel", "excel", "XLS"],
                "plaintext": ["Plain text", "plaintext", "Tab-delimited"],
            }
            format_labels = format_map.get(format.lower(), ["BibTeX", "bibtex"])

            format_btn = None
            for label in format_labels:
                try:
                    format_btn = await find_element(result_page, [
                        f"button:has-text('{label}')",
                        f"a:has-text('{label}')",
                        f"mat-option:has-text('{label}')",
                        f"[data-ta='export-{label.lower()}']",
                    ], f"格式选项 {label}", timeout=5000)
                    break
                except Exception:
                    continue

            if not format_btn:
                return json.dumps({"error": f"未找到 {format} 导出选项"}, ensure_ascii=False)

            await format_btn.click()
            await delay(1, 2)

            # 设置记录范围和内容
            # 尝试设置 "Full Record" 内容选项
            try:
                content_sel = await find_element(result_page, [
                    "mat-select[data-ta='record-content']",
                    "select.record-content",
                    "#recordContent",
                ], "记录内容选择", timeout=5000)
                await content_sel.click()
                await delay(0.5, 1)

                full_record_opt = await find_element(result_page, [
                    "mat-option:has-text('Full Record')",
                    "mat-option:has-text('完整记录')",
                    "option:has-text('Full Record')",
                ], "完整记录选项", timeout=3000)
                await full_record_opt.click()
                await delay(0.5, 1)
            except Exception:
                pass  # 有些格式没有内容选择

            # 设置记录范围
            count = max(1, min(count, 1000))
            try:
                range_input = await find_element(result_page, [
                    "input[data-ta='record-range-to']",
                    "input.range-to",
                    "#recordRangeTo",
                ], "记录范围输入", timeout=5000)
                await range_input.fill(str(count))
            except Exception:
                pass

            # 点击导出/下载按钮
            async with result_page.expect_download(timeout=120000) as dl_info:
                dl_btn = await find_element(result_page, [
                    "button[data-ta='export-submit']",
                    "button:has-text('Export')",
                    "button:has-text('导出')",
                    "button:has-text('Download')",
                    "button.export-submit",
                ], "确认导出按钮", timeout=10000)
                await dl_btn.click()

            download = await dl_info.value
            filename = download.suggested_filename or f"wos_export.{format}"
            file_path = WOS_DOWNLOAD_DIR / filename
            await download.save_as(str(file_path))

            return json.dumps({
                "success": True,
                "file_path": str(file_path),
                "file_name": filename,
                "format": format,
                "records": count,
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "error": f"导出失败: {e}",
                "tip": "请确保已先运行 search_wos 并有搜索结果",
            }, ensure_ascii=False)

    @mcp.tool()
    async def login_wos(ctx: Context = None) -> str:
        """
        打开浏览器通过大学代理登录 Web of Science。
        首次使用时调用一次，之后登录态会自动持久化。
        会打开一个可见的浏览器窗口。

        Returns:
            登录状态信息
        """
        pool = _get_pool(ctx)
        await pool.restart(headless=False)
        page = await pool.new_page()
        keep_session_open = False

        try:
            await page.goto(WOS_ADVANCED_SEARCH, wait_until="domcontentloaded", timeout=30000)
            await delay(3, 5)
            await _dismiss_cookie_banner(page)

            current_url = page.url

            # Authenticate against the real advanced-search route. Some proxy
            # front pages load while the target route still redirects to CAS.
            if "webofscience" in current_url.lower() and "login" not in current_url.lower() and "authserver" not in current_url.lower():
                keep_session_open = True
                return json.dumps({
                    "status": "already_authenticated",
                    "message": "WoS proxy authentication is active in the current MCP process. Keep this browser window open while using WoS tools.",
                    "url": current_url,
                }, ensure_ascii=False)

            # 被重定向到登录页 — 等待用户登录
            try:
                await page.wait_for_url(
                    lambda u: "webofscience" in u.lower() and "login" not in u.lower() and "authserver" not in u.lower(),
                    timeout=300000,
                )
                keep_session_open = True
                return json.dumps({
                    "status": "login_success",
                    "message": "WoS login succeeded. Keep this browser window open while using WoS tools in the current MCP process.",
                    "url": page.url,
                }, ensure_ascii=False)
            except Exception:
                keep_session_open = True
                return json.dumps({
                    "status": "waiting_for_login",
                    "message": "WoS login page is open. Complete SZU proxy authentication, then retry search in the same MCP process.",
                    "url": page.url,
                }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"error": f"打开 WoS 失败: {e}"}, ensure_ascii=False)

        finally:
            if not keep_session_open:
                await page.close()
                await pool.restart(headless=True)

    globals()["login_wos_impl"] = login_wos

    @mcp.tool()
    async def check_wos_status(ctx: Context = None) -> str:
        """
        检查 Web of Science 连接和认证状态。

        Returns:
            连接状态、代理认证状态
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()

        try:
            await page.goto(WOS_BASE, wait_until="domcontentloaded", timeout=30000)
            await delay(2, 3)
            await _dismiss_cookie_banner(page)

            current_url = page.url
            status = {"wos_base_url": WOS_BASE, "download_dir": str(WOS_DOWNLOAD_DIR)}

            if "webofscience" in current_url.lower() and "login" not in current_url.lower():
                status["wos_accessible"] = True
                status["proxy_authenticated"] = True

                # 尝试获取机构名称
                for sel in [
                    ".institution-name", "[data-ta='institution']",
                    ".user-institution", "#inst_name",
                ]:
                    el = page.locator(sel)
                    if await el.count() > 0:
                        status["institution"] = (await el.first.inner_text()).strip()
                        break
            else:
                status["wos_accessible"] = True
                status["proxy_authenticated"] = False
                status["redirect_url"] = current_url
                status["tip"] = "需要登录，请运行 login_wos"

            return json.dumps(status, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({
                "wos_accessible": False, "error": str(e),
            }, ensure_ascii=False)

        finally:
            await page.close()

    # ── 资源 ──

    globals()["check_wos_status_impl"] = check_wos_status

    @mcp.resource("wos://field-tags")
    async def get_wos_field_tags() -> str:
        """WoS 高级检索字段标签参考"""
        return json.dumps({
            "TS": "Topic — 主题（标题+摘要+关键词）",
            "TI": "Title — 仅标题",
            "AU": "Author — 作者",
            "AI": "Author Identifiers — ORCID/ResearcherID",
            "SO": "Source — 期刊名",
            "DO": "DOI",
            "PY": "Publication Year — 发表年份",
            "AB": "Abstract — 摘要",
            "AK": "Author Keywords — 作者关键词",
            "DT": "Document Type — 文献类型 (Article/Review/...)",
            "OG": "Organization-Enhanced — 机构",
            "FU": "Funding Agency — 基金资助机构",
            "SU": "Research Area — 研究领域",
            "WC": "Web of Science Category — WoS 分类",
            "IS": "ISSN/ISBN",
            "示例": "TS=(deep learning) AND PY=(2023-2025) AND OG=(Shenzhen University)",
        }, ensure_ascii=False, indent=2)

    @mcp.tool()
    async def debug_wos(target: str = "advanced_search", ctx: Context = None) -> str:
        """
        调试工具：截图并分析 WoS 页面的 DOM 结构。
        用于诊断选择器失效等问题。

        Args:
            target: 要调试的页面 (advanced_search / home / current_url)

        Returns:
            页面截图路径 + DOM 结构摘要（表单元素、按钮等）
        """
        pool = _get_pool(ctx)
        page = await pool.new_page()

        try:
            url = WOS_ADVANCED_SEARCH if target == "advanced_search" else WOS_BASE
            if target.startswith("http"):
                url = target

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await delay(3, 5)
            await _dismiss_cookie_banner(page)

            # 截图
            screenshot_path = await _save_debug_screenshot(page, f"debug_{target}")

            # 收集页面关键 DOM 信息
            dom_info = await page.evaluate("""() => {
                const info = {
                    url: location.href,
                    title: document.title,
                    textareas: [],
                    buttons: [],
                    inputs: [],
                    iframes: [],
                    overlays: [],
                };

                // 所有 textarea
                document.querySelectorAll('textarea').forEach(el => {
                    info.textareas.push({
                        id: el.id, class: el.className,
                        dataTA: el.getAttribute('data-ta') || '',
                        placeholder: el.placeholder || '',
                        visible: el.offsetParent !== null,
                        rect: el.getBoundingClientRect().toJSON(),
                    });
                });

                // 所有 button
                document.querySelectorAll('button').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    if (!text) return;
                    info.buttons.push({
                        id: el.id, class: el.className,
                        dataTA: el.getAttribute('data-ta') || '',
                        text: text,
                        visible: el.offsetParent !== null,
                    });
                });

                // 所有 input
                document.querySelectorAll('input[type="text"], input[type="search"]').forEach(el => {
                    info.inputs.push({
                        id: el.id, class: el.className,
                        dataTA: el.getAttribute('data-ta') || '',
                        placeholder: el.placeholder || '',
                        visible: el.offsetParent !== null,
                    });
                });

                // iframe
                document.querySelectorAll('iframe').forEach(el => {
                    info.iframes.push({src: el.src || '', id: el.id});
                });

                // 遮罩层 / 弹窗
                document.querySelectorAll('[class*="overlay"], [class*="modal"], [class*="dialog"], [class*="cookie"], [id*="onetrust"]').forEach(el => {
                    info.overlays.push({
                        tag: el.tagName, id: el.id, class: el.className.toString().substring(0, 100),
                        visible: el.offsetParent !== null,
                    });
                });

                return info;
            }""")

            return json.dumps({
                "screenshot": screenshot_path,
                "dom_info": dom_info,
                "tip": "检查 screenshot 图片查看页面实际状态，dom_info 中的 textareas/buttons 用于更新选择器",
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"error": f"调试失败: {e}"}, ensure_ascii=False)

        finally:
            await page.close()
