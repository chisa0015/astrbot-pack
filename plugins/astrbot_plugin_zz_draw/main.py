"""生图 / 图生图：本地 ComfyUI（OpenAI 兼容接口）。

触发：
  · 生图 白发狐耳少女              → txt2img
  · 图生图 改成水彩风（带图/引用图） → img2img，参考图就是那条消息里的图
  · 生图 换成赛博朋克（带图）        → 同样走 img2img（有图就默认重绘）

后端在 D:\\DSK\\BOT\\IMG-API：两段式出图（hires fix）+ 手部加权词 + 手指畸形负面词，
这些都在那边做，本插件只管收图、抠提示词、调接口、发图。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Image, Plain, Reply

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
IMG_DIR = Path(__file__).resolve().parent / "images"

DRAW_WORDS = ["生图", "画图", "出图", "绘图", "画一张"]
I2I_WORDS = ["图生图", "以图生图", "改图", "重绘", "参考图", "换风格"]


@register("zz_draw", "夜煞", "生图/图生图：本地 ComfyUI 出图", "1.1.0")
class DrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._config = config
        self._last: dict[str, float] = {}
        try:
            IMG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _opt(self, key: str, default=""):
        cfg = self._config
        if cfg is None:
            return default
        try:
            value = cfg.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value in (None, "") else value

    def _draw_words(self) -> list[str]:
        raw = str(self._opt("keywords", "生图,画图,出图,绘图,画一张"))
        words = [w.strip() for w in raw.replace("，", ",").split(",") if w.strip()]
        return list(dict.fromkeys(words + I2I_WORDS))

    # ---------------------------------------------------------------- 提示词
    def _extract_prompt(self, text: str) -> str:
        prompt = text.strip()
        for word in sorted(self._draw_words(), key=len, reverse=True):
            prompt = prompt.replace(word, " ")
        prompt = re.sub(r"^[\s:：,，、/]+", "", prompt)
        prompt = re.sub(r"^(帮我|给我|请|麻烦|来一张|来张|画个|画一个|画下|改成|换成)+", "", prompt)
        prompt = re.sub(r"[\s:：,，、]+$", "", prompt)
        return prompt.strip()

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
                if target is not None:
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
        url = getattr(target, "url", "") or ""
        if not url:
            return None
        for proxy in (str(self._opt("proxy", "http://127.0.0.1:7890")) or None, None):
            try:
                async with httpx.AsyncClient(timeout=30, verify=False, proxy=proxy, trust_env=False,
                                             follow_redirects=True, headers={"User-Agent": UA}) as client:
                    resp = await client.get(url)
                if resp.status_code == 200 and len(resp.content) > 2000:
                    return resp.content
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---------------------------------------------------------------- 调接口
    def _call_blocking(self, prompt: str, ref: bytes | None) -> str:
        api = str(self._opt("api_base", "http://127.0.0.1:9868")).rstrip("/")
        timeout = int(self._opt("timeout_sec", 300) or 300)
        payload: dict = {
            "prompt": prompt,
            "n": 1,
            "size": str(self._opt("size", "768x1024")),
            "hires": bool(self._opt("hires", True)),
        }
        if ref:
            payload["image"] = base64.b64encode(ref).decode()
            payload["strength"] = float(self._opt("img2img_strength", 0.55) or 0.55)
        with httpx.Client(timeout=timeout, trust_env=False, headers={"User-Agent": UA}) as client:
            resp = client.post(f"{api}/v1/images/generations", json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"生图接口 HTTP {resp.status_code}：{resp.text[:140]}")
        data = (resp.json() or {}).get("data") or []
        if not data:
            raise RuntimeError("生图接口没给图")
        item = data[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            with httpx.Client(timeout=90, trust_env=False, headers={"User-Agent": UA}) as client:
                raw = client.get(item["url"]).content
        else:
            raise RuntimeError("生图接口返回里既没有 b64_json 也没有 url")
        if len(raw) < 8192:
            raise RuntimeError("图太小，估计生成失败了")
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if raw[:4] == b"\x89PNG" else ".jpg"
        out = IMG_DIR / f"draw-{int(time.time() * 1000)}{suffix}"
        out.write_bytes(raw)
        return str(out)

    # ---------------------------------------------------------------- 入口
    @filter.regex(r".*(生图|画图|出图|绘图|画一张|图生图|以图生图|改图|重绘).*")
    async def on_draw(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        words = self._draw_words()
        if not any(word in text for word in words):
            return

        ref = await self._image_bytes(event)
        prompt = self._extract_prompt(text)
        img2img = ref is not None
        if img2img and len(prompt) < 2:
            prompt = "keep the composition, improve details, anime style, high quality"
        if not prompt:
            yield event.plain_result("要画啥？说清楚点：生图 白发狐耳少女")
            event.stop_event()
            return
        prompt = prompt[:400]

        key = str(getattr(event, "unified_msg_origin", "") or event.get_session_id())
        now = time.time()
        cooldown = max(0, int(self._opt("cooldown_sec", 20) or 0))
        if cooldown and now - self._last.get(key, 0.0) < cooldown:
            event.stop_event()
            return
        self._last[key] = now

        mode = "图生图" if img2img else "生图"
        yield event.plain_result(f"{mode}：{prompt[:60]} …（本地 ComfyUI，约 30~90 秒）")
        try:
            path = await asyncio.to_thread(self._call_blocking, prompt, ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[draw] {mode}失败：{exc}")
            yield event.plain_result(f"没画出来：{exc}")
            event.stop_event()
            return

        yield event.chain_result([Plain(f"{mode}好了：{prompt[:40]}"), Image.fromFileSystem(path)])
        event.stop_event()

        try:
            files = sorted(IMG_DIR.glob("draw-*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in files[40:]:
                stale.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
