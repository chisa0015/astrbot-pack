"""用无头浏览器在 SPA 站点上搜番，拿到播放/详情链接（不依赖 astrbot）。

站点实测（2026-10-31）：
    agedm.io      搜索框 input[name=query]，结果 /detail/{id}
    girigiri      搜索框 input[placeholder*=搜索]，结果 /detail/{id}
    tvtfun        搜索框 input[type=search]，回车后跳 /anime?keyword=

用法：
    res = PlayResolver()
    links = await res.search("名侦探柯南")   # {"agedm": url, "girigiri": url, "tvtfun": url}
    await res.close()
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable

logger = logging.getLogger("playresolve")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SITES: list[dict] = [
    {
        "name": "agedm",
        "base": "https://www.agedm.io/",
        "input": 'input[name="query"]',
        "keep": "/detail/",
        "play": lambda url: url,  # 详情页里就有播放入口
    },
    {
        "name": "girigiri",
        "base": "https://ani.girigirilove.com/home",
        "input": 'input[placeholder*="搜索"]',
        "keep": "/detail/",
        "play": lambda url: url,
    },
    {
        "name": "tvtfun",
        "base": "https://www.tvtfun.net/",
        "input": 'input[type="search"]',
        "keep": "/anime",
        "play": lambda url: url,
    },
]


def _norm(text: str) -> str:
    return re.sub(r"[\s【】\[\]（）()·・~～!！?？,，.。:：★☆#]", "", text or "")


class PlayResolver:
    """懒启动的 Chromium 复用实例（每次搜索用独立 context，避免串味）。"""

    def __init__(self, cache_ttl: float = 6 * 3600, concurrency: int = 4, enabled: tuple[str, ...] | None = None) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[str, tuple[dict, float]] = {}
        self._ttl = cache_ttl
        self._enabled = enabled

    async def _ensure(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"]
            )
            logger.info("[playresolve] chromium 已启动")

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    async def _search_site(self, site: dict, keyword: str) -> str:
        page = await self._browser.new_page(user_agent=UA, locale="zh-CN")
        try:
            await page.goto(site["base"], wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(600)
            await page.fill(site["input"], keyword)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2600)
            links = await page.eval_on_selector_all(
                "a", "els => els.map(e => [e.href, (e.textContent||'').trim()])"
            )
            keep = site["keep"]
            cands = [(h, t) for h, t in links if keep in (h or "") and (h or "").startswith("http")]
            target = _norm(keyword)
            best = ""
            for href, text in cands:
                title = _norm(text)
                if target and (target in title or title[:6] and title[:6] in target):
                    best = href
                    break
            if not best and cands:
                best = cands[0][0]
            # 去掉重复段（同一 URL 常出现多次）
            return best or ""
        except Exception as exc:  # noqa: BLE001
            logger.info("[playresolve] %s 搜索失败：%r", site["name"], exc)
            return ""
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    async def search(self, keyword: str) -> dict[str, str]:
        """返回 {站点名: 链接}；失败的站点不出现。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return {}
        cached = self._cache.get(keyword)
        if cached and time.time() - cached[1] < self._ttl:
            return dict(cached[0])
        try:
            await self._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[playresolve] chromium 启动失败：%r", exc)
            return {}

        async def one(site: dict) -> tuple[str, str]:
            async with self._sem:
                return site["name"], await self._search_site(site, keyword)

        active = [s for s in SITES if not self._enabled or s["name"] in self._enabled]
        results = await asyncio.gather(*(one(s) for s in active), return_exceptions=True)
        found: dict[str, str] = {}
        for item in results:
            if isinstance(item, tuple) and item[1]:
                found[item[0]] = item[1]
        self._cache[keyword] = (dict(found), time.time())
        return found
