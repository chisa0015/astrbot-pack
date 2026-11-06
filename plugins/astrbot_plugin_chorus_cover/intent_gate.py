"""点歌 / 翻唱需求判定闸门（本插件唯一词表来源）。

为什么要有这个文件：

    点歌插件（astrbot_plugin_music）的命令匹配要求「点歌」出现在命令位置。
    像「嗯。点歌。失眠飞行。」这种把点歌夹在句子中间的消息不会命中命令，
    会落到大模型手里；模型看到 smart_cover 这类翻唱工具，很容易顺手调用，
    把「点歌」变成「翻唱」。反过来，「点歌 晴天」偶尔也会因为命令没命中而
    被翻唱入口接走。

    这里把判定收成一处：消息里出现点歌类词、且没有翻唱类词，就判定为
    「点歌需求」，所有翻唱入口（LLM 工具 / 本地音频自动触发）一律拒绝执行，
    只把事实回给模型，由模型引导用户用点歌命令（或直接调用点歌插件的
    play_song_by_name 工具）。

以后加词/删词只改下面两个 TICKET_WORDS_* 常量。本模块只依赖标准库，
不 import astrbot，方便用假 event 单独跑判定测试。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# === 翻唱类词 ===
# 出现任意一个，就认为用户确实想「唱」，不再按点歌拦截。
# 「唱」一个字已经覆盖「翻唱」「唱一首」「用三月七唱」等写法；
# 「翻唱」「翻唱成」重复列出只为让词表一眼看懂，不影响结果。
TICKET_WORDS_COVER: tuple[str, ...] = (
    "唱",
    "翻唱",
    "翻唱成",
    "变声",
    "cover",
)

# === 点歌类词 ===
# 出现任意一个、且没有翻唱类词时，判定为点歌需求。
TICKET_WORDS_MUSIC: tuple[str, ...] = (
    "点歌",
    "放歌",
    "听歌",
    "来一首",
    "放一首",
    "播一首",
    # 下面是把「一」省掉或换成「首」的常见说法（如「给我放首歌」）
    "点一首",
    "点首歌",
    "来首歌",
    "放首歌",
    "播首歌",
)

# 词表之外再补两条正则，兜住「放首音乐」这类不常见的省略写法。
TICKET_PATTERNS_MUSIC: tuple[str, ...] = (
    r"(点|放|播|来)(一)?首?歌",
    r"(点|放|播|来)(一)?首(曲|音乐)",
)

# 「用…唱」本来就含「唱」，单列一条是为了把这条口语写法固定下来。
TICKET_PATTERNS_COVER: tuple[str, ...] = (
    r"用.{1,16}唱",
)

# 判定为点歌需求时回给模型的话。这是工具返回值（事实+指令），
# 不是直接发给用户的聊天回复；最终怎么说由人格模型组织。
MUSIC_REQUEST_HINT = (
    "这是点歌需求，不是翻唱：用户想听原曲，不要执行翻唱，也不要再调用其它翻唱工具。"
    "请让用户用「点歌 歌名」命令点原曲；如果 play_song_by_name 工具可用，也可以直接用它播放原曲。"
)


def _normalize(text: Any) -> str:
    """统一成小写字符串；None、非字符串一律当空串。"""
    return str(text or "").casefold()


def has_music_ticket(text: str) -> bool:
    """文本里是否出现点歌类词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(word in normalized for word in TICKET_WORDS_MUSIC):
        return True
    return any(re.search(pattern, normalized) for pattern in TICKET_PATTERNS_MUSIC)


def has_cover_ticket(text: str) -> bool:
    """文本里是否出现翻唱类词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    if any(word in normalized for word in TICKET_WORDS_COVER):
        return True
    return any(re.search(pattern, normalized) for pattern in TICKET_PATTERNS_COVER)


def is_music_request(text: str) -> bool:
    """消息是不是点歌需求：有点歌词、且没有翻唱词。"""
    normalized = _normalize(text)
    if not normalized:
        return False
    return has_music_ticket(normalized) and not has_cover_ticket(normalized)


def message_text(event: Any) -> str:
    """从事件里取当前消息文本；取不到或不是字符串时返回空串（按放行处理）。"""
    try:
        return str(getattr(event, "message_str", "") or "")
    except Exception:
        return ""


def cover_refusal(event: Any) -> Optional[str]:
    """翻唱入口统一闸门：点歌需求返回提示句，其它情况返回 None 表示放行。"""
    return MUSIC_REQUEST_HINT if is_music_request(message_text(event)) else None
