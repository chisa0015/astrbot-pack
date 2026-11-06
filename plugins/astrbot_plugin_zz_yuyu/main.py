"""鱼鱼找鸡（新引擎）：AstrBot 侧封装。

引擎不在这里重写 —— 反查的脏活全在 DSH 那个包里：
    D:\\DSK\\BOT\\dsh-yuyu-find-chicken\\bin\\run.mjs
它一次跑完 lens(谷歌 Lens，认真人) + yandex(找原帖/补位)，输出一行 JSON。
本插件只做四件事：取图 → 起子进程跑引擎 → 把 JSON 拼成人话 → 发回群里。

为什么要调子进程而不是在本进程里实现：
  · 那套跑法要带 HTTPS_PROXY 和不带代理的浏览器两条通道，Node 的 --use-env-proxy 一次搞定；
  · 崩也崩子进程，不拖 AstrBot；
  · 换引擎/调参数只改那边，这里一个字不用动。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Reply

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 引擎包位置（DSH 侧那一套）
PLUGIN_ROOT = Path(r"D:\DSK\BOT\dsh-yuyu-find-chicken")
RUNNER = PLUGIN_ROOT / "bin" / "run.mjs"
VENDOR = PLUGIN_ROOT / "vendor" / "coser-image-search"

# node 可执行文件候选：优先真 node（Electron 壳不能直接跑 .mjs）
NODE_CANDIDATES = [
    os.environ.get("YUYU_NODE", ""),
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]

PROXY_PORTS = [7890, 7892, 7897, 7891, 7893, 1080, 10808, 10090]

ARTWORK_PLATFORMS = {"pixiv"}


@register("zz_yuyu", "夜煞", "鱼鱼找鸡（Lens+Yandex 新引擎）", "1.0.0")
class YuyuFindChicken(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._config = config
        self._proxy_cache: tuple[str, float] = ("", 0.0)

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

    # ---------------------------------------------------------------- 代理
    def _tls_ok(self, port: int, host: str = "lens.google.com", timeout: float = 4.0) -> bool:
        """代理端口光看 CONNECT 200 不够（坏掉的 VPN 也回 200 然后 reset），要真握一次 TLS。"""
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
            sock.settimeout(timeout)
            sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            buf = b""
            while b"\r\n" not in buf:
                chunk = sock.recv(1024)
                if not chunk:
                    return False
                buf += chunk
                if len(buf) > 4096:
                    break
            line = buf.split(b"\r\n", 1)[0].decode("latin1", "ignore")
            if " 200" not in line:
                return False
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            tls = ctx.wrap_socket(sock, server_hostname=host)
            tls.close()
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:  # noqa: BLE001
                pass

    def _resolve_proxy(self) -> str:
        """返回可用代理 URL；都不通就返回空串（直连）。结果缓存 60 秒。"""
        cached, at = self._proxy_cache
        if cached and time.time() - at < 60:
            return cached
        want = str(self._opt("proxy", "auto")).strip()
        candidates: list[str] = []
        if want and want.lower() != "auto":
            candidates.append(want)
        candidates.extend(f"http://127.0.0.1:{port}" for port in PROXY_PORTS)
        for url in dict.fromkeys(candidates):
            try:
                port = int(url.rsplit(":", 1)[-1].strip("/"))
            except Exception:  # noqa: BLE001
                continue
            if self._tls_ok(port):
                logger.info(f"[yuyu] 代理可用：{url}")
                self._proxy_cache = (url, time.time())
                return url
        self._proxy_cache = ("", time.time())
        return ""

    # ---------------------------------------------------------------- 取图
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
                data = Path(str(target.file)).read_bytes()
                if data:
                    return data
        except Exception:  # noqa: BLE001
            pass
        url = getattr(target, "url", "") or (f"file://{target.file}" if getattr(target, "file", "") else "")
        if not url:
            return None
        proxy = self._resolve_proxy()
        for pr in ([proxy, None] if proxy else [None]):
            try:
                async with httpx.AsyncClient(
                    timeout=30, verify=False, proxy=pr, trust_env=False,
                    follow_redirects=True, headers={"User-Agent": UA},
                ) as cli:
                    resp = await cli.get(url)
                if resp.status_code == 200 and len(resp.content) > 2000:
                    return resp.content
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---------------------------------------------------------------- 跑引擎
    def _node_bin(self) -> str:
        for candidate in NODE_CANDIDATES:
            if candidate and Path(candidate).exists():
                return candidate
        return "node"

    def _run_engine(self, img: bytes) -> dict:
        if not RUNNER.exists():
            return {"error": f"找不到引擎入口：{RUNNER}"}
        suffix = ".png" if img[:4] == b"\x89PNG" else ".jpg"
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(PLUGIN_ROOT)) as fp:
                fp.write(img)
                tmp_path = fp.name
            engines = str(self._opt("engines", "lens,yandex"))
            timeout_sec = int(self._opt("timeout_sec", 60) or 60)
            max_accounts = int(self._opt("max_accounts", 3) or 3)
            proxy = self._resolve_proxy()
            env = os.environ.copy()
            env.update({"NODE_USE_ENV_PROXY": "1", "NO_PROXY": "localhost,127.0.0.1,::1", "NO_COLOR": "1"})
            if proxy:
                env["HTTPS_PROXY"] = env["HTTP_PROXY"] = env["ALL_PROXY"] = proxy
            else:
                for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
                    env.pop(key, None)
            cmd = [
                self._node_bin(), "--use-env-proxy", str(RUNNER),
                "--vendor", str(VENDOR),
                "--image", tmp_path,
                "--engines", engines,
                "--timeout", str(timeout_sec),
                "--max", str(max_accounts),
                "--strict", "true",
                "--proxy", proxy or "",
            ]
            budget = min(420, max(150, int(timeout_sec * 2.5) + 30))
            started = time.time()
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=budget, env=env, cwd=str(PLUGIN_ROOT),
            )
            out = (proc.stdout or "").strip()
            if not out:
                return {"error": f"引擎没输出（exit={proc.returncode}）：{(proc.stderr or '')[:200]}"}
            line = out.splitlines()[-1]
            data = json.loads(line)
            data["_elapsed"] = round(time.time() - started, 1)
            return data
        except subprocess.TimeoutExpired:
            return {"error": "查图超时（引擎卡住了，稍后再试）"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"引擎调用失败：{exc}"}
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------------- 拼话术
    def _compose(self, data: dict) -> str:
        if data.get("error"):
            return f"❌ {data['error']}"
        accounts = list(data.get("accountsStrict") or [])
        text = str(data.get("textStrict") or "")
        # 严格那套只剩画作平台、放宽那套有真社交号 → 用社交号（跟 DSH 侧同一套兜底逻辑）
        if not [a for a in accounts if a.get("platform") not in ARTWORK_PLATFORMS]:
            relaxed = list(data.get("accountsRelaxed") or [])
            if [a for a in relaxed if a.get("platform") not in ARTWORK_PLATFORMS]:
                accounts = relaxed
                text = str(data.get("textRelaxed") or text)
        top = str(data.get("yandexTop") or "").strip()
        if accounts and top:
            text = f"{text}\n\n· 出处线索：{top}"
        if not text.strip():
            text = "❌ 这次没查到结果，等一会儿再发一次（谷歌有频率限制）"
        used = "、".join(data.get("enginesUsed") or []) or "无"
        elapsed = data.get("_elapsed")
        tail = f"\n\n（引擎 {used}"
        if elapsed:
            tail += f" · {elapsed}s"
        tail += "）"
        return text.rstrip() + tail

    async def _run(self, event: AstrMessageEvent):
        ack = str(self._opt("ack_text", "收到，开始找鸡"))
        engines = str(self._opt("engines", "lens,yandex"))
        if "lens" in engines:
            ack = f"{ack}（走谷歌识图 + Yandex，约 1 分钟）"
        yield event.plain_result(ack)

        img = await self._image_bytes(event)
        if not img:
            yield event.plain_result("没拿到图片：把图带上，或者引用一条带图的消息再发「找鸡」")
            return
        logger.info(f"[yuyu] 收到图片 {len(img) / 1024:.0f}KB，开始反查")
        data = await asyncio.to_thread(self._run_engine, img)
        yield event.plain_result(self._compose(data))

    # ---------------------------------------------------------------- 入口
    @filter.command("鱼鱼找鸡")
    async def cmd_full(self, event: AstrMessageEvent):
        async for result in self._run(event):
            yield result

    @filter.command("找鸡")
    async def cmd_find(self, event: AstrMessageEvent):
        async for result in self._run(event):
            yield result

    @filter.command("认图")
    async def cmd_recognize(self, event: AstrMessageEvent):
        async for result in self._run(event):
            yield result

    @filter.command("这是谁")
    async def cmd_who(self, event: AstrMessageEvent):
        async for result in self._run(event):
            yield result

    @filter.regex(r"^(鱼鱼找鸡|找鸡|认图|这是谁|这谁)$")
    async def group_trigger(self, event: AstrMessageEvent):
        """群里免唤醒前缀：直接发「找鸡」也触发。"""
        async for result in self._run(event):
            yield result
        event.stop_event()
