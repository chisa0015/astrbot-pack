"""日语回复锁 v3：只在「私聊 + 指定 QQ」时强制日语，群聊和其它人保持中文。

判定：
- 会话类型必须是 FriendMessage（私聊）
- 发送者必须在 JP_USER_IDS 里（默认只有主人 2899675402）

想给更多人开日语：把 QQ 号加进 JP_USER_IDS；想全员私聊日语：把 JP_PRIVATE_ALL 设 True。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

JP_USER_IDS: set[str] = set()   # 空集合 = 全中文（2026-10-29 佩丽卡切中文，日语锁停用）
JP_PRIVATE_ALL = False

RULE_SYS = (
    "\n\n【语言｜最高优先级】\n"
    "无论对方使用什么语言、无论上文任何设定如何描述（包括任何要求用简体中文、英文回答的规则），"
    "你的每一条回复都必须使用自然、口语化的日语（日本語）。\n"
    "禁止使用中文或英文作答；专有名词、人名、作品名可以保留原文。\n"
    "语气、口癖、称呼仍严格按角色设定来，只是把语言换成日语。\n"
)

RULE_USER = "\n\n（返答は必ず日本語で。中国語・英語での返答は禁止です。）"


@register(
    "astrbot_plugin_zz_ja_lock",
    "yesha",
    "只在私聊（指定 QQ）强制日语，群聊保持中文",
    "1.2.0",
    "",
)
class JaLockPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._hits = 0

    @filter.on_llm_request()
    async def force_japanese(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if event.get_message_type() != MessageType.FRIEND_MESSAGE:
                return  # 群聊：不注入，保持中文
            sender = str(event.get_sender_id() or "")
            if not JP_PRIVATE_ALL and sender not in JP_USER_IDS:
                return  # 非主人私聊：保持中文

            prompt = getattr(req, "system_prompt", None) or ""
            if RULE_SYS not in prompt:
                req.system_prompt = prompt + RULE_SYS

            user_text = getattr(req, "prompt", None)
            if isinstance(user_text, str) and user_text.strip() and RULE_USER not in user_text:
                req.prompt = user_text + RULE_USER

            self._hits += 1
            if self._hits <= 5 or self._hits % 20 == 0:
                logger.info("[ja_lock] 已注入日语规则（第 %s 次，私聊 %s）", self._hits, sender)
        except Exception:
            logger.exception("[ja_lock] 注入日语规则失败")
