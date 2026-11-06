r"""JoyACG 本子源：从 joyacg.cc 抓一篇随机漫画的页面图。

站点结构（2026-10 实测）：
  /wp-sitemap.xml                      → 一组 /wp-sitemap-posts-post-N.xml
  /wp-sitemap-posts-post-N.xml         → 每份最多 2000 条 <loc> 文章地址（形如 /000007/）
  文章页                                → 正文里是 <img src="https://i0/i1.wp.com/supercdn.site/galleries/<id>/<page>.jpg">

网络注意（实测于 AstrBot 进程）：
- 站点对 curl 的 TLS 指纹会 reset，python httpx 正常；
- 站内页面直连可能被 reset、走本地代理通常可行；图床 i*.wp.com 是 fake-IP，必须走代理。
  所以统一「直连 → 代理」双路重试，图床下载默认先走代理。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from urllib.parse import urlparse

import httpx

from .service import DoujinshiGallery

SITEMAP_INDEX = "https://joyacg.cc/wp-sitemap.xml"
HOST = "https://joyacg.cc"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
IMAGE_PROXY = os.getenv("DOUJINSHI_IMAGE_PROXY") or "http://127.0.0.1:7890"
_GALLERY_HINT = re.compile(r"/(galleries|gallery|comic)/", re.I)
_IMG_TAG = re.compile(r"""<img[^>]+(?:data-src|data-original|src)\s*=\s*["']([^"']+)["']""", re.I)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I)


def _client(proxy: str | None, timeout: float = 30.0, referer: str | None = None) -> httpx.AsyncClient:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        verify=False,
        proxy=proxy,
        trust_env=False,
    )


def image_client(timeout: float = 60.0) -> httpx.AsyncClient:
    """图床下载用：默认走本地代理（fake-IP 只有代理认得）。"""
    return _client(IMAGE_PROXY, timeout=timeout, referer=HOST + "/")


async def _fetch_text(url: str, *, referer: str | None = None, retries: int = 3) -> str:
    """直连 / 代理 双路尝试，任一成功即返回。"""
    last: Exception | None = None
    for attempt in range(retries):
        for proxy in (IMAGE_PROXY, None):
            try:
                async with _client(proxy, referer=referer) as cli:
                    resp = await cli.get(url)
                if resp.status_code == 200 and resp.text:
                    return resp.text
                last = RuntimeError(f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                last = exc
        await asyncio.sleep(0.6 * (attempt + 1))
    raise RuntimeError((f"抓取失败 {url}：{last!r}" if last else f"抓取失败 {url}")[:300])


async def _post_sitemaps(limit: int = 6) -> list[str]:
    index = await _fetch_text(SITEMAP_INDEX)
    maps = [u for u in _LOC.findall(index) if "posts-post" in u]
    return maps[:limit] if limit else maps


async def _random_post_url() -> str:
    sitemaps = await _post_sitemaps()
    if not sitemaps:
        raise RuntimeError("JoyACG sitemap 里没有文章列表")
    recent = sitemaps[-3:] or sitemaps
    page = await _fetch_text(random.choice(recent))
    urls = [u for u in _LOC.findall(page) if u.rstrip("/").split("/")[-1].isdigit()]
    if not urls:
        raise RuntimeError("JoyACG sitemap 页面里没有可用文章地址")
    return random.choice(urls)


def _pick_pages(html: str) -> list[str]:
    candidates = []
    for raw in _IMG_TAG.findall(html):
        url = raw.strip()
        if not url.startswith("http"):
            continue
        low = url.lower()
        if any(bad in low for bad in ("qrserver", "apple-touch-icon", "logo", "avatar", "emoji", "icon-", ".svg")):
            continue
        candidates.append(url)
    gallery = [u for u in candidates if _GALLERY_HINT.search(urlparse(u).path)]
    chosen = gallery or [u for u in candidates if re.search(r"\.(jpe?g|png|webp)$", urlparse(u).path, re.I)]

    def sort_key(u: str) -> tuple[int, str]:
        m = re.search(r"/(\d+)\.(?:jpe?g|png|webp)$", urlparse(u).path, re.I)
        return (int(m.group(1)) if m else 10**6, u)

    return list(dict.fromkeys(sorted(chosen, key=sort_key)))


async def fetch_random_gallery(
    tags: list[str] | None = None,
    max_page: int | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> DoujinshiGallery:
    """抓一篇随机漫画，返回与 Atri 源同结构的本子元数据。

    JoyACG 不支持标签过滤，``tags`` 仅为接口兼容保留。
    """
    last_error: Exception | None = None
    for _ in range(4):  # 抽到没有图的文章（新闻/动画）就重抽
        try:
            url = await _random_post_url()
            html = await _fetch_text(url, referer=HOST + "/comic/")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        pages = _pick_pages(html)
        if len(pages) < 3:
            last_error = RuntimeError(f"文章 {url} 只有 {len(pages)} 张图，跳过")
            continue
        if max_page and max_page > 0:
            pages = pages[:max_page]
        title_match = _TITLE.search(html)
        title = title_match.group(1).strip() if title_match else "JoyACG 本子"
        title = re.sub(r"\s*-\s*JoyACG\s*$", "", title, flags=re.I) or "JoyACG 本子"
        slug = url.rstrip("/").split("/")[-1]
        gallery_id = int(slug) if slug.isdigit() else abs(hash(url)) % (10**9)
        return DoujinshiGallery(
            id=gallery_id,
            title=title[:120],
            page_urls=tuple(pages),
            upstream_title=title,
            source_url=url,
        )
    raise RuntimeError(f"JoyACG 连续抓取失败：{last_error}")
