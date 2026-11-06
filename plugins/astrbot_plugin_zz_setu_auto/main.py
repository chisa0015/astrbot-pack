"""色图自动应答：消息里只要出现「色图/涩图/瑟图」，就丢一张。

设计取舍：
  · 用 filter.regex 挂在「包含即触发」，不用 filter.command —— 群友不会乖乖打斜杠命令；
  · 每个会话独立冷却（默认 15 秒），否则一句「色图」能刷满屏；
  · 图源走 lolicon v2，不依赖别的插件内部对象（跨插件拿 handler 太脆）；
  · 拿到图先落地成文件再发（直接发 URL 有可能被 QQ 侧防盗链掐掉）；
  · 触发后 stop_event()，免得 LLM 又跟着回一句，变成「图 + 废话」两条。
"""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API = "https://api.lolicon.app/setu/v2"
PROXY_PORTS = [7890, 7892, 7897, 7891, 7893, 1080, 10808]
IMG_DIR = Path(__file__).resolve().parent / "images"


@register("zz_setu_auto", "夜煞", "消息含「色图」就自动丢一张", "1.0.0")
class SetuAuto(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._config = config
        self._last: dict[str, float] = {}
        self._proxy_cache: tuple[str, float] = ("", 0.0)
        try:
            IMG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 配置
    def _opt(self, key: str, default=""):
        cfg = self._config
        if cfg is None:
            return default
        try:
            value = cfg.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value in (None, "") else value

    def _keywords(self) -> list[str]:
        raw = str(self._opt("keywords", "色图,涩图,瑟图"))
        return [word.strip() for word in raw.replace("，", ",").split(",") if word.strip()]

    # ---------------------------------------------------------------- 代理
    def _port_ok(self, port: int, host: str = "api.lolicon.app", timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
                data = sock.recv(64)
                return b" 200" in data
        except Exception:  # noqa: BLE001
            return False

    def _proxy(self) -> str:
        url, at = self._proxy_cache
        if url and time.time() - at < 120:
            return url
        for port in PROXY_PORTS:
            if self._port_ok(port):
                self._proxy_cache = (f"http://127.0.0.1:{port}", time.time())
                return self._proxy_cache[0]
        self._proxy_cache = ("", time.time())
        return ""

    # ---------------------------------------------------------------- 抓图
    def _fetch_blocking(self, r18: int) -> str:
        """返回本地图片路径；失败抛异常。同步实现，调用方丢线程里跑。

        尺寸策略（实测这台机的 pixiv 线路）：
            small(540px, ~36KB) 约 4 秒 ；regular(~600KB) 要 40~60 秒 ；原图 1MB+ 更久。
        所以默认走 small，只有 small 拿不到才升级到 regular —— 群里喊一声要图，等 4 秒和等 1 分钟是两个体验。
        """
        proxy = self._proxy()
        headers = {"User-Agent": UA, "Referer": "https://www.pixiv.net/"}
        size_chain = [str(self._opt("size", "small") or "small")]
        if "regular" not in size_chain:
            size_chain.append("regular")

        last_error = ""
        for size in size_chain:
            try:
                with httpx.Client(timeout=20, proxy=proxy or None, trust_env=False, verify=False,
                                  headers={"User-Agent": UA}) as client:
                    resp = client.get(API, params={"r18": int(r18), "num": 1, "size": size, "proxy": "i.pixiv.re"})
                    resp.raise_for_status()
                    payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                last_error = f"图源接口不通（{exc}）"
                continue

            items = payload.get("data") or []
            if not items:
                last_error = "图源返回空数据"
                continue
            urls = items[0].get("urls") or {}
            url = next((value for value in urls.values() if value), "")
            if not url:
                last_error = "图源没给地址"
                continue

            for use_proxy in ([proxy, None] if proxy else [None]):
                try:
                    with httpx.Client(timeout=25, proxy=use_proxy or None, trust_env=False,
                                      verify=False, follow_redirects=True, headers=headers) as client:
                        got = client.get(url)
                    if got.status_code == 200 and len(got.content) > 4096:
                        suffix = ".png" if got.content[:4] == b"\x89PNG" else ".jpg"
                        IMG_DIR.mkdir(parents=True, exist_ok=True)
                        out = IMG_DIR / f"setu-{int(time.time() * 1000)}{suffix}"
                        out.write_bytes(got.content)
                        return str(out)
                    last_error = f"HTTP {got.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
        raise RuntimeError(f"图下载失败（{last_error}）")

    # ---------------------------------------------------------------- 借用瑟瑟插件的图源
    def _find_setu_handler(self):
        """拿到 astrbot_plugin_setu 的 SetuCommandHandler（它管图源、r18、发送缓存）。

        为什么要借：那个插件的图源/分级/去重/撤回策略都在它自己的配置里，
        自己再写一份只会两边参数漂移。这里只负责「什么时候发」，发什么归它管。
        """
        try:
            from astrbot.core.star.star import star_map
        except Exception:  # noqa: BLE001
            return None
        for meta in list(star_map.values()):
            name = str(getattr(meta, "name", "") or "")
            dir_name = str(getattr(meta, "root_dir_name", "") or "")
            if name != "astrbot_plugin_setu" and dir_name != "astrbot_plugin_setu":
                continue
            module = getattr(meta, "module", None)
            handler = getattr(module, "_setu_handler", None)
            if handler is not None and hasattr(handler, "get_random_picture"):
                return handler
        return None

    # ---------------------------------------------------------------- 入口
    @filter.regex(r".*(色图|涩图|瑟图).*")
    async def on_message(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        words = self._keywords()
        if not any(word in text for word in words):
            return
        # 自己发的、纯图片消息不算
        if not text:
            return

        key = str(getattr(event, "unified_msg_origin", "") or event.get_session_id())
        now = time.time()
        cooldown = max(0, int(self._opt("cooldown_sec", 15) or 0))
        last = self._last.get(key, 0.0)
        if cooldown and now - last < cooldown:
            event.stop_event()
            return
        self._last[key] = now

        # 首选：把「发什么」交给瑟瑟插件（图源、r18、缓存全按它的配置走）
        if str(self._opt("source", "setu_plugin")) != "builtin":
            handler = self._find_setu_handler()
            if handler is not None:
                try:
                    async for result in handler.get_random_picture(event):
                        yield result
                    event.stop_event()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[setu-auto] 瑟瑟插件那条路失败，退回内置图源：{exc}")

        r18 = int(self._opt("r18", 0) or 0)
        try:
            path = await asyncio.to_thread(self._fetch_blocking, r18)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[setu-auto] 没抓到图：{exc}")
            yield event.plain_result("图源这会儿不给货，过一会儿再喊")
            event.stop_event()
            return

        caption = str(self._opt("caption", "拿去，别催")).strip()
        chain = []
        if caption:
            chain.append(Plain(caption))
        chain.append(Image.fromFileSystem(path))
        yield event.chain_result(chain)
        event.stop_event()

        # 顺手清理：目录里只留最近 30 张，别把磁盘塞满
        try:
            files = sorted(IMG_DIR.glob("setu-*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in files[30:]:
                stale.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
