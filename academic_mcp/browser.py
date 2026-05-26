"""共享浏览器基础设施 — BrowserPool + 辅助函数"""

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Playwright,
)

# ─── 路径常量 ─────────────────────────────────────────────────────────────────

BROWSER_DATA_DIR = Path.home() / ".academic-mcp" / "browser-data"
DOWNLOAD_DIR = Path.home() / ".academic-mcp" / "downloads"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]


# ─── BrowserPool ──────────────────────────────────────────────────────────────

@dataclass
class BrowserPool:
    """管理 Playwright 持久化浏览器上下文，共享登录态"""

    headless: bool = True
    _context: Optional[BrowserContext] = field(default=None, init=False, repr=False)
    _playwright: Optional[Playwright] = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def get_context(self) -> BrowserContext:
        async with self._lock:
            if self._context is None:
                BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
                self._playwright = await async_playwright().start()
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DATA_DIR),
                    headless=self.headless,
                    viewport={"width": 1920, "height": 1080},
                    user_agent=random.choice(USER_AGENTS),
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                    ignore_default_args=["--enable-automation"],
                    bypass_csp=True,
                    accept_downloads=True,
                    locale="zh-CN",
                )
                await self._context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
                    window.chrome = { runtime: {} };
                """)
            return self._context

    async def new_page(self) -> Page:
        ctx = await self.get_context()
        return await ctx.new_page()

    async def restart(self, headless: bool):
        """关闭后以新模式重启"""
        await self.cleanup()
        self.headless = headless

    async def cleanup(self):
        async with self._lock:
            if self._context:
                await self._context.close()
                self._context = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None


# ─── 辅助函数 ─────────────────────────────────────────────────────────────────

async def delay(lo=0.5, hi=1.5):
    """随机延迟，模拟人类操作"""
    await asyncio.sleep(random.uniform(lo, hi))


async def human_type(page: Page, selector: str, text: str):
    """模拟人类打字速度"""
    el = page.locator(selector)
    await el.click()
    await el.fill("")
    for ch in text:
        await page.keyboard.type(ch, delay=random.randint(30, 80))
    await delay(0.3, 0.8)


async def find_element(page: Page, candidates: list[str], description: str, timeout: int = 10000):
    """
    分层选择器发现 — 按优先级尝试多个选择器，返回第一个可见的。
    用于 SPA 等选择器不稳定的场景。

    策略：先快速扫一遍 count()（不触发滚动），找到 DOM 中存在的候选；
    再对存在的候选做 wait_for(visible)，避免对不存在的选择器长时间等待。
    """
    # 第一轮：快速扫描哪些选择器在 DOM 中有匹配（不触发滚动）
    found_candidates = []
    for sel in candidates:
        try:
            if await page.locator(sel).count() > 0:
                found_candidates.append(sel)
        except Exception:
            continue

    # 对找到的候选等待可见
    if found_candidates:
        per_timeout = max(timeout // len(found_candidates), 2000)
        for sel in found_candidates:
            try:
                el = page.locator(sel)
                await el.first.wait_for(state="visible", timeout=per_timeout)
                return el.first
            except Exception:
                continue

    # 第二轮：如果第一轮全部落空（元素可能还在加载），
    # 用原始候选列表做 wait_for，给 SPA 异步渲染一个机会
    per_timeout = max(timeout // len(candidates), 2000)
    for sel in candidates:
        try:
            el = page.locator(sel)
            await el.first.wait_for(state="visible", timeout=per_timeout)
            return el.first
        except Exception:
            continue

    raise Exception(f"找不到 {description}，尝试的选择器: {candidates}")
