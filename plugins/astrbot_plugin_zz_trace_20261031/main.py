"""鱼鱼找鸡：反向图搜（SauceNAO 双索引 → Google Lens → 网页版 → IQDB）"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Reply

PROXY = "http://127.0.0.1:7890"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PLATFORM = [
    (r"twitter\.com|x\.com", "Twitter"),
    (r"instagram\.com", "Instagram"),
    (r"pixiv\.net", "Pixiv"),
    (r"facebook\.com", "Facebook"),
    (r"pinterest", "Pinterest"),
    (r"tiktok\.com", "TikTok"),
    (r"danbooru", "Danbooru"),
    (r"yande\.re", "yande.re"),
    (r"gelbooru", "Gelbooru"),
    (r"deviantart", "DeviantArt"),
    (r"fanbox", "Fanbox"),
    (r"weibo", "微博"),
    (r"douyin", "抖音"),
    (r"sotwe", "Sotwe"),
]


SITE_HINTS = [
    ("pixiv", "Pixiv"), ("zerochan", "Zerochan"), ("pinterest", "Pinterest"),
    ("deviantart", "DeviantArt"), ("danbooru", "Danbooru"), ("konachan", "konachan"),
    ("yande.re", "yande.re"), ("gelbooru", "Gelbooru"), ("twitter", "Twitter"),
    ("x.com", "Twitter"), ("facebook", "Facebook"), ("instagram", "Instagram"),
    ("reddit", "Reddit"), ("hoyolab", "HoYoLAB"), ("巴哈", "巴哈姆特"),
    ("bilibili", "B站"), ("weibo", "微博"), ("artstation", "ArtStation"),
    ("anime-pictures", "Anime-Pictures"), ("safebooru", "Safebooru"),
    ("wiki", "Wiki"), ("fandom", "Fandom"), ("amazon", "Amazon"), ("ebay", "eBay"),
]


def platform_of(url: str) -> str:
    low = (url or "").lower()
    for pat, name in PLATFORM:
        if re.search(pat, low):
            return name
    try:
        return low.split("//", 1)[-1].split("/")[0].replace("www.", "") or "网页"
    except Exception:  # noqa: BLE001
        return "网页"


@register("zz_trace", "鲸鱼娘", "反向图搜：鱼鱼找鸡", "1.1.0")
class ZzTracePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._config = config
        self._lens_cache: dict[str, tuple[list[str], float]] = {}

    def _opt(self, key: str, default=""):
        cfg = self._config
        if cfg is None:
            return default
        try:
            val = cfg.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if val in (None, "") else val

    async def _image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        target = None
        chain = list(getattr(event.message_obj, "message", []) or [])
        for comp in chain:
            if isinstance(comp, Image):
                target = comp
                break
        if target is None:
            for comp in chain:
                if isinstance(comp, Reply):
                    for sub in getattr(comp, "chain", []) or []:
                        if isinstance(sub, Image):
                            target = sub
                            break
        if target is None:
            return None
        try:
            if target.file and Path(str(target.file)).exists():
                return Path(str(target.file)).read_bytes()
        except Exception:  # noqa: BLE001
            pass
        url = target.url or (f"file://{target.file}" if target.file else "")
        if not url:
            return None
        proxy = str(self._opt("proxy", PROXY)) or None
        for pr in (proxy, None):
            try:
                async with httpx.AsyncClient(timeout=30, verify=False, proxy=pr, trust_env=False,
                                             follow_redirects=True, headers={"User-Agent": UA}) as cli:
                    r = await cli.get(url)
                if r.status_code == 200 and len(r.content) > 2000:
                    return r.content
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _saucenao_api(self, img: bytes, db: str) -> list[dict]:
        key = str(self._opt("saucenao_key", ""))
        if not key:
            return []
        proxy = str(self._opt("proxy", PROXY)) or None
        for pr in (proxy, None):
            try:
                async with httpx.AsyncClient(timeout=45, verify=False, proxy=pr, trust_env=False,
                                             follow_redirects=True, headers={"User-Agent": UA}) as cli:
                    r = await cli.post(
                        "https://saucenao.com/search.php",
                        params={"output_type": "2", "db": db, "api_key": key, "numres": "4"},
                        files={"file": ("x.jpg", img, "image/jpeg")},
                    )
                if r.status_code != 200:
                    continue
                return list(r.json().get("results") or [])
            except Exception as exc:  # noqa: BLE001
                logger.info("[trace] saucenao db=%s 失败：%r", db, exc)
                continue
        return []

    async def _saucenao(self, img: bytes) -> list[str]:
        if not str(self._opt("saucenao_key", "")):
            return []
        rows = []
        seen: set[str] = set()
        for db in ("999", "41"):
            for item in await self._saucenao_api(img, db):
                header = item.get("header") or {}
                body = item.get("data") or {}
                try:
                    sim = float(header.get("similarity") or 0)
                except Exception:  # noqa: BLE001
                    sim = 0.0
                if sim < 40:
                    continue
                urls = [u for u in (body.get("ext_urls") or []) if isinstance(u, str)]
                main = urls[0] if urls else ""
                if main and main in seen:
                    continue
                if main:
                    seen.add(main)
                handle = body.get("twitter_user_handle") or ""
                author = (handle or body.get("author_name") or body.get("creator")
                          or body.get("member_name") or "")
                if isinstance(author, list):
                    author = author[0] if author else ""
                author = str(author).strip()
                title = str(body.get("title") or body.get("source") or "")[:36]
                if not main and not author:
                    continue
                plat = platform_of(main) if main else ("Twitter" if db == "41" else "网页")
                rows.append((sim, plat, author, title, main))
        rows.sort(key=lambda x: (-x[0], 0 if x[1] == "Twitter" else 1))
        lines: list[str] = []
        for sim, plat, author, title, main in rows[:4]:
            lines.append(f"相似度：{sim:.1f}%")
            lines.append(f"平台：{plat}")
            if author:
                lines.append(f"名字：{author}")
                if plat == "Twitter" and not author.startswith("@"):
                    lines.append(f"账号：@{author}")
            if title:
                lines.append(f"标题：{title}")
            if main:
                lines.append(f"链接：{main}")
            elif author:
                lines.append(f"链接：https://x.com/{author.lstrip('@')}")
            lines.append("―――――")
        return lines


    @staticmethod
    def _dhash(data: bytes, size: int = 8) -> int | None:
        """计算 dHash（差分感知哈希），失败返回 None。"""
        try:
            import io
            from PIL import Image as _Image
            im = _Image.open(io.BytesIO(data)).convert("L").resize((size + 1, size), _Image.LANCZOS)
            px = list(im.getdata())
            bits = 0
            idx = 0
            for row in range(size):
                for col in range(size):
                    left = px[row * (size + 1) + col]
                    right = px[row * (size + 1) + col + 1]
                    if left > right:
                        bits |= 1 << idx
                    idx += 1
            return bits
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _hamming(a: int | None, b: int | None) -> int:
        if a is None or b is None:
            return 999
        return bin(a ^ b).count("1")

    async def _upload_public(self, img: bytes) -> str:
        proxy = str(self._opt("proxy", PROXY)) or None
        hosts = [
            ("https://uguu.se/upload?output=text", "files[]"),
            ("https://catbox.moe/user/api.php", "fileToUpload"),
        ]
        for url, field in hosts:
            for pr in (proxy, None):
                try:
                    async with httpx.AsyncClient(timeout=45, verify=False, proxy=pr, trust_env=False,
                                                 follow_redirects=True, headers={"User-Agent": UA}) as cli:
                        if "catbox" in url:
                            r = await cli.post(url, data={"reqtype": "fileupload"},
                                               files={field: ("x.jpg", img, "image/jpeg")})
                        else:
                            r = await cli.post(url, files={field: ("x.jpg", img, "image/jpeg")})
                    text = (r.text or "").strip()
                    if r.status_code == 200 and text.startswith("http"):
                        return text.split()[0]
                except Exception:  # noqa: BLE001
                    continue
        return ""

    @staticmethod
    def _build_link(platform: str, name: str, title: str) -> str:
        q = (name or title or "").strip()
        if not q:
            return ""
        tag = ""
        tg = re.search(r"#([^\s#]{2,20})", title or q)
        if tg:
            tag = tg.group(1)
        low = platform.lower()
        if platform in ("Twitter", "X"):
            return "https://x.com/search?q=" + quote(q[:24])
        if "instagram" in low:
            if tag:
                return "https://www.instagram.com/explore/tags/" + quote(tag)
            return "https://www.instagram.com/explore/search/keyword/?q=" + quote(q[:30])
        if "facebook" in low:
            return "https://www.facebook.com/search/top?q=" + quote(q[:40])
        if "pinterest" in low:
            return "https://www.pinterest.com/search/pins/?q=" + quote(q[:30])
        if "pixiv" in low:
            return "https://www.pixiv.net/search.php?word=" + quote(q[:30])
        if "巴哈" in platform or "gamer" in low:
            return "https://www.google.com/search?q=site:gamer.com.tw " + quote(q[:40])
        return "https://www.google.com/search?q=" + quote(q[:50])

    async def _lens(self, img: bytes) -> list[str]:
        hkey = hashlib.sha1(img).hexdigest()
        hit = self._lens_cache.get(hkey)
        if hit and time.time() - hit[1] < 3600:
            return list(hit[0])
        try:
            from playwright.async_api import async_playwright
        except Exception:  # noqa: BLE001
            logger.info("[trace] 未安装 playwright，跳过 Lens")
            return []
        pub = await self._upload_public(img)
        if not pub:
            logger.info("[trace] 图床上传失败，跳过 Lens")
            return []
        lens_url = "https://lens.google.com/uploadbyurl?url=" + quote(pub, safe="") + "&hl=en"

        raw = ""
        anchors: list[dict] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"]
                )
                try:
                    ctx = await browser.new_context(
                        user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900},
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    )
                    page = await ctx.new_page()
                    await page.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                    )
                    await page.goto(lens_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(9000)
                    if "sorry/index" in page.url:
                        logger.info("[trace] Google 限流，等待后重试一次")
                        await page.wait_for_timeout(6000)
                        await page.goto(lens_url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(9000)

                    async def grab() -> tuple[str, list[dict]]:
                        _t = await page.inner_text("body")
                        _a = await page.evaluate(
                            """() => {
                                const out = [];
                                document.querySelectorAll('a[href]').forEach(a => {
                                    const t = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                                    if (t.length > 6) out.push({txt: t.slice(0, 120), href: a.href});
                                });
                                return out;
                            }"""
                        )
                        return _t, _a

                    raw, anchors = await grab()

                    # 按人工流程走：外观匹配 → 查看完全匹配的结果 → 点第一个 → 读落地页里的真实推文
                    exact_url = ""
                    try:
                        tab = page.get_by_text("外观匹配", exact=True).first
                        if await tab.count() == 0:
                            tab = page.get_by_text("Visual matches", exact=True).first
                        if await tab.count() > 0:
                            await tab.click(timeout=5000)
                            await page.wait_for_timeout(3000)
                        for label in ("查看完全匹配的结果", "查看完全匹配", "See exact matches", "Exact matches"):
                            link = page.get_by_text(label).first
                            if await link.count() > 0:
                                await link.click(timeout=5000)
                                await page.wait_for_timeout(4000)
                                break
                        raw2, _ = await grab()
                        raw = raw + "\n" + raw2
                        # 收集候选卡片（带缩略图），用 dHash 挑跟原图最像的那张
                        src_hash = self._dhash(img)
                        cards: list[tuple[int, object]] = []
                        for idx, cand in enumerate(await page.query_selector_all("a")):
                            try:
                                t = (await cand.inner_text() or "").strip()
                                if "x.com/" not in t and "twitter.com/" not in t:
                                    continue
                                thumb = await cand.query_selector("img")
                                thumb_src = await thumb.get_attribute("src") if thumb else ""
                            except Exception:  # noqa: BLE001
                                continue
                            dist = 500
                            if thumb_src.startswith("data:image") and src_hash is not None:
                                try:
                                    import base64 as _b64
                                    raw_thumb = _b64.b64decode(thumb_src.split(",", 1)[1])
                                    dist = self._hamming(src_hash, self._dhash(raw_thumb))
                                except Exception:  # noqa: BLE001
                                    dist = 500
                            elif thumb_src and src_hash is not None:
                                try:
                                    async with httpx.AsyncClient(timeout=20, verify=False,
                                                                 proxy=str(self._opt("proxy", PROXY)) or None,
                                                                 trust_env=False, follow_redirects=True,
                                                                 headers={"User-Agent": UA}) as cli:
                                        rr = await cli.get(thumb_src)
                                    if rr.status_code == 200:
                                        dist = self._hamming(src_hash, self._dhash(rr.content))
                                except Exception:  # noqa: BLE001
                                    pass
                            cards.append((dist, cand))
                            if len(cards) >= 5:
                                break
                        cards.sort(key=lambda x: x[0])
                        if cards:
                            logger.info("[trace] dHash 距离：%s", [c[0] for c in cards])
                        first = cards[0][1] if cards else None
                        exact_list: list[str] = []
                        for _dist, card in cards[:2]:
                            try:
                                href = await card.get_attribute("href") or ""
                                target = href if href.startswith("http") else "https://www.google.com" + href
                                sub = await ctx.new_page()
                                found = ""
                                try:
                                    await sub.goto(target, wait_until="domcontentloaded", timeout=40000)
                                    await sub.wait_for_timeout(2500)
                                    cand_urls = await sub.eval_on_selector_all(
                                        "a[href]", "els=>els.map(e=>e.href)"
                                    )
                                    for u in cand_urls or []:
                                        if re.search(r"(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{15,25}", u or ""):
                                            found = u
                                            break
                                    if not found:
                                        txt2 = await sub.inner_text("body")
                                        m2 = re.search(
                                            r"https?://(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{15,25}", txt2 or ""
                                        )
                                        if m2:
                                            found = m2.group(0)
                                finally:
                                    await sub.close()
                                if found and found not in exact_list:
                                    exact_list.append(found)
                            except Exception:  # noqa: BLE001
                                continue
                            if len(exact_list) >= 2:
                                break
                        if exact_list:
                            exact_url = exact_list[0]
                            raw = raw + "\n" + "\n".join(exact_list) + "\n"
                    except Exception as exc:  # noqa: BLE001
                        logger.info("[trace] 完全匹配流程失败：%r", exc)
                    if exact_url:
                        logger.info("[trace] 完全匹配第一条：%s", exact_url)
                        raw = raw + "\n" + exact_url + "\n"
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.info("[trace] Lens 失败：%r", exc)
            return []

        rows = [x.strip() for x in (raw or "").split("\n") if x.strip()]
        lines: list[str] = []
        ident = ""
        for i, row in enumerate(rows):
            if row in ("AI 概览", "AI Overview", "AI 摘要", "Обзор", "Vista general") and i + 1 < len(rows):
                ident = rows[i + 1]
                break
        if not ident:
            for row in rows[:25]:
                low = row.lower()
                if any(k in low for k in ("portraying", "cosplayer", "cosplay of", "角色扮演", "cos 的")):
                    ident = row
                    break
        if ident:
            lines.append(f"识别：{ident[:110]}")
            lines.append("―――――")

        # Lens 页面文本里直接出现的 @handle（最可靠，不用再搜）
        handles: list[str] = []
        for m in re.finditer(r"\(@([A-Za-z0-9_.]{3,20})\)", raw or ""):
            h = m.group(1)
            if h.lower() not in {x.lower() for x in handles}:
                handles.append(h)
        # 优先取「完全匹配 / Exact matches」区段里带 /status/ 的行的作者 handle
        for marker in ("完全匹配", "Exact matches"):
            if marker in (raw or ""):
                tail = (raw or "").split(marker, 1)[1]
                for m in re.finditer(r"(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})/status/", tail):
                    h = m.group(1)
                    if h.lower() not in {x.lower() for x in handles}:
                        handles.insert(0, h)
                for m in re.finditer(r"@([A-Za-z0-9_.]{3,20})", tail):
                    h = m.group(1)
                    if h.lower() not in {x.lower() for x in handles}:
                        handles.append(h)
                break
        if not handles:
            for m in re.finditer(r"@([A-Za-z0-9_]{3,20})", raw or ""):
                h = m.group(1)
                if h.lower() in {"gmail", "google", "youtube"}:
                    continue
                if h.lower() not in {x.lower() for x in handles}:
                    handles.append(h)
        # Lens 的「完全匹配」列表里会直接印出推文 URL（锚点是加密跳转，但文字是真的）
        status_urls: list[str] = []
        for m in re.finditer(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]{2,20})/status/(\d{6,25})", raw or ""):
            sid = m.group(2)
            tail = (raw or "")[m.end():m.end() + 3]
            if len(sid) < 18 or tail.startswith(("…", "...", " ..")):
                continue  # 被省略号截断的链接是废的，直接丢掉
            u = f"https://x.com/{m.group(1)}/status/{sid}"
            if u not in status_urls:
                status_urls.append(u)
        m_exact = re.search(r"(https?://(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{15,25})", raw or "")
        if m_exact and m_exact.group(1) not in status_urls:
            status_urls.insert(0, m_exact.group(1))
        status_urls = [u for u in status_urls if len(u.rsplit("/status/", 1)[-1]) >= 18]
        if status_urls:
            lines.append("X 帖子（完全匹配）：")
            for u in status_urls[:3]:
                snippet = ""
                for row in (raw or "").split("\n"):
                    if u.split("://", 1)[-1] in row:
                        snippet = row.replace(u, "").strip(" ·|")[:40]
                        break
                lines.append(f"  {u}　{snippet}" if snippet else f"  {u}")
            lines.append("―――――")

        if handles and not status_urls and str(self._opt("x_auth_token", "")):
            for h in handles[:2]:
                got = await self._x_posts(f"from:{h}", limit=2)
                if got:
                    status_urls.extend([g.split("　")[0] for g in got])
                    lines.append("X 帖子（完全匹配）：")
                    for g in got[:2]:
                        lines.append(f"  {g}")
                    lines.append("―――――")
                    break

        for h in handles[:2]:
            name_hint = ""
            hm = re.search(r"([^\n]{2,20}?)（?@?" + re.escape(h) + r"）?", raw or "")
            if hm:
                cand_name = hm.group(1).strip(" ·•|（(\n")
                cand_name = re.sub(r"https?://\S*", "", cand_name).strip()
                if 1 < len(cand_name) <= 20 and "@" not in cand_name:
                    name_hint = cand_name
            lines.append("平台：Twitter")
            if name_hint:
                lines.append(f"名字：{name_hint}")
            lines.append(f"账号：@{h}")
            lines.append(f"链接：https://x.com/{h}")
            lines.append("―――――")

        seen: set[str] = set()
        pushed = [0]

        def guess_platform(txt: str, url: str) -> str:
            low = f"{txt} {url}".lower()
            if re.search(r"\bx\b|twitter|x\.com|on x|sotwe", low):
                return "Twitter"
            for pat, name in PLATFORM:
                if re.search(pat, low):
                    return name
            if re.match(r"^(\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2})", txt.strip()):
                return "Twitter"
            head = txt.strip().lower()
            for key, name in SITE_HINTS:
                if head.startswith(key):
                    return name
            for key, name in SITE_HINTS:
                if key in head:
                    return name
            m2 = re.match(r"^([A-Za-z0-9\u4e00-\u9fff\.\-]{3,16})\s", txt.strip())
            if m2:
                return m2.group(1)
            return "网页"

        def push(plat: str, name: str, url: str, title: str = "") -> None:
            if pushed[0] >= 4:
                return
            key = (name or title)[:18]
            if not key or key in seen:
                return
            seen.add(key)
            if not url:
                url = self._build_link(plat, name, title)
            lines.append(f"平台：{plat}")
            if name:
                lines.append(f"名字：{name[:24]}")
            if title:
                lines.append(f"标题：{title[:44]}")
            if url:
                lines.append(f"链接：{url[:120]}")
            lines.append("―――――")
            pushed[0] += 1

        SKIP_TXT = {
            "accessibility help", "skip to main content", "sign in", "ai mode", "all",
            "exact matches", "visual matches", "feedback", "search results", "translate",
            "跳到主要内容", "无障碍功能帮助", "翻译", "登录", "AI 模式", "全部",
            "完全匹配", "外观匹配", "反馈", "搜索结果",
        }
        for a in anchors or []:
            txt = a.get("txt") or ""
            href = a.get("href") or ""
            if re.search(r"google\.com/(search|support|preferences|policies)|sorry/index", href):
                continue
            if txt.strip().lower() in SKIP_TXT:
                continue
            url = ""
            um = re.search(r"https?://[^\s，。)）]+", txt)
            if um:
                url = um.group(0)
            else:
                hm = re.search(r"@([A-Za-z0-9_.]{2,20})", txt)
                if hm:
                    url = f"https://x.com/{hm.group(1)}"
            plat = guess_platform(txt, url)
            name = ""
            title = ""
            nm = re.search(r"^(.{2,26}?)\s*\(@([A-Za-z0-9_.]+)\)", txt)
            if nm:
                name = f"{nm.group(1).strip()} @{nm.group(2)}"
                url = f"https://x.com/{nm.group(2)}"
            else:
                nm2 = re.search(r"^(.{2,26}?)\s*(?:on X|\| X|/ X|의 X)", txt)
                if nm2:
                    name = nm2.group(1).strip()
            tm = re.search(r'[:：]\s*"?(.{6,60}?)"?\s*(?:https?://|$)', txt)
            if tm:
                title = tm.group(1)
            if not name and not title:
                title = re.sub(r"https?://\S+", "", txt).strip()[:44]
            if title:
                for key, _nm in SITE_HINTS:
                    if title.lower().startswith(key):
                        title = title[len(key):].lstrip(" -:|/·")
                        break
            if not name and title and plat in ("Twitter", "X"):
                hm2 = re.search(r"@([A-Za-z0-9_.]{2,20})", txt)
                if hm2:
                    url = f"https://x.com/{hm2.group(1)}"
                    name = f"@{hm2.group(1)}"
            push(plat, name, url, title)

        if pushed[0] == 0:
            for row in rows:
                m = re.search(r"^(?:X|Twitter)\s+(.{2,24}?)\s+on X", row)
                if m:
                    push("Twitter", m.group(1).strip(), "", "")
                    if pushed[0] >= 2:
                        break

        # 统一清洗：丢掉被省略号截断的链接、修正 @handle 末尾的点
        cleaned: list[str] = []
        for ln in lines:
            if ("…" in ln or "..." in ln) and "http" in ln:
                continue
            ln = re.sub(r"@([A-Za-z0-9_]+)\.(?=\s|$|　)", r"@\1", ln)
            cleaned.append(ln)
        lines = cleaned
        if lines:
            self._lens_cache[hkey] = (list(lines), time.time())
        return lines

    async def _x_search(self, name: str, limit: int = 3) -> list[str]:
        """登录态下在 x.com 搜名字（用户搜索），返回真实主页链接。"""
        name = (name or "").strip()
        if not name:
            return []
        token = str(self._opt("x_auth_token", ""))
        ct0 = str(self._opt("x_ct0", ""))
        if not token:
            return []
        try:
            from playwright.async_api import async_playwright
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"]
                )
                try:
                    ctx = await browser.new_context(
                        user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900}
                    )
                    cookies = [{
                        "name": "auth_token", "value": token, "domain": ".x.com", "path": "/",
                        "httpOnly": True, "secure": True, "sameSite": "None",
                    }]
                    if ct0:
                        cookies.append({
                            "name": "ct0", "value": ct0, "domain": ".x.com", "path": "/",
                            "httpOnly": False, "secure": True, "sameSite": "Lax",
                        })
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    await page.goto(
                        "https://x.com/search?q=" + quote(name) + "&f=user",
                        wait_until="domcontentloaded", timeout=45000,
                    )
                    await page.wait_for_timeout(6000)
                    cells = await page.evaluate(
                        """() => {
                            const out = [];
                            document.querySelectorAll('[data-testid="UserCell"]').forEach(c => {
                                const a = c.querySelector('a[href^="/"]');
                                if (!a) return;
                                const href = a.getAttribute('href') || '';
                                const text = (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80);
                                out.push({href, text});
                            });
                            return out.slice(0, 8);
                        }"""
                    )
                    me = await page.evaluate(
                        """() => {
                            const a = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"] a[href^="/"]');
                            return a ? (a.getAttribute('href') || '') : '';
                        }"""
                    )
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.info("[trace] X 搜索失败：%r", exc)
            return []

        self_handle = me.strip("/").lower() if isinstance(me, str) else ""
        out = []
        for cell in cells or []:
            href = (cell or {}).get("href") or ""
            m = re.fullmatch(r"/([A-Za-z0-9_]{2,20})", href)
            if not m:
                continue
            handle = m.group(1)
            if handle.lower() == self_handle:
                continue
            text = (cell or {}).get("text") or ""
            if not text:
                continue
            url = f"https://x.com/{handle}"
            label = re.split(r"\s(?:关注|正在关注|Following|Follow|由 Grok|Translate|Translated)", text)[0].strip()[:40]
            entry = f"{url}　{label}"
            if url not in " ".join(out):
                out.append(entry)
            if len(out) >= limit:
                break
        if out:
            logger.info("[trace] X 搜索 %s → %s", name, out[:limit])
        return out

    async def _avatar(self) -> Path | None:
        """取机器人 QQ 头像（缓存 7 天），找不到返回 None。"""
        try:
            base = Path(r"D:\DSK\BOT\runtime")
            base.mkdir(parents=True, exist_ok=True)
            out = base / "zz_trace_avatar.png"
            if out.exists() and time.time() - out.stat().st_mtime < 7 * 86400:
                return out
            qq = str(self._opt("bot_qq", "1132046"))
            url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
            proxy = str(self._opt("proxy", PROXY)) or None
            for pr in (proxy, None):
                try:
                    async with httpx.AsyncClient(timeout=25, verify=False, proxy=pr,
                                                 trust_env=False, follow_redirects=True,
                                                 headers={"User-Agent": UA}) as cli:
                        r = await cli.get(url)
                    if r.status_code == 200 and len(r.content) > 2000:
                        out.write_bytes(r.content)
                        return out
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.info("[trace] 头像获取失败：%r", exc)
        return None

    async def _x_posts(self, query: str, limit: int = 2) -> list[str]:
        """登录态下搜推文，返回真实帖子（含 /status/ 的永久链接）。"""
        query = (query or "").strip()
        token = str(self._opt("x_auth_token", ""))
        if not query or not token:
            return []
        try:
            from playwright.async_api import async_playwright
        except Exception:  # noqa: BLE001
            return []
        cells: list[dict] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True, args=["--disable-blink-features=AutomationControlled"]
                )
                try:
                    ctx = await browser.new_context(
                        user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900}
                    )
                    cookies = [{
                        "name": "auth_token", "value": token, "domain": ".x.com", "path": "/",
                        "httpOnly": True, "secure": True, "sameSite": "None",
                    }]
                    ct0 = str(self._opt("x_ct0", ""))
                    if ct0:
                        cookies.append({
                            "name": "ct0", "value": ct0, "domain": ".x.com", "path": "/",
                            "httpOnly": False, "secure": True, "sameSite": "Lax",
                        })
                    await ctx.add_cookies(cookies)
                    page = await ctx.new_page()
                    await page.goto(
                        "https://x.com/search?q=" + quote(query) + "&f=top",
                        wait_until="domcontentloaded", timeout=45000,
                    )
                    await page.wait_for_timeout(6500)
                    cells = await page.evaluate(
                        """() => {
                            const out = [];
                            document.querySelectorAll('[data-testid="cellInnerDiv"]').forEach(c => {
                                const a = c.querySelector('a[href*="/status/"]');
                                if (!a) return;
                                const txt = (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90);
                                out.push({href: a.getAttribute('href') || '', text: txt});
                            });
                            return out.slice(0, 12);
                        }"""
                    )
                finally:
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.info("[trace] X 推文搜索失败：%r", exc)
            return []
        out: list[str] = []
        seen_ids: set[str] = set()
        for cell in cells or []:
            href = (cell or {}).get("href") or ""
            m = re.search(r"/status/(\d{6,25})", href)
            if not m:
                continue
            sid = m.group(1)
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            url = "https://x.com" + href if href.startswith("/") else href
            snippet = (cell or {}).get("text") or ""
            snippet = re.sub(r"https?://\S+", "", snippet).strip()[:40]
            out.append(f"{url}　{snippet}")
            if len(out) >= limit:
                break
        if out:
            logger.info("[trace] X 推文搜索 %s → %s", query, [o[:52] for o in out])
        return out

    @staticmethod
    def _strip_tags(html: str) -> str:
        txt = re.sub(r"<br\s*/?>", " ", html or "")
        txt = re.sub(r"<[^>]+>", "", txt)
        return re.sub(r"\s+", " ", txt).strip()

    async def _saucenao_web(self, img: bytes) -> list[str]:
        proxy = str(self._opt("proxy", PROXY)) or None
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://saucenao.com/",
            "Origin": "https://saucenao.com",
        }
        html = ""
        for pr in (proxy, None):
            try:
                async with httpx.AsyncClient(timeout=60, verify=False, proxy=pr, trust_env=False,
                                             follow_redirects=True, headers=hdr) as cli:
                    r = await cli.post("https://saucenao.com/search.php",
                                       data={"db": "999", "numres": "6"},
                                       files={"file": ("x.jpg", img, "image/jpeg")})
                if r.status_code == 200 and "resulttable" in r.text:
                    html = r.text
                    break
            except Exception as exc:  # noqa: BLE001
                logger.info("[trace] saucenao web 失败：%r", exc)
                continue
        if not html:
            return []
        lines: list[str] = []
        for block in re.split(r'<div class="result"', html)[1:]:
            block = block[:4000]
            sm = re.search(r'resultsimilarityinfo">\s*([\d.]+)%', block)
            if not sm:
                continue
            try:
                sim = float(sm.group(1))
            except Exception:  # noqa: BLE001
                continue
            if sim < 40:
                continue
            tm = re.search(r'class="resulttitle">(.*?)</div>', block, re.S)
            title = self._strip_tags(tm.group(1)) if tm else ""
            fields = []
            for fm in re.finditer(
                r"<strong>([^<:]+):\s*</strong>\s*(?:<a[^>]*>(.*?)</a>|([^<]{0,60}))", block, re.S
            ):
                val = self._strip_tags(fm.group(2) or fm.group(3) or "")
                if val:
                    fields.append((fm.group(1).strip(), val))
            links = [u for u in re.findall(r'href="(https?://[^"]+)"', block)
                     if "saucenao.com" not in u and "img1.saucenao" not in u]
            main = links[0] if links else ""
            plat = platform_of(main) if main else "网页"
            author = next((v for k, v in fields
                           if k.lower() in ("member", "creator", "author", "twitter", "artist")), "")
            lines.append(f"相似度：{sim:.1f}%")
            lines.append(f"平台：{plat}")
            if author:
                lines.append(f"名字：{author}")
            if title:
                lines.append(f"标题：{title[:36]}")
            if main:
                lines.append(f"链接：{main}")
            lines.append("―――――")
            if len(lines) >= 12:
                break
        return lines

    async def _iqdb(self, img: bytes) -> list[str]:
        proxy = str(self._opt("proxy", PROXY)) or None
        try:
            async with httpx.AsyncClient(timeout=45, verify=False, proxy=proxy, trust_env=False,
                                         follow_redirects=True,
                                         headers={"User-Agent": UA, "Referer": "https://iqdb.org/"}) as cli:
                r = await cli.post("https://iqdb.org/", files={"file": ("x.jpg", img, "image/jpeg")})
            if r.status_code != 200:
                return []
            rows = re.findall(r"<td>(\d{2,3})%</td>", r.text)
            srcs = [s for s in re.findall(r'<a href="(https?://[^"]+)"[^>]*>([^<]{0,40})</a>', r.text)
                    if not re.search(r"iqdb|\.css|\.js", s[0])][:3]
            if not rows:
                return []
            lines = [f"IQDB 最高相似度：{max(rows)}%"]
            for url, title in srcs:
                lines.append(f"平台：{platform_of(url)}")
                if title.strip():
                    lines.append(f"名字：{title.strip()[:30]}")
                lines.append(f"链接：{url}")
                lines.append("―――――")
            return lines
        except Exception as exc:  # noqa: BLE001
            logger.info("[trace] iqdb 失败：%r", exc)
            return []

    JUNK_TITLES = (
        "privacy policy", "terms of service", "terms", "help", "login", "sign in",
        "the largest online art gallery", "discover", "explore", "home",
        "behance", "deviantart", "pinterest", "facebook", "instagram",
        "图片", "壁纸", "大全", "官网", "首页",
    )

    @classmethod
    def _keywords(cls, text: str) -> set[str]:
        words: set[str] = set()
        for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", text or ""):
            words.add(w.lower())
        for w in re.findall(r"[\u4e00-\u9fff]{2,6}", text or ""):
            words.add(w)
        drop = {"image", "shows", "features", "cosplayer", "portraying", "character",
                "from", "game", "mobile", "this", "that", "with", "the", "and"}
        return {w for w in words if w not in drop}

    @classmethod
    def _char_tokens(cls, ident: str) -> set[str]:
        """抠「角色名」强特征词：只取大写开头的词，别把 from/with 带进去。"""
        out: set[str] = set()
        text = ident or ""

        def add(name: str) -> None:
            name = (name or "").strip(" .,·")
            if len(name) < 3:
                return
            low = name.lower()
            out.add(low)
            for part in re.split(r"[\s\-]+", low):
                if len(part) >= 3:
                    out.add(part)

        m = re.search(r"portraying\s+([A-Z][\w\-']*(?:\s+[A-Z][\w\-']*){0,2})", text)
        if m:
            add(m.group(1))
        m2 = re.search(r"character\s+([A-Z][\w\-']*(?:\s+[A-Z][\w\-']*){0,2})", text)
        if m2:
            add(m2.group(1))
        m3 = re.search(r"\(([A-Z][\w\-']*(?:\s+[A-Z][\w\-']*){0,1})\)", text)
        if m3:
            add(m3.group(1))
        m4 = re.search(r"角色[：: ]?([\u4e00-\u9fff]{2,8})", text)
        if m4:
            add(m4.group(1))
        return {t for t in out if t and t not in {"the", "from", "with", "and", "cos"}}

    @classmethod
    def _curate(cls, lines: list[str], limit: int = 4, strict: set[str] | None = None) -> list[str]:
        """按识别关键词过滤无关条目，去掉垃圾标题，限制条数。"""
        ident = ""
        for ln in lines:
            if ln.startswith("识别："):
                ident = ln.replace("识别：", "")
                break
        kws = cls._keywords(ident)
        blocks: list[list[str]] = []
        cur: list[str] = []
        for ln in lines:
            if set(ln.strip()) <= set("―-—"):
                if cur:
                    blocks.append(cur)
                    cur = []
                continue
            cur.append(ln)
        if cur:
            blocks.append(cur)

        kept: list[tuple[int, list[str]]] = []
        for blk in blocks:
            text = " ".join(blk)
            if blk and blk[0].startswith("识别："):
                kept.append((99, blk))
                continue
            if blk and (blk[0].startswith("X 帖子") or blk[0].startswith("X 账号")):
                if not kws:
                    continue
                if blk[0].startswith("X 帖子"):
                    rows = [r for r in blk[1:]
                            if not any(jk in r.lower() for jk in cls.JUNK_TITLES)
                            and (("/status/" not in r)
                                 or re.search(r"/status/\d{18,20}(?:\s|　|$)", r.strip()))]
                    if not rows:
                        continue
                    blk = [blk[0]] + rows[:2]
                # X 帖子/账号是我们自己针对性搜出来的，不再按关键词二次过滤
                if len(blk) > 1:
                    kept.append((50, blk))
                continue
            title_part = " ".join(r for r in blk if r.startswith("标题：") or r.startswith("名字："))
            low = (title_part or text).lower()
            junk = any(j in low for j in cls.JUNK_TITLES)
            score = sum(1 for k in kws if k.lower() in low)
            if junk and score == 0:
                continue
            kept.append((score, blk))

        kept.sort(key=lambda x: -x[0])
        seen_urls: set[str] = set()
        dedup: list[tuple[int, list[str]]] = []
        for score, blk in kept:
            urls = [re.search(r"https?://\S+", r) for r in blk]
            urls = [m.group(0)[:60] for m in urls if m]
            if urls and all(u in seen_urls for u in urls):
                continue
            for u in urls:
                seen_urls.add(u)
            dedup.append((score, blk))
        kept = dedup
        out: list[str] = []
        for score, blk in kept[:limit + 1]:
            out.extend(blk)
            out.append("―――――")
        return out if out else lines

    @staticmethod
    def _format_card(lines: list[str], source: str) -> str:
        """固定模板：平台 / 账号 / X 帖子 / 链接，最多两条（dHash 最像的两张）。"""
        posts: list[str] = []
        platform_hint = ""
        for ln in lines:
            t = ln.strip()
            if not t or set(t) <= set("―-—"):
                continue
            low = t.lower()
            if any(j in low for j in ("privacy", "terms of service", "cookie", "accessibility")):
                continue
            if t.startswith("平台：") and not platform_hint:
                val = t.replace("平台：", "").strip()
                if val and val.lower() not in ("网页", "privacy", "learn"):
                    platform_hint = val
            for m in re.finditer(r"https?://(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d{10,25}", t):
                u = m.group(0)
                if u not in posts:
                    posts.append(u)
        out = ["已找到鸡", "🎯 鱼鱼找鸡结果", ""]
        shown = 0
        for post in posts[:2]:
            m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/", post)
            if not m:
                continue
            handle = "@" + m.group(1)
            prof = "https://x.com/" + m.group(1)
            if shown:
                out.append("")
            out.append(f"🌐 平台：{platform_hint or 'Twitter'}")
            out.append(f"🆔 账号：{handle}")
            out.append(f"📮 X 帖子：{post}")
            out.append(f"🔗 链接：{prof}")
            shown += 1
        if shown == 0:
            out.append("没找到可靠结果（Lens 识别可能有误，换张更清晰的原图再试）")
        return "\n".join(out)

    async def _run(self, event: AstrMessageEvent):
        yield event.plain_result("收到，开始找鸡")
        img = await self._image_bytes(event)
        logger.info("[trace] 收到图片 %s B", len(img) if img else 0)
        if not img:
            yield event.plain_result("没拿到图片，回复一张图再发指令试试")
            return

        # 主引擎：Google Lens（SauceNAO 已按需关闭）
        use_sn = bool(self._opt("use_saucenao", False))
        lines: list[str] = []
        source = ""
        if use_sn:
            lines = await self._saucenao(img)
            source = "SauceNAO API"
        if not lines:
            yield event.plain_result("正在用 Google Lens 找…")
            lines = await self._lens(img)
            source = "Google Lens"
        if not lines:
            lines = await self._iqdb(img)
            source = "IQDB 兜底"
        if not lines:
            yield event.plain_result(
                "Google Lens 这次没给出结果（可能被限流）。\n"
                "等几分钟再发，或者换一张更清晰的图。"
            )
            return
        # 识别出名字/标题 → 去 x.com 搜帖子与账号，把真实链接放到最前面
        ident = next((ln.replace("识别：", "") for ln in lines if ln.startswith("识别：")), "")
        is_cos = bool(re.search(r"cosplay|cosplayer|cos\s*play|角色扮演|cos照", ident or "", re.I))
        has_ident = bool(ident)
        lens_has_posts = any(ln.startswith("X 帖子") for ln in lines)
        if source == "Google Lens" and has_ident and is_cos and not lens_has_posts:
            cand = ""
            title_hint = ""
            for ln in lines:
                if not cand and ln.startswith("名字："):
                    cand = ln.replace("名字：", "").strip()
                if not title_hint and ln.startswith("标题："):
                    title_hint = ln.replace("标题：", "").strip()
            heads: list[str] = []
            if title_hint:
                for q in (title_hint[:24], title_hint[:12]):
                    if q:
                        got = await self._x_posts(q, limit=2)
                        if got:
                            heads = got
                            break
            if not cand:
                toks = self._char_tokens(ident)
                cand = max(toks, key=len) if toks else ""
            if not heads and cand:
                heads = await self._x_posts(f"{cand} cos", limit=2)
            accounts: list[str] = []
            if cand and heads:
                accounts = await self._x_search(cand, limit=2)
            prefix: list[str] = []
            if heads:
                prefix.append("X 帖子（最匹配）：")
                prefix.extend(f"  {u}" for u in heads)
                prefix.append("―――――")
            if accounts:
                prefix.append("X 账号：")
                prefix.extend(f"  {u}" for u in accounts)
                prefix.append("―――――")
            if prefix:
                # 识别行保留在最前，其余挪到后面
                while lines and lines[0].startswith("识别："):
                    prefix.insert(0, lines.pop(0))
                if lines and lines[0] == "―――――":
                    lines.pop(0)
                lines = prefix + lines

        ident_line = next((ln for ln in lines if ln.startswith("识别：")), "")
        strict = self._char_tokens(ident_line.replace("识别：", "")) if ident_line else set()
        kws = self._keywords(ident) if ident else set()
        raw_lines = list(lines)
        lines = self._curate(lines, limit=4, strict=strict)
        meaningful = [ln for ln in lines if ln.strip() and not set(ln.strip()) <= set("―-—")]
        if len(meaningful) <= 1:
            base = [ln for ln in lines if ln.strip() and not set(ln.strip()) <= set("―-—")]
            extra: list[str] = []
            buf: list[str] = []
            for ln in raw_lines:
                if not ln.strip() or set(ln.strip()) <= set("―-—"):
                    if buf:
                        extra.append("\n".join(buf))
                        buf = []
                    continue
                buf.append(ln)
            if buf:
                extra.append("\n".join(buf))
            picked: list[str] = []
            for b in extra:
                if "http" not in b or b.startswith("识别："):
                    continue
                low = b.lower()
                if any(jk in low for jk in self.JUNK_TITLES):
                    continue
                if kws and not any(k.lower() in low for k in kws):
                    continue
                picked.append(b)
                if len(picked) >= 2:
                    break
            lines = base
            if picked:
                for blk in picked:
                    lines.extend(blk.split("\n"))
                    lines.append("―――――")
            else:
                lines.append("没找到可靠来源（Lens 的识别可能有误，换张更清晰的原图再试）")

        body = self._format_card(lines, source)
        yield event.plain_result(body)

    @filter.command("鱼鱼找鸡")
    async def trace_cmd(self, event: AstrMessageEvent):
        async for r in self._run(event):
            yield r

    @filter.command("找鸡")
    async def trace_cmd2(self, event: AstrMessageEvent):
        async for r in self._run(event):
            yield r

    @filter.command("认图")
    async def trace_cmd3(self, event: AstrMessageEvent):
        async for r in self._run(event):
            yield r

    # ---------------------------------------------------------------- 群聊免唤醒入口
    @filter.regex(r"^(鱼鱼找鸡|找鸡|认图)$")
    async def trace_group(self, event: AstrMessageEvent):
        """群聊直接发即可触发（RegexFilter 不受 wake_prefix 限制）。"""
        msg = (event.message_str or "").strip()
        head = msg.split()[0] if msg.split() else ""
        rest = msg[len(head):].strip()
        async for r in self.trace_cmd(event):
            yield r
        event.stop_event()
