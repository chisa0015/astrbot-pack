"""无 key 的 Google Lens 引擎（Playwright 真浏览器）

流程对齐人工操作：
  上传图 → lens.google.com 结果页 → 点「外观匹配」→ 点「查看完全匹配的结果」
  → 每张候选卡算 dHash 与原图比对 → 取最像的 1~2 张 → 点进去 → 从落地页抓真推文链接
"""

from __future__ import annotations

import re
from urllib.parse import quote

import aiohttp

try:  # 与 engines 的结构保持一致，失败时由调用方降级
    from .engines import EngineError, PostCandidate
except Exception:  # pragma: no cover
    from engines import EngineError, PostCandidate  # type: ignore

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _dhash(data: bytes, size: int = 8) -> int | None:
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(data)).convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(im.getdata())
        bits = 0
        idx = 0
        for row in range(size):
            for col in range(size):
                if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1]:
                    bits |= 1 << idx
                idx += 1
        return bits
    except Exception:
        return None


def _hamming(a: int | None, b: int | None) -> int:
    if a is None or b is None:
        return 999
    return bin(a ^ b).count("1")


async def _upload_public(image_bytes: bytes, proxy: str, timeout: int) -> str:
    """传到临时图床拿公网 URL（Lens 的 uploadbyurl 需要）。"""
    hosts = [
        ("https://uguu.se/upload?output=text", "files[]", "image/png"),
        ("https://catbox.moe/user/api.php", "fileToUpload", "image/png"),
    ]
    for url, field, mime in hosts:
        try:
            data = aiohttp.FormData()
            if "catbox" in url:
                data.add_field("reqtype", "fileupload")
            data.add_field(field, image_bytes, filename="x.png", content_type=mime)
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as sess:
                async with sess.post(url, data=data, proxy=proxy or None) as resp:
                    text = (await resp.text()).strip()
            if text.startswith("http"):
                return text.split()[0]
        except Exception:
            continue
    return ""


async def search_lens_playwright(
    image_bytes: bytes,
    *,
    timeout: int = 120,
    proxy: str = "",
    max_matches: int = 2,
) -> list[PostCandidate]:
    """Google Lens 网页抓取（无需 SerpApi key）。"""
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:  # noqa: BLE001
        raise EngineError(f"playwright 不可用: {exc}") from exc

    pub = await _upload_public(image_bytes, proxy, max(15, timeout // 3))
    if not pub:
        raise EngineError("图床上传失败")

    src_hash = _dhash(image_bytes)
    posts: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        try:
            ctx = await browser.new_context(
                user_agent=UA,
                locale="zh-CN",
                viewport={"width": 1440, "height": 950},
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
            page = await ctx.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            url = "https://lens.google.com/uploadbyurl?url=" + quote(pub, safe="") + "&hl=zh-CN"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            if "sorry/index" in page.url:
                await page.wait_for_timeout(5000)
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(8000)

            # 外观匹配 → 查看完全匹配的结果
            try:
                tab = page.get_by_text("外观匹配", exact=True).first
                if await tab.count() > 0:
                    await tab.click(timeout=5000)
                    await page.wait_for_timeout(3000)
                for label in ("查看完全匹配的结果", "查看完全匹配"):
                    link = page.get_by_text(label).first
                    if await link.count() > 0:
                        await link.click(timeout=5000)
                        await page.wait_for_timeout(4000)
                        break
            except Exception:
                pass

            # 候选卡片 + dHash 挑图
            cards: list[tuple[int, object]] = []
            for cand in await page.query_selector_all("a"):
                try:
                    text = (await cand.inner_text() or "").strip()
                    if "x.com/" not in text and "twitter.com/" not in text:
                        continue
                    thumb = await cand.query_selector("img")
                    src = (await thumb.get_attribute("src")) if thumb else ""
                except Exception:
                    continue
                dist = 500
                if src and src.startswith("data:image") and src_hash is not None:
                    try:
                        import base64

                        dist = _hamming(src_hash, _dhash(base64.b64decode(src.split(",", 1)[1])))
                    except Exception:
                        dist = 500
                cards.append((dist, cand))
                if len(cards) >= 5:
                    break

            cards.sort(key=lambda x: x[0])
            for _dist, card in cards[: max(1, max_matches)]:
                try:
                    href = await card.get_attribute("href") or ""
                    target = href if href.startswith("http") else "https://www.google.com" + href
                    sub = await ctx.new_page()
                    found = ""
                    try:
                        await sub.goto(target, wait_until="domcontentloaded", timeout=40000)
                        await sub.wait_for_timeout(2500)
                        hrefs = await sub.eval_on_selector_all("a[href]", "els=>els.map(e=>e.href)")
                        for u in hrefs or []:
                            if re.search(r"(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{15,25}", u or ""):
                                found = u
                                break
                        if not found:
                            txt = await sub.inner_text("body")
                            m = re.search(
                                r"https?://(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{15,25}", txt or ""
                            )
                            if m:
                                found = m.group(0)
                    finally:
                        await sub.close()
                    if found and found not in posts:
                        posts.append(found)
                except Exception:
                    continue
        finally:
            await browser.close()

    out: list[PostCandidate] = []
    for idx, post in enumerate(posts[:max_matches]):
        m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/", post)
        handle = m.group(1) if m else ""
        out.append(
            PostCandidate(
                engine="lens",
                page_url=post,
                title="Google Lens 完全匹配",
                author_handle=handle,
                author_platform="X/Twitter" if handle else "",
                post_url=post,
                description="",
                image_url="",
                weight=1.6 - 0.1 * idx,  # Lens 完全匹配是强证据
            )
        )
    if not out:
        raise EngineError("Lens 无完全匹配结果")
    return out
