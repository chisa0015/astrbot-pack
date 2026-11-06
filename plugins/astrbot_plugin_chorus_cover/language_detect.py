"""按歌曲语言挑 RVC 音色模型（0.13.0）。

只从音乐层面判断语言：先看歌词的字形占比，没有歌词再看歌名和歌手。
模型文件名/别名一律不参与判断——名字里带「日」「日语」的模型不会被当成证据。

结果按 ``来源:歌曲ID`` 缓存在插件数据目录的 ``language_cache.json`` 里，
同一首歌不重复请求歌词；缓存过期或读不出来就当没命中，重新检测一次。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

# 字形统计用的正则
KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")

# LRC 时间轴、编曲/制作信息这类行对判断语言没用，先剔掉
LRC_TAG_RE = re.compile(r"\[[^\]]*\]")

# 明确是纯音乐/伴奏的标记，见到了就不猜语言
INSTRUMENTAL_HINTS = ("纯音乐", "请欣赏", "纯伴奏", "instrumental", "no vocal")

LANG_NAMES = {"zh": "中文", "ja": "日语", "en": "英文"}

CACHE_TTL_SECONDS = 30 * 24 * 3600
CACHE_FILE_NAME = "language_cache.json"


def lang_name(lang: str) -> str:
    return LANG_NAMES.get(lang, lang or "未知")


def _read_cache(cache_path: str) -> Dict[str, Any]:
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(cache_path: str, data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, cache_path)
    except OSError as error:
        logger.debug(f"[语言判定] 缓存写入失败（不影响翻唱）: {error}")


def _strip_lyric(raw: str) -> str:
    """去掉 LRC 时间轴和制作人员行，只留正文。"""
    lines = []
    for line in str(raw or "").splitlines():
        cleaned = LRC_TAG_RE.sub("", line).strip()
        if not cleaned:
            continue
        if cleaned.startswith(("作词", "作曲", "编曲", "制作", "混音", "母带", "出品")):
            continue
        lines.append(cleaned)
    return "\n".join(lines)


def _fetch_lyric(song_id: Any, bases: List[str], timeout: float) -> str:
    """按顺序问几个网易云接口要歌词，拿到就返回正文。"""
    song_id = str(song_id or "").strip()
    if not song_id:
        return ""
    for base in bases:
        base = str(base or "").strip().rstrip("/")
        if not base:
            continue
        url = f"{base}/lyric?id={urllib.parse.quote(song_id)}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
        except Exception as error:  # 网络问题不该影响翻唱，直接换下一个源
            logger.debug(f"[语言判定] 取歌词失败 base={base}: {error}")
            continue
        lyric = ""
        if isinstance(payload, dict):
            lrc = payload.get("lrc")
            if isinstance(lrc, dict):
                lyric = str(lrc.get("lyric") or "")
            if not lyric:
                lyric = str(payload.get("lyric") or "")
        lyric = _strip_lyric(lyric)
        if lyric:
            return lyric
    return ""


def _score(text: str) -> Tuple[str, str]:
    """按字形占比判断语言，返回 (lang, evidence)。判不出来返回空串。"""
    text = str(text or "").strip()
    if not text:
        return "", "empty"
    lowered = text.lower()
    if any(hint in lowered for hint in INSTRUMENTAL_HINTS) and len(text) < 60:
        return "", "instrumental"

    kana = len(KANA_RE.findall(text))
    han = len(HAN_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    evidence = f"kana={kana},han={han},latin={latin}"

    # 假名是日语最硬的证据（中文歌里基本不会成片出现）
    if kana >= 2 and (kana + han) > 0 and kana / (kana + han) >= 0.02:
        return "ja", evidence
    # 拉丁字母明显压过汉字才算英文歌：英文歌词里偶尔夹几个中文署名/译名，
    # 不能因为出现几个汉字就判成中文（Shape of You 就踩过这个坑）。
    if latin and han:
        if latin > han * 2:
            return "en", evidence
        if han >= 2 and han >= latin:
            return "zh", evidence
    if latin >= 8 and han == 0:
        return "en", evidence
    if han >= 2 and latin == 0:
        return "zh", evidence
    if han > 0 and latin > 0:
        return ("zh" if han >= latin else "en"), evidence
    if latin >= 4:
        return "en", evidence
    return "", evidence


async def detect_language(
    song: Dict[str, Any],
    cache_path: str,
    lyric_bases: Optional[List[str]] = None,
    timeout: float = 8.0,
) -> Tuple[str, str]:
    """判断这首歌是什么语言，返回 (lang, evidence)。

    ``lang`` 是 ``zh`` / ``ja`` / ``en``；判不出来（纯音乐、没歌词也没线索）时为空串，
    调用方按「不猜」处理，回落到默认音色。
    """
    song = song or {}
    key = f"{song.get('source') or '-'}:{song.get('id') or '-'}"
    now = time.time()

    cache = _read_cache(cache_path)
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("lang"):
        age = now - float(cached.get("ts") or 0)
        if 0 <= age < CACHE_TTL_SECONDS:
            return str(cached["lang"]), str(cached.get("evidence") or "cache")

    lyric = ""
    if song.get("source") in (None, "", "netease"):
        lyric = await asyncio.to_thread(
            _fetch_lyric, song.get("id"), list(lyric_bases or []), timeout
        )

    if lyric:
        lang, evidence = _score(lyric)
        evidence = f"lyrics({evidence})"
    else:
        lang, evidence = _score(f"{song.get('name') or ''} {song.get('artists') or ''}")
        evidence = f"title({evidence})"

    if lang:
        cache[key] = {"lang": lang, "evidence": evidence, "ts": now}
        # 顺手清掉过期项，别让缓存无限长
        stale = [k for k, v in cache.items() if now - float((v or {}).get("ts") or 0) > CACHE_TTL_SECONDS]
        for stale_key in stale:
            cache.pop(stale_key, None)
        await asyncio.to_thread(_write_cache, cache_path, cache)
    return lang, evidence
