"""新番监控：抓吐槽弹幕网的周表 / 今日更新，支持命令查询与每日定时推送。

命令：
    新番 / 本周新番        → 本周番组表（按日期分组）
    今天新番 / 今日新番    → 今天更新的番
    新番 20                → 只看前 20 条

定时推送：
    配置 push_enabled=true + push_time（默认 20:00）+ push_targets（umi 列表，支持 owner 简写）
"""

from __future__ import annotations

import asyncio
import random
import time
import re
from datetime import datetime, timedelta
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import MessageChain
from urllib.parse import quote

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

WEEK_URL = "https://www.tucao.my/html/week_bgm.html"
TODAY_URL = "https://www.tucao.my/html/new.html"
HOST = "https://www.tucao.my"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PROXY = "http://127.0.0.1:7890"
UA_PLAY = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CACHE_DIR = Path(r"D:\DSK\BOT\runtime\bangumi_cache")
OWNER_UMO = "aiocqhttp:FriendMessage:2899675402"

_ITEM = re.compile(
    r'<div class="item" date="(?P<date>[\d-]+)">.*?'
    r'background-image:url\((?P<img>[^)]+)\).*?'
    r'<a href="(?P<link>/play/[^"]+)"[^>]*class="t">(?P<title>.*?)</a>.*?'
    r'<div class="i"><span>(?P<views>[\d,]+)</span><a>(?P<ep>[^<]*)</a>',
    re.S,
)
_TAG = re.compile(r"<[^>]+>")


class BangumiPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self._config = config
        self._task: asyncio.Task | None = None
        self._play_cache: dict[str, tuple[str, float]] = {}
        self._resolver = None

    # ---------------------------------------------------------------- 配置
    def _opt(self, key: str, default):
        cfg = self._config
        if cfg is None:
            return default
        try:
            val = cfg.get(key, default) if hasattr(cfg, "get") else getattr(cfg, key, default)
        except Exception:
            val = default
        return default if val in (None, "") else val

    @property
    def max_items(self) -> int:
        """0 = 不限（全部列出）；>0 为上限。"""
        try:
            return max(0, min(int(self._opt("max_items", 0)), 500))
        except Exception:
            return 0

    # ---------------------------------------------------------------- 抓取解析
    def _client(self, proxy: str | None, timeout: float = 25.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": UA, "Referer": HOST + "/"},
            verify=False,
            proxy=proxy,
            trust_env=False,
        )

    async def _html(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(2):
            for proxy in (None, PROXY):
                try:
                    async with self._client(proxy) as cli:
                        resp = await cli.get(url)
                    if resp.status_code == 200 and resp.text:
                        return resp.text
                    last = RuntimeError(f"HTTP {resp.status_code}")
                except Exception as exc:  # noqa: BLE001
                    last = exc
            await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"抓取失败 {url}：{last!r}"[:200])

    async def _source_items(self, kind: str) -> list[dict]:
        """kind: today / week；source: merged（默认，bgm+吐槽）/ bangumi / tucao。"""
        src = str(self._opt("source", "merged")).strip().lower()
        if src == "tucao":
            items = await self._fetch_items(TODAY_URL if kind == "today" else WEEK_URL)
            try:
                await self._resolve_play_links(items, len(items))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[bangumi] 播放链接解析失败：%r", exc)
            return items
        if src == "bangumi":
            return await self._bgm_today()
        # merged：bgm 名单 + 吐槽补充（kind=week 时强制整周），再统一解析播放链接
        items = await self._merged_items("week" if kind == "week" else None)
        try:
            await self._resolve_play_links(items, len(items))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[bangumi] 播放链接解析失败：%r", exc)
        return items

    async def _fetch_items(self, url: str) -> list[dict]:
        """逐块解析：按 <div class="item"> 切片，兼容「周表(带 date)」和「今日更新(无 date)」两种结构。"""
        html = await self._html(url)
        today = datetime.now().strftime("%Y-%m-%d")
        items: list[dict] = []
        seen: set[str] = set()
        for chunk in html.split('<div class="item"')[1:]:
            chunk = chunk[:4000]
            tm = re.search(r'class="t">(.*?)</a>', chunk, re.S)
            if not tm:
                continue
            title = _TAG.sub("", tm.group(1)).strip()
            if not title or title in seen:
                continue
            seen.add(title)
            lm = re.search(r'href="(/play/[^"]+)"', chunk)
            dm = re.search(r'date="(\d{4}-\d{2}-\d{2})"', chunk)
            em = re.search(r'<div class="i">.*?<a>(.*?)</a>', chunk, re.S)
            im = re.search(r'background-image:url\(([^)]+)\)', chunk)
            vm = re.search(r'<div class="i"><span>([\d,]+)</span>', chunk)
            items.append(
                {
                    "date": dm.group(1) if dm else today,
                    "title": title,
                    "ep": _TAG.sub("", em.group(1)).strip() if em else "",
                    "link": HOST + lm.group(1) if lm else HOST,
                    "img": im.group(1) if im else "",
                    "views": vm.group(1) if vm else "",
                }
            )
        return items

    # ---------------------------------------------------------------- 合并名单（bgm + 吐槽）
    async def _tucao_today_items(self) -> list[dict]:
        """吐槽周表里属于今天的条目（没有就退回今日更新页）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            week = await self._fetch_items(WEEK_URL)
        except Exception:  # noqa: BLE001
            week = []
        todays = [it for it in week if it.get("date") == today]
        if not todays:
            try:
                todays = await self._fetch_items(TODAY_URL)
            except Exception:  # noqa: BLE001
                todays = []
        return todays

    async def _bgm_week_items(self) -> list[dict]:
        """Bangumi 整周日历（每条带 weekday 标签）。"""
        url = "https://api.bgm.tv/calendar"
        headers = {"User-Agent": "yesha-astrbot-novel/2.0 (https://github.com/yesha)"}
        data = None
        for proxy in (PROXY, None):
            try:
                async with self._client(proxy, timeout=25.0) as cli:
                    resp = await cli.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    break
            except Exception:  # noqa: BLE001
                continue
        if not data:
            raise RuntimeError("Bangumi 周日历抓取失败")
        items: list[dict] = []
        for day in data:
            wd = str((day.get("weekday") or {}).get("cn", ""))
            for it in day.get("items") or []:
                imgs = it.get("images") or {}
                rating = (it.get("rating") or {}).get("score") or 0
                items.append(
                    {
                        "date": wd,
                        "title": it.get("name_cn") or it.get("name") or "",
                        "ep": f"⭐{rating}" if rating else "",
                        "link": it.get("url") or f"https://bgm.tv/subject/{it.get('id')}",
                        "img": imgs.get("large") or imgs.get("common") or "",
                        "views": "",
                        "_bgm_id": it.get("id"),
                        "_name_cn": it.get("name_cn") or "",
                        "_name": it.get("name") or "",
                        "_weekday": wd,
                    }
                )
        return items

    async def _merged_items(self, scope_override: str | None = None) -> list[dict]:
        """bgm 名单为主，吐槽有、bgm 没有的补进来（去重后合并）。"""
        scope = (scope_override or str(self._opt("merge_scope", "today"))).strip().lower()
        if scope == "week":
            bgm_items = await self._bgm_week_items()
            try:
                tucao_items = await self._fetch_items(WEEK_URL)
            except Exception:  # noqa: BLE001
                tucao_items = []
        else:
            bgm_items = await self._bgm_items()
            try:
                tucao_items = await self._tucao_today_items()
            except Exception:  # noqa: BLE001
                tucao_items = []
        bgm_titles = [it.get("_name_cn") or it.get("_name") or it["title"] for it in bgm_items]
        extras: list[dict] = []
        for it in tucao_items:
            raw_title = it["title"]
            if not self._norm_title(raw_title):
                continue
            if any(self._same_title(raw_title, t) for t in bgm_titles):
                continue
            extras.append(
                {
                    "date": it.get("date") or "",
                    "title": re.sub(r"^【[^】]*】", "", it["title"]).strip(),
                    "ep": it.get("ep") or "",
                    "link": it.get("link") or "",
                    "img": it.get("img") or "",
                    "views": "",
                    "_name_cn": "",
                    "_name": "",
                    "_bgm_id": None,
                    "_from_tucao": True,
                }
            )
            bgm_titles.append(raw_title)
        logger.info("[bangumi] 合并名单：bgm %s 部 + 吐槽补充 %s 部", len(bgm_items), len(extras))
        return bgm_items + extras

    # ---------------------------------------------------------------- 播放链接
    async def _akianime_play(self, item: dict) -> str:
        """AkiAnime 搜索（/bgmsearch/{kw}-------------），依次试中文名 / 原名 / 去季名。"""
        names: list[str] = []

        def variants(raw: str) -> list[str]:
            s = (raw or "").strip()
            if not s:
                return []
            m = re.match(r"^(.*?)[（(【\[]", s)
            base = (m.group(1).strip() if m else s)
            out: list[str] = []
            # 括号前的主体太短就说明标题本身带【】前缀，用完整名
            cand = base if len(base) >= 3 else s
            for v in (cand, s, re.sub(r"第[一二三四五六七八九十0-9]+季", "", cand).strip()):
                v = v.strip()
                if len(v) >= 2 and v not in out:
                    out.append(v)
            return out

        for raw in (item.get("_name_cn") or "", item.get("_name") or ""):
            for v in variants(raw):
                if v not in names:
                    names.append(v)
        for kw in names:
            url = "https://www.akianime.cc/bgmsearch/" + quote(kw) + "-------------.html"
            for attempt in range(2):
                for proxy in (None, PROXY):
                    try:
                        async with self._client(proxy, timeout=15.0) as cli:
                            resp = await cli.get(url)
                        if resp.status_code == 200:
                            m = re.search(r"(/bgmplay/[A-Za-z0-9]+-1-1\.html)", resp.text)
                            if m:
                                return "https://www.akianime.cc" + m.group(1)
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(0.2)
        return ""

    @staticmethod
    def _norm_title(text: str) -> str:
        s = text or ""
        s = re.sub(r"【[^】]*】", "", s)          # 【10月】/【字幕组】
        s = re.sub(r"[（(][^）)]*[）)]", "", s)     # 括号别名
        s = re.sub(r"(第[一二三四五六七八九十0-9]+[季期]|Season\s*\d+|\d+(st|nd|rd|th)\s*Season)", "", s, flags=re.I)
        s = re.sub(r"[ⅡⅢⅣⅤ]", "2", s)
        s = s.replace("二", "2") if re.search(r"第2季", s) else s
        s = re.sub(r"[\s·・~～!！?？,，.。:：★☆#/／\\\-—_、&＆'\"]", "", s)
        return s.strip().lower()

    @classmethod
    def _same_title(cls, a: str, b: str) -> bool:
        """判断两条标题是不是同一部番（规范化 + 包含 + 相似度）。"""
        na, nb = cls._norm_title(a), cls._norm_title(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(short) >= 4 and short in long_:
            return True
        if len(short) >= 5:
            import difflib

            return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82
        return False

    async def _tucao_play(self, title: str, pool: dict[str, str]) -> str:
        """在吐槽周表里按标题匹配：先精确（规范化后相等），再模糊（互相包含）。"""
        key = self._norm_title(title)
        if not key:
            return ""
        if key in pool:
            return pool[key]
        if len(key) >= 5:
            for k, link in pool.items():
                if len(k) >= 4 and (key in k or k in key):
                    return link
        return ""

    async def _acfun_play(self, title: str) -> str:
        """AcFun 搜索页抓第一个 /v/acXXXX 链接。"""
        kw = re.split(r"[（(【\[]", title)[0].strip()
        if not kw:
            return ""
        url = "https://www.acfun.cn/search?keyword=" + quote(kw)
        for proxy in (None, PROXY):
            try:
                async with self._client(proxy, timeout=15.0) as cli:
                    resp = await cli.get(url)
                if resp.status_code == 200:
                    m = re.search(r'(/v/ac\d+)', resp.text)
                    if m:
                        return "https://www.acfun.cn" + m.group(1)
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def _bilibili_play(self, title: str) -> str:
        """B站番剧搜索 API（media_bangumi），取第一条 ss/ep 链接。"""
        kw = re.split(r"[（(【\[]", title)[0].strip()
        if not kw:
            return ""
        headers = {"User-Agent": UA_PLAY, "Referer": "https://www.bilibili.com/"}
        for kw_try in (kw,):
            for stype in ("media_bangumi", "media_ft"):
                url = (
                    "https://api.bilibili.com/x/web-interface/search/type?search_type="
                    + stype
                    + "&keyword="
                    + quote(kw_try)
                )
                for proxy in (None, PROXY):
                    try:
                        async with self._client(proxy, timeout=15.0) as cli:
                            resp = await cli.get(url, headers=headers)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        for it in (data.get("data") or {}).get("result") or []:
                            link = it.get("url") or it.get("arcurl") or ""
                            if "bilibili.com/bangumi" in link:
                                return link if link.startswith("http") else "https:" + link
                    except Exception:  # noqa: BLE001
                        continue
        return ""

    async def _resolve_play_links(self, items: list[dict], limit: int) -> None:
        """给前 limit 部补播放链接（AkiAnime 主、吐槽兜底），结果写回 item['play']。"""
        if not items:
            return
        pool: dict[str, str] = {}
        tucao_items: list[dict] = []
        try:
            tucao_items = await self._fetch_items(WEEK_URL)
            for it in tucao_items:
                pool.setdefault(self._norm_title(it["title"]), it["link"])
            for it in await self._fetch_items(TODAY_URL):
                pool.setdefault(self._norm_title(it["title"]), it["link"])
        except Exception:  # noqa: BLE001
            pass

        sem = asyncio.Semaphore(5)
        browser_limit = self._browser_limit()

        async def one(item: dict, idx: int) -> None:
            title = item.get("title") or ""
            if not title:
                return
            async with sem:
                cached = self._play_cache.get(title)
                if cached and time.time() - cached[1] < 21600:
                    item["plays"] = dict(cached[0])
                    return
                plays: dict[str, str] = {}
                # ① 吐槽周表匹配（纯内存，零请求）
                tucao = await self._tucao_play(title, pool)
                if tucao:
                    plays["tucao"] = tucao
                # ② tvtfun：用 bgm id 直接拼，零请求零耗时，命中率 100%
                bgm_id = item.get("_bgm_id")
                if bgm_id:
                    plays["tvtfun"] = f"https://www.tvtfun.net/anime/{bgm_id}"
                # ③ akianime：HTTP 搜索（限前 limit 部，并发 5）
                if (limit <= 0 or idx < limit) and "tucao" not in plays:
                    try:
                        link = await self._akianime_play(item)
                        if link:
                            plays["akianime"] = link
                    except Exception:  # noqa: BLE001
                        pass
                # ③.5 acfun / bilibili：HTTP，成本低，只在前面都没命中时补
                if not plays.get("tucao") and not plays.get("akianime") and (limit <= 0 or idx < limit):
                    try:
                        acfun = await self._acfun_play(item.get("_name_cn") or title)
                        if acfun:
                            plays["acfun"] = acfun
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        bili = await self._bilibili_play(item.get("_name_cn") or title)
                        if bili:
                            plays["bilibili"] = bili
                    except Exception:  # noqa: BLE001
                        pass
                # ④ agedm / girigiri（无头浏览器，只给前 N 部，耗时长）
                if idx < browser_limit and not plays.get("tucao"):
                    try:
                        if self._resolver is None:
                            from .playresolve import PlayResolver

                            self._resolver = PlayResolver(enabled=("agedm", "girigiri"))
                        found = await self._resolver.search(
                            item.get("_name_cn") or item.get("_name") or title
                        )
                        for site, url in found.items():
                            plays.setdefault(site, url)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[bangumi] agedm 搜索失败：%r", exc)
                item["plays"] = plays
                self._play_cache[title] = (dict(plays), time.time())

        await asyncio.gather(*(one(it, i) for i, it in enumerate(items)))

    # ---------------------------------------------------------------- 源：Bangumi 日历
    async def _bgm_today(self) -> list[dict]:
        """Bangumi 今日番 + 播放链接解析（单源模式用）。"""
        items = await self._bgm_items()
        try:
            await self._resolve_play_links(items, self._play_limit())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[bangumi] 播放链接解析失败：%r", exc)
        return items

    async def _bgm_items(self) -> list[dict]:
        """Bangumi 番剧日历 → 今天（按星期几）放送的番（不含播放链接）。"""
        url = "https://api.bgm.tv/calendar"
        headers = {"User-Agent": "yesha-astrbot-novel/2.0 (https://github.com/yesha)"}
        data = None
        last: Exception | None = None
        for attempt in range(2):
            for proxy in (PROXY, None):
                try:
                    async with self._client(proxy, timeout=25.0) as cli:
                        resp = await cli.get(url, headers=headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    last = RuntimeError(f"HTTP {resp.status_code}")
                except Exception as exc:  # noqa: BLE001
                    last = exc
            if data:
                break
            await asyncio.sleep(0.5)
        if not data:
            raise RuntimeError(f"Bangumi 日历抓取失败：{last!r}"[:160])
        weekday_cn = "一二三四五六日"[datetime.now().weekday()]
        day = next((d for d in data if weekday_cn in str((d.get("weekday") or {}).get("cn", ""))), None)
        if day is None:
            day = data[datetime.now().weekday() % len(data)]
        items: list[dict] = []
        for it in day.get("items") or []:
            imgs = it.get("images") or {}
            rating = (it.get("rating") or {}).get("score") or 0
            items.append(
                {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "title": it.get("name_cn") or it.get("name") or "",
                    "ep": f"⭐{rating}｜{it.get('air_date') or ''}".strip("｜"),
                    "link": it.get("url") or f"https://bgm.tv/subject/{it.get('id')}",
                    "img": imgs.get("large") or imgs.get("common") or "",
                    "views": "",
                    "_bgm_id": it.get("id"),
                    "_name_cn": it.get("name_cn") or "",
                    "_name": it.get("name") or "",
                }
            )
        logger.info("[bangumi] Bangumi 日历：周%s 共 %s 部", weekday_cn, len(items))
        return items

    # 默认优先级（play_order 可覆盖）：吐槽 → agedm → akianime → girigiri → acfun → bilibili → tvtfun（保险）
    PLAY_ORDER = ("tucao", "agedm", "akianime", "girigiri", "acfun", "bilibili", "tvtfun")

    def _play_order(self) -> tuple[str, ...]:
        raw = str(self._opt("play_order", "") or "").strip()
        if not raw:
            return self.PLAY_ORDER
        parts = tuple(p.strip().lower() for p in re.split(r"[,，>\s]+", raw) if p.strip())
        return parts or self.PLAY_ORDER

    def _pick_play(self, item: dict) -> str:
        """每条只给一个播放链接，按 play_order 优先级取第一个可用的。"""
        plays = item.get("plays") or {}
        for site in self._play_order():
            if plays.get(site):
                return plays[site]
        for url in plays.values():
            if url:
                return url
        return ""

    def _browser_limit(self) -> int:
        try:
            return max(0, min(int(self._opt("play_link_browser_items", 4)), 12))
        except Exception:
            return 4

    def _play_limit(self) -> int:
        try:
            return max(0, min(int(self._opt("play_link_items", 8)), 25))
        except Exception:
            return 8

    # ---------------------------------------------------------------- 封面图
    async def _download_img(self, url: str, idx: int) -> Path | None:
        if not url:
            return None
        if url.startswith("//"):
            url = "https:" + url
        for proxy in (None, PROXY):
            try:
                async with self._client(proxy, timeout=40.0) as cli:
                    resp = await cli.get(url)
                if resp.status_code == 200 and len(resp.content) > 2048:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    suffix = Path(url.split("?")[0]).suffix or ".jpg"
                    if len(suffix) > 5:
                        suffix = ".jpg"
                    out = CACHE_DIR / f"bgm_{idx}.jpg"
                    out.write_bytes(resp.content)
                    return out
            except Exception:  # noqa: BLE001
                continue
        return None

    def _cover_limit(self) -> int:
        try:
            return max(0, min(int(self._opt("cover_count", 6)), 15))
        except Exception:
            return 6

    def _batch_size(self) -> int:
        try:
            return max(1, min(int(self._opt("batch_size", 6)), 15))
        except Exception:
            return 6

    async def _cards(self, title: str, items: list[dict], start_no: int = 1, with_covers: bool = True) -> MessageChain:
        """一条消息：抬头 + 若干「编号 标题 / 链接 / 封面图」。"""
        chain: list = [Comp.Plain(title)]
        cover_n = self._cover_limit()
        for offset, it in enumerate(items):
            idx = start_no + offset
            ep = f"｜{it['ep']}" if it.get("ep") else ""
            play = self._pick_play(it)
            head = f"\n{idx}. {it['title']}{ep}"
            link = it.get("link", "") or ""
            if link and "bgm.tv" not in link:
                head += f"\n{link}"
            if play:
                head += f"\n▶ {play}"
            chain.append(Comp.Plain(head))
            if with_covers and offset < cover_n:
                path = await self._download_img(it.get("img", ""), idx)
                if path is not None:
                    chain.append(Comp.Image(file=str(path)))
        return chain

    async def _batched_chains(self, header: str, items: list[dict], limit: int, with_covers: bool = True, covers_first_only: bool = False) -> list[MessageChain]:
        """合并转发（聊天记录）：每部一个节点，节点内「文字(标题+链接+播放) + 封面图」。"""
        picked = items if limit <= 0 else items[:limit]
        if not picked:
            return [MessageChain([Comp.Plain("今天没有抓到更新的番～")])]
        cover_n = self._cover_limit() if with_covers else 0
        nodes: list = [Comp.Node([Comp.Plain(f"{header}（{len(picked)} 部）")])]
        for idx, it in enumerate(picked, 1):
            head = f"{idx}. {it.get('title','')}"
            if it.get("ep"):
                head += f"｜{it['ep']}"
            link = it.get("link", "") or ""
            if link and "bgm.tv" not in link:
                head += f"\n{link}"
            play = self._pick_play(it)
            if play:
                head += f"\n▶ {play}"
            comps: list = [Comp.Plain(head)]
            if idx <= cover_n:
                path = await self._download_img(it.get("img", ""), idx)
                if path is not None:
                    comps.append(Comp.Image(file=str(path)))
            nodes.append(Comp.Node(comps))
        return [MessageChain([Comp.Nodes(nodes=nodes)])]

    # ---------------------------------------------------------------- 文本渲染
    @staticmethod
    def _fmt_week(items: list[dict], limit: int) -> str:
        if not items:
            return "本周番组没抓到，站点可能抽风～"
        by_date: dict[str, list[dict]] = {}
        for it in items:
            by_date.setdefault(it["date"], []).append(it)
        today = datetime.now().strftime("%Y-%m-%d")
        lines = ["📺 本周新番表（吐槽弹幕网）"]
        count = 0
        for date in sorted(by_date):
            day_items = by_date[date]
            weekday = "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
            mark = " ← 今天" if date == today else ""
            lines.append(f"\n▍{date[5:]} {weekday}{mark}")
            for it in day_items:
                if count >= limit:
                    lines.append("…（还有更多，发「新番 30」看全）")
                    return "\n".join(lines)
                ep = f"｜{it['ep']}" if it["ep"] else ""
                lines.append(f"· {it['title']}{ep}")
                count += 1
        return "\n".join(lines)

    @staticmethod
    def _fmt_today(items: list[dict], limit: int) -> str:
        if not items:
            return "今天好像没有更新（或者站点没抓到）～"
        lines = [f"🎬 今天更新（{len(items)} 部）"]
        for it in items[:limit]:
            ep = f"｜{it['ep']}" if it["ep"] else ""
            lines.append(f"· {it['title']}{ep}")
            lines.append(f"  {it['link']}")
        if len(items) > limit:
            lines.append(f"…还有 {len(items) - limit} 部")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 命令
    @filter.command("新番")
    async def bangumi_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """今天更新的新番（每日追踪口径）。"""
        async for r in self.today_cmd(event, arg):
            yield r

    @filter.command("本周新番")
    async def bangumi_cmd2(self, event: AstrMessageEvent, arg: str = ""):
        """本周番组表（想看整周时才用）。"""
        limit = self.max_items
        if str(arg).strip().isdigit():
            n = int(str(arg).strip())
            limit = 0 if n == 0 else max(3, min(n, 200))
        try:
            items = await self._source_items("week")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[bangumi] 周表抓取失败：%r", exc)
            yield event.plain_result("番组表没抓到，稍后再试～")
            return
        # 整周条目多：默认不带封面（避免几十条图片消息）；week_covers>0 时只给第一条带图
        try:
            week_covers = max(0, min(int(self._opt("week_covers", 0)), 10))
        except Exception:
            week_covers = 0
        for chain in await self._batched_chains(
            "📺 本周新番（bgm + 吐槽 去重）",
            items,
            limit or 0,
            with_covers=week_covers > 0,
            covers_first_only=True,
        ):
            yield event.chain_result(chain)
            await asyncio.sleep(1.0)

    @filter.command("今天新番")
    async def today_cmd(self, event: AstrMessageEvent, arg: str = ""):
        """今天更新的番。"""
        try:
            items = await self._source_items("today")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[bangumi] 今日更新抓取失败：%r", exc)
            yield event.plain_result("今日更新没抓到，稍后再试～")
            return
        for chain in await self._batched_chains("🎬 今天更新", items, self.max_items):
            yield event.chain_result(chain)
            await asyncio.sleep(1.2)

    @filter.command("今日新番")
    async def today_cmd2(self, event: AstrMessageEvent, arg: str = ""):
        async for r in self.today_cmd(event, arg):
            yield r

    # ---------------------------------------------------------------- 定时推送
    async def initialize(self):
        if str(self._opt("push_enabled", "false")).strip().lower() not in ("true", "1", "on", "yes"):
            return
        self._task = asyncio.create_task(self._push_loop())
        logger.info("[bangumi] 每日推送已启动：%s", self._opt("push_time", "20:00"))

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()

    def _targets(self) -> list[str]:
        raw = self._opt("push_targets", OWNER_UMO)
        if isinstance(raw, str):
            parts = [p.strip() for p in re.split(r"[,，\s]+", raw) if p.strip()]
        elif isinstance(raw, list):
            parts = [str(p).strip() for p in raw]
        else:
            parts = []
        return [OWNER_UMO if p in ("owner", "主人") else p for p in parts] or [OWNER_UMO]

    async def _push_loop(self) -> None:
        while True:
            try:
                hh, _, mm = str(self._opt("push_time", "20:00")).partition(":")
                target = datetime.now().replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
                if target <= datetime.now():
                    target += timedelta(days=1)
                wait = (target - datetime.now()).total_seconds()
                logger.info("[bangumi] 下次推送：%s（%.0f 分钟后）", target.strftime("%m-%d %H:%M"), wait / 60)
                await asyncio.sleep(max(30, wait))
                await self._push_once()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[bangumi] 推送循环异常：%r", exc)
                await asyncio.sleep(300)

    async def _push_once(self) -> None:
        try:
            items = await self._source_items("today")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[bangumi] 推送抓取失败：%r", exc)
            return
        chains = await self._batched_chains("🎬 今天更新", items, self.max_items)
        for umo in self._targets():
            for chain in chains:
                try:
                    ok = await self.context.send_message(umo, chain)
                    logger.info("[bangumi] 推送 %s -> %s", umo, ok)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[bangumi] 推送失败 %s：%r", umo, exc)
                await asyncio.sleep(1.2)

    # ---------------------------------------------------------------- 群聊免唤醒入口
    @filter.regex(r"^(新番|今天新番|今日新番|本周新番)(\s+.*)?$")
    async def bangumi_group(self, event: AstrMessageEvent):
        """群聊直接发即可触发（RegexFilter 不受 wake_prefix 限制）。"""
        msg = (event.message_str or "").strip()
        head = msg.split()[0] if msg.split() else ""
        rest = msg[len(head):].strip()
        async for r in self.week_cmd(event):
            yield r
        event.stop_event()
