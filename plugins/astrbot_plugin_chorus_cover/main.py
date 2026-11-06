from pathlib import Path
import os
import re
import uuid
import time

import asyncio
import shutil
import json
import sys
import subprocess
from datetime import datetime
from typing import Optional, Dict, List, Any, Set
import traceback
from contextlib import asynccontextmanager
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Record, File
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Image as CompImage
from astrbot.api import logger
from functools import partial
from gradio_client import Client

from . import netease_fetch
from . import source_router
from . import language_detect
from . import model_library
from .selection import CoverSelectionUI
from .outbound import Outbound

#: 插件名（AstrBot 的数据目录按这个名字分：data/plugin_data/<plugin_name>）
PLUGIN_NAME = "astrbot_plugin_chorus_cover"


def _plugin_data_dir() -> str:
    """插件自己的持久化目录：``<AstrBot data>/plugin_data/astrbot_plugin_chorus_cover``。

    审核要求：用户数据不能放在 ``data/`` 根目录（会跟 AstrBot 自己的文件混在一起，
    迁移和审计都分不清），也不能写在插件代码目录（升级会被覆盖）。
    """
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        base = get_astrbot_plugin_data_path()
    except Exception:  # 兜底：脱离 AstrBot 跑单测时用相对路径
        base = os.path.join(os.getcwd(), "data", "plugin_data")
    path = os.path.join(base, PLUGIN_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _mask_url(url: str) -> str:
    """日志里只留 scheme://host/path，去掉查询串（里面常带签名和 token）。"""
    text = str(url or "").strip()
    if not text:
        return "-"
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(text)
        if not parts.scheme:
            return text[:80]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return text[:80]


def _temp_audio_dir() -> str:
    """临时音频目录：``<plugin_data>/temp_audio``（24 小时后自动清理）。"""
    path = os.path.join(_plugin_data_dir(), "temp_audio")
    os.makedirs(path, exist_ok=True)
    return path


from .intent_gate import cover_refusal, is_music_request, message_text as event_message_text

MODEL_ALIAS_SEPARATOR = "|||"

# 0.10.1：设置页删掉了一批历史遗留 / 重复 / 空转项，代码里保留固定默认值，
# 免得删键之后 .get 取不到值时行为漂移，也避免 KeyError。
# - default_model_index：与 rvc_default_model 下拉重复（一个序号一个名字），
#   统一回落到模型列表第 1 个；用户偏好里的字段也改名为 model_index。
FALLBACK_MODEL_INDEX = 1
# - msst_default_model：当前 chorus-mw 的分离模型由后端启动配置固定，
#   这个值只作为 /切换msst模型 的运行时状态，不再从设置页读。
MSST_DEFAULT_MODEL = "bs_roformer_ep_317_sdr_12.9755"


def _bounded_float(config: Any, key: str, default: float, low: float, high: float) -> float:
    """读一个浮点配置并夹到 [low, high]。

    设置页的输入不一定可信（老配置可能存成字符串、也可能被人手改成越界值），
    统一在这里兜底，免得一个坏值让整次翻唱失败。
    """
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        logger.warning(f"{key} 配置格式错误，使用默认值 {default}")
        value = float(default)
    clamped = max(float(low), min(float(high), value))
    if clamped != value:
        logger.warning(f"{key}={value} 超出 {low}~{high}，按 {clamped} 处理")
    return clamped


class _OutboundTarget:
    """把"主动发给指定会话"包装成 outbound 能用的最小 event 形状。

    统一出口 ``Outbound.say`` 只用到三样东西：``plain_result``、``send``、
    ``unified_msg_origin``。提醒管理员时当前事件不是管理员会话，又不能绕过
    出口直接 ``event.send``，所以在这里做一个薄适配层：消息照样由出口层组织
    语言，只是最终发到 ``context.send_message`` 指定的 session。
    """

    def __init__(self, context: Any, session: str, unified_msg_origin: str = ""):
        self._context = context
        self._session = str(session or "")
        self.unified_msg_origin = unified_msg_origin

    def plain_result(self, text: str) -> MessageEventResult:
        return MessageEventResult().message(str(text))

    async def send(self, result: Any) -> bool:
        session = self._session
        if not session:
            return False
        chain = result if isinstance(result, MessageChain) else MessageChain().message(str(result))
        try:
            sent = await self._context.send_message(session, chain)
        except Exception as exc:
            logger.warning(f"主动发送到会话 {session} 失败: {exc}")
            return False
        return sent is not False


@register(
    "astrbot_plugin_chorus_cover",
    "void-HaoNan",
    "RVC 副歌翻唱：取歌走多音源，翻唱只走 RVC；对话出口统一走模型、失败结果结构化",
    "0.19.6",
    "https://github.com/void-HaoNan/astrbot-plugin-chorus-cover",
)
class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 统一对话出口：进度/重试默认静音，失败只说一句话（台词由人格模型现编）
        self.outbound = Outbound(self)
        
        self.rvc_base_url = config.get("rvc_base_url", "http://127.0.0.1:3333/")
        # 搜歌/取歌用哪个平台由 source_order 决定（默认网易云 → 酷狗 → 酷我 → QQ 音乐）
        self.timeout = config.get("timeout", 30)
        self.selection_ui = CoverSelectionUI(
            buttons=bool(config.get("qq_official_buttons", True)),
            config_getter=context.get_config,
            cards=bool(config.get("qq_official_cards", True)),
        )
        
        self.rvc_models_keywords = config.get("rvc_models_keywords", [])
        # 0.11.0：音色模型文件夹。留空=用中间层默认目录；填了会拿去和中间层核对
        self.rvc_models_dir = str(config.get("rvc_models_dir", "") or "").strip()
        # 0.14.0：分类音色库根目录（中文/日语/未分类…），/同步音色库 用它自动建硬链接
        self.model_library_dir = str(config.get("model_library_dir", "") or "").strip()
        self._models_dir_info = {}
        self.enable_rvc = config.get("enable_rvc", True)

        self.inference_timeout = config.get("inference_timeout", 300)
        self.music_api_timeout = max(5, int(config.get("music_api_timeout", 20)))

        # === 网易云取歌（0.5.0：照搬点歌插件 astrbot_plugin_music）===
        self.netease_cookie = str(config.get("netease_cookie", "") or "").strip()
        try:
            quality = int(str(config.get("netease_quality", 320) or 320).strip() or 320)
        except (TypeError, ValueError):
            logger.warning("netease_quality 配置格式错误，使用默认值 320")
            quality = 320
        if quality not in (128, 192, 320):
            logger.warning(f"netease_quality={quality} 不在 128/192/320 之内，已按 320 处理")
            quality = 320
        self.netease_quality = quality
        # 取直链的两条路都来自点歌插件的平台配置：meting（netEase 平台）+ NodeJS 服务（netease_api 平台）
        raw_meting_base = str(config.get("netease_meting_base", "") or "").strip()
        self.netease_meting_base = (
            netease_fetch.normalize_meting_base(raw_meting_base)
            if raw_meting_base
            else netease_fetch.DEFAULT_METING_BASE
        )
        raw_api_base = str(config.get("netease_api_base", "") or "").strip()
        self.netease_api_base = netease_fetch.normalize_api_base(raw_api_base)
        if raw_api_base and not self.netease_api_base:
            logger.warning(f"netease_api_base={raw_api_base} 不像是可用地址，已忽略")
        self.netease_fetcher = netease_fetch.NeteaseFetcher(
            timeout=self.music_api_timeout,
            cookie=self.netease_cookie,
            meting_base=self.netease_meting_base,
            api_base=self.netease_api_base,
            api_token=str(config.get("netease_api_token", "") or "").strip(),
        )
        # === 多音源路由（0.7.0）：按 source_order 依次尝试，前一个没成自动换下一个 ===
        # 网易云用上面这个取歌器（原单源逻辑），酷狗/酷我/QQ 音乐动态 import
        # 点歌插件 astrbot_plugin_music 的现成平台类，配置也从它那里读。
        plugin_dir = Path(__file__).resolve().parent
        self.source_order = source_router.normalize_source_order(
            config.get("source_order", list(source_router.DEFAULT_SOURCE_ORDER))
        )
        self.song_router = source_router.SourceRouter(
            source_order=self.source_order,
            netease_fetcher=self.netease_fetcher,
            dest_dir=self._netease_temp_dir(),
            music_plugin_dir=plugin_dir.parent / "astrbot_plugin_music",
            music_config_path=(
                plugin_dir.parent.parent
                / "config"
                / "astrbot_plugin_music_config.json"
            ),
            timeout=self.music_api_timeout,
            download_timeout=max(60, self.timeout),
            limit=10,
            quality_br=self._netease_quality_br(),
        )
        logger.info(
            f"[音源路由] 顺序：{source_router.describe_order(self.source_order)}"
            "（改插件配置 source_order 可调整顺序/停用某个源）"
        )
        self.alert_admin_on_cookie_expired = bool(
            config.get("alert_admin_on_cookie_expired", True)
        )
        try:
            self.cookie_alert_interval_hours = max(
                1, int(config.get("cookie_alert_interval_hours", 6))
            )
        except (TypeError, ValueError):
            logger.warning("cookie_alert_interval_hours 配置格式错误，使用默认值 6")
            self.cookie_alert_interval_hours = 6
        # 连续 cookie 相关失败计数 + 上次提醒时间（同一失效状态按间隔去重）
        self._netease_cookie_fail_streak = 0
        self._cookie_alert_sent_at = 0.0
        
        self.llm_force_mode = config.get("llm_force_mode", False)
        self.enable_smart_cover = config.get("enable_smart_cover", True)
        self.enable_enhanced_context = config.get("enable_enhanced_context", True)
        # 0.16.0：按人格自动选歌（用户没给歌名时要求模型按人设挑，避开烂大街热歌）
        self.persona_song_pick_enable = bool(config.get("persona_song_pick_enable", False))
        self.persona_song_pick_hint = str(config.get("persona_song_pick_hint", "") or "").strip()
        try:
            self.persona_song_pick_avoid_count = max(
                0, min(50, int(config.get("persona_song_pick_avoid_count", 12)))
            )
        except (TypeError, ValueError):
            self.persona_song_pick_avoid_count = 12
        self.enable_confirm_mechanism = config.get("enable_confirm_mechanism", True)
        self.enable_batch_cover = config.get("enable_batch_cover", True)
        self.enable_preference_learning = config.get("enable_preference_learning", True)
        
        self.enable_llm_success_notify = config.get("enable_llm_success_notify", True)
        self.enable_send_file = config.get("enable_send_file", False)
        self.enable_config_report = config.get("enable_config_report", True)
        
        # === 方案A+B：上下文注入 + 任务追踪 ===
        self.enable_inject_context = config.get("enable_inject_context", True)
        self.enable_task_tracking = config.get("enable_task_tracking", True)
        self.task_timeout_seconds = config.get("task_timeout_seconds", 9000)
        
        # 0.10.1：默认模型序号设置项已删除（与 rvc_default_model 下拉重复），固定第 1 个兜底
        self.fallback_model_index = FALLBACK_MODEL_INDEX
        # 插件设置里下拉选的默认 RVC 音色（空串表示不指定，回落到第 1 个模型）
        self.rvc_default_model = str(config.get("rvc_default_model", "") or "").strip()
        # === 0.13.0 按歌曲语言挑音色（只认中文/日语/英文，空串表示这一档没配）===
        self.language_model_enable = bool(config.get("language_model_enable", True))
        self.language_models = {
            "zh": str(config.get("language_model_zh", "") or "").strip(),
            "ja": str(config.get("language_model_ja", "") or "").strip(),
            "en": str(config.get("language_model_en", "") or "").strip(),
        }
        self.default_key_shift = config.get("default_key_shift", 0)
        self.f0_method = config.get("f0_method", "rmvpe")
        self.index_rate = _bounded_float(config, "index_rate", 0.8, 0.0, 1.0)
        # === 0.8.0 中间层真正会用的采样/混音参数 ===
        # 中间层 0.8.0 起把这些值按名字传进 RVC（index_rate/rms_mix_rate/protect/f0）
        # 和混音（inst_volume_db/vocal_volume_db）；老后端会在提交前按参数表探测、缺了就不传。
        self.rms_mix_rate = _bounded_float(config, "rms_mix_rate", 0.35, 0.0, 1.0)
        self.protect = _bounded_float(config, "protect", 0.33, 0.0, 0.5)
        self.inst_volume_db = _bounded_float(config, "inst_volume_db", -6.0, -20.0, 0.0)
        self.vocal_volume_db = _bounded_float(config, "vocal_volume_db", 0.0, -12.0, 12.0)
        self.shift_accompaniment = config.get("shift_accompaniment", True)

        # === 0.6.0 翻唱时长（只对 chorus-mw 这类支持 render_mode 的后端生效）===
        raw_render_mode = str(config.get("render_mode", "chorus") or "chorus").strip().lower()
        if raw_render_mode not in ("chorus", "full", "custom"):
            logger.warning(f"render_mode={raw_render_mode} 不认识，按 chorus（副歌片段）处理")
            raw_render_mode = "chorus"
        self.render_mode = raw_render_mode
        try:
            seconds = int(round(float(config.get("chorus_seconds", 45))))
        except (TypeError, ValueError):
            logger.warning("chorus_seconds 配置格式错误，使用默认值 45")
            seconds = 45
        self.chorus_seconds = max(20, min(180, seconds))
        # 0.16.2：指定副歌起点（秒）。0=自动；>0 时要求中间层 ≥0.9.4 从这一秒开始截。
        try:
            self.chorus_start_seconds = max(0, min(600, int(config.get("chorus_start_seconds", 0))))
        except (TypeError, ValueError):
            self.chorus_start_seconds = 0

        
        # 0.10.1：MSST 批大小/重叠/归一化/TTA 与整组 UVR5 参数已从设置页删除。
        # 它们只是 /convert 的形参占位：chorus-mw 收到后不传给 pipeline，
        # 分离模型固定用后端 config.SEP_MODEL，改了也不影响成品。
        # 这里只留 /切换msst模型 需要的运行时状态（不再从设置页读）。
        self.msst_default_model = MSST_DEFAULT_MODEL
        
        self.max_batch_size = config.get("max_batch_size", 5)
        # 0.12.1：偏好文件默认放 data/plugin_data/<插件名>/，不再写 data/ 根目录。
        # 老配置里若还是旧默认值 "data/user_preferences.json"，自动迁到新位置。
        default_pref = os.path.join(_plugin_data_dir(), "user_preferences.json")
        configured_pref = str(config.get("preference_storage_path", "") or "").strip()
        legacy_pref = os.path.join("data", "user_preferences.json")
        if not configured_pref or configured_pref.replace("\\", "/") == legacy_pref.replace("\\", "/"):
            self.preference_storage_path = default_pref
            legacy_abs = os.path.join(os.getcwd(), "data", "user_preferences.json")
            if not os.path.exists(default_pref) and os.path.exists(legacy_abs):
                try:
                    shutil.copy2(legacy_abs, default_pref)
                    logger.info(f"用户偏好已从旧位置迁移到 {default_pref}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"迁移旧偏好文件失败（继续用新路径）: {exc}")
        else:
            self.preference_storage_path = configured_pref

        self.enable_auto_key_shift = config.get("enable_auto_key_shift", False)
        self.enable_auto_key_shift_debug = config.get("enable_auto_key_shift_debug", False)
        self.gender_detection_timeout = max(
            3, int(config.get("gender_detection_timeout", 15))
        )
        self.male_to_female_shift = config.get("male_to_female_shift", 5)
        self.female_to_male_shift = config.get("female_to_male_shift", -5)
        self.artist_gender_map = self._parse_gender_map(config.get("artist_gender_map", []))
        self.model_gender_map = self._parse_gender_map(config.get("model_gender_map", []))

        # === 方案E：偏好学习系统 ===
        self.user_preferences: Dict[str, Dict] = {}
        self._pref_lock = asyncio.Lock()  # 偏好数据并发锁
        self._load_preferences()
        
        # === 异步任务追踪 ===
        self._pending_tasks: Set[asyncio.Task] = set()
        
        # === 方案B：翻唱任务状态追踪 ===
        self._active_cover_tasks: Dict[str, Dict] = {}
        
        # === 本地音频缓存 ===
        self._recent_audio_cache: Dict[str, tuple[str, float]] = {}
        self._recent_audio_cache_ttl = config.get("local_audio_cache_ttl", 300)
        self.max_local_audio_size_mb = config.get("max_local_audio_size_mb", 20)
        self.enable_local_audio_cover = config.get("enable_local_audio_cover", True)
        self.local_audio_auto_trigger = config.get("local_audio_auto_trigger", False)
        # 只留「翻唱」「变声」这类明确想唱的词；「转换/处理/修音」会把只想聊天
        # 或点歌的消息也吞进来，0.4.3 起从默认词表里去掉。词表见 intent_gate.py。
        self.local_audio_trigger_keywords = config.get(
            "local_audio_trigger_keywords", ["翻唱", "变声"]
        )
        self.local_audio_default_model = config.get("local_audio_default_model", "")
        self.local_audio_default_shift = config.get("local_audio_default_shift", 0)
        self._audio_cache_lock = asyncio.Lock()
        
        # === 清理旧临时文件 ===
        self._cleanup_old_temp_files()
        
        # === 性别识别缓存 ===
        self._gender_cache: Dict[str, str] = {}
        self._gender_cache_max_size = 100
        

    @staticmethod
    def _onebot_response_data(response: Any) -> Dict[str, Any]:
        """Extract the data object returned by aiocqhttp/OneBot actions."""
        if not isinstance(response, dict):
            return {}
        data = response.get("data")
        return data if isinstance(data, dict) else response

    async def _find_recent_group_file(
        self,
        call_action,
        group_id: int,
        file_name: str,
        file_size: int,
    ) -> tuple[Optional[Dict[str, Any]], str]:
        """Recover an upload that succeeded before LLBot's file-card feed failed.

        LLBot uploads the binary first and then publishes a group-file feed.  QQ
        may return -1002 for the second step even though the file is already in
        the group file system.  Verify the root file list before retrying the
        whole upload so we do not create duplicates.
        """
        expected_name = str(file_name or "").strip()
        expected_size = max(0, int(file_size or 0))
        now = int(time.time())

        for check in range(3):
            if check:
                await asyncio.sleep(check)
            try:
                response = await call_action(
                    "get_group_root_files",
                    group_id=int(group_id),
                )
                data = self._onebot_response_data(response)
                files = data.get("files", [])
                if not isinstance(files, list):
                    files = []

                candidates = []
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("file_name", "")).strip() != expected_name:
                        continue
                    try:
                        listed_size = int(item.get("file_size", 0) or 0)
                        upload_time = int(item.get("upload_time", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if expected_size and listed_size != expected_size:
                        continue
                    # Only accept a fresh item from this upload attempt.  This
                    # avoids mistaking an old cached song with the same name.
                    if upload_time and abs(now - upload_time) > 15 * 60:
                        continue
                    candidates.append(item)

                if not candidates:
                    continue
                uploaded = max(
                    candidates,
                    key=lambda item: int(item.get("upload_time", 0) or 0),
                )
                download_url = ""
                file_id = uploaded.get("file_id")
                if file_id:
                    try:
                        url_response = await call_action(
                            "get_group_file_url",
                            group_id=int(group_id),
                            file_id=file_id,
                            busid=int(uploaded.get("busid", 0) or 0),
                        )
                        url_data = self._onebot_response_data(url_response)
                        download_url = str(url_data.get("url", "") or "").strip()
                    except Exception as url_error:
                        logger.debug(f"读取已上传群文件下载链接失败: {url_error}")
                return uploaded, download_url
            except Exception as verify_error:
                logger.debug(
                    f"核验QQ群文件上传结果失败: check={check + 1}/3 "
                    f"error={verify_error}"
                )
        return None, ""

    def _cover_archive_dir(self) -> str:
        path = os.path.join(_plugin_data_dir(), "archive")
        os.makedirs(path, exist_ok=True)
        return path

    def _prune_cover_archive(self, keep_hours: int = 48) -> None:
        """删掉超过保留期的归档音频，避免磁盘越攒越满。"""
        try:
            cutoff = time.time() - keep_hours * 3600
            for name in os.listdir(self._cover_archive_dir()):
                full = os.path.join(self._cover_archive_dir(), name)
                if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                    os.remove(full)
        except OSError as error:
            logger.debug(f"清理翻唱归档失败: {error}")

    def _archive_cover_file(self, result_path: str, song_name: str = "翻唱") -> str:
        """把没发出去的成品复制到插件数据目录，返回取回编号。"""
        try:
            safe = re.sub(r'[\\/:*?"<>|]', "", song_name).strip()[:24] or "翻唱"
            stamp = time.strftime("%m%d-%H%M%S")
            archive_id = f"{stamp}_{safe}"
            target = os.path.join(self._cover_archive_dir(), archive_id + ".wav")
            shutil.copy2(result_path, target)
            self._prune_cover_archive()
            logger.info(f"[MatsukoCover] 音频已归档：{archive_id} -> {target}")
            return archive_id
        except Exception as error:
            logger.warning(f"归档翻唱音频失败: {error}")
            return ""

    def _find_archived_cover(self, archive_id: str) -> str:
        """按编号（或编号前缀）找归档文件，找不到返回空串。"""
        wanted = str(archive_id or "").strip()
        if not wanted:
            return ""
        directory = self._cover_archive_dir()
        exact = os.path.join(directory, wanted + ".wav")
        if os.path.isfile(exact):
            return exact
        matches = sorted(
            name for name in os.listdir(directory)
            if name.endswith(".wav") and name.startswith(wanted)
        )
        if len(matches) == 1:
            return os.path.join(directory, matches[0])
        return ""

    @filter.command("取回翻唱")
    async def fetch_archived_cover(self, event: AstrMessageEvent):
        """把当时没发出去的翻唱音频重新发一次。"""
        archive_id = str(event.message_str or "").replace("取回翻唱", "").strip()
        if not archive_id:
            directory = self._cover_archive_dir()
            files = sorted(
                (name for name in os.listdir(directory) if name.endswith(".wav")),
                reverse=True,
            )[:10]
            if not files:
                yield event.plain_result("暂时没有存下来的翻唱音频。")
                return
            listing = "\n".join("· " + os.path.splitext(name)[0] for name in files)
            yield event.plain_result(f"用法：/取回翻唱 <编号>\n最近存档：\n{listing}")
            return
        path = self._find_archived_cover(archive_id)
        if not path:
            yield event.plain_result(f"没找到编号 {archive_id} 的存档，发 /取回翻唱 看最近有哪些。")
            return
        try:
            await event.send(event.chain_result([Record(file=path)]))
            yield event.plain_result("已重新发送上面那条音频。")
        except Exception as error:
            logger.error(f"重发归档音频失败: {error}")
            yield event.plain_result(f"发送失败：{error}")

    async def _send_cover_result(self, event: AstrMessageEvent, result_path: str, song_name: str = "翻唱", cache_hit: bool = False):
        if not result_path or not os.path.exists(result_path):
            await event.send(event.plain_result("生成失败，后端未返回有效文件路径。"))
            return
        if cache_hit:
            await event.send(event.plain_result("⚡ 命中缓存，这首之前唱过，直接发给你～"))
        record_sent = False
        file_sent = False
        record_error = None
        try:
            await event.send(event.chain_result([Record(file=result_path)]))
            record_sent = True
            logger.info(f"[MatsukoCover发送Debug] QQ语音发送成功: {result_path}")
        except Exception as e:
            record_error = e
            logger.error(f"QQ语音发送失败，将继续尝试发送文件: {e}")
        # 配置开启时额外发送文件；即使未开启，QQ 语音失败也必须自动兜底。
        if self.enable_send_file or not record_sent:
            file_name = os.path.basename(result_path)
            _, ext = os.path.splitext(file_name)
            if not ext:
                ext = ".mp3"
            safe_name = re.sub(r'[\\/:*?"<>|]', '', song_name).strip() or "翻唱"
            send_name = f"{safe_name}{ext}"
            file_path = os.path.abspath(result_path)

            # OneBot 群聊中的普通 File 消息可能被协议端静默忽略。优先走
            # upload_group_file，确保真正上传到群文件；其他平台/私聊再回退 File。
            group_id = event.get_group_id()
            call_action = getattr(getattr(event, "bot", None), "call_action", None)
            if group_id and callable(call_action):
                # LLBot may still be finalizing the preceding voice message. An
                # immediate file upload commonly returns code=-1002 / 请重试.
                if record_sent:
                    await asyncio.sleep(2)
                ascii_name = f"matsuko_cover_{int(time.time())}{ext.lower()}"
                upload_names = (send_name, send_name, ascii_name)
                upload_error = None
                for attempt, upload_name in enumerate(upload_names, start=1):
                    if attempt > 1:
                        await asyncio.sleep(2 * (attempt - 1))
                    try:
                        response = await call_action(
                            "upload_group_file",
                            group_id=int(group_id),
                            file=file_path,
                            name=upload_name,
                        )
                        file_sent = True
                        logger.info(
                            "[MatsukoCover发送Debug] "
                            f"QQ群文件上传成功: name={upload_name} attempt={attempt} "
                            f"response={response!r}"
                        )
                        break
                    except Exception as e:
                        upload_error = e
                        logger.warning(
                            "[MatsukoCover发送Debug] "
                            f"QQ群文件上传失败: name={upload_name} "
                            f"attempt={attempt}/{len(upload_names)} error={e}"
                        )
                        uploaded, download_url = await self._find_recent_group_file(
                            call_action,
                            int(group_id),
                            upload_name,
                            os.path.getsize(file_path),
                        )
                        if uploaded:
                            file_sent = True
                            logger.info(
                                "[MatsukoCover发送Debug] "
                                "QQ群文件主体已上传，只有聊天文件卡片发布失败: "
                                f"name={upload_name} file_id={uploaded.get('file_id')}"
                            )
                            notice = (
                                f"✅ 音频已保存到群文件：{upload_name}\n"
                                "⚠️ QQ 拒绝了群聊文件卡片，已自动核验实际上传结果。"
                            )
                            if download_url:
                                notice += f"\n🔗 下载链接：{download_url}"
                            await event.send(event.plain_result(notice))
                            break
                if not file_sent:
                    logger.warning(f"QQ群文件重试全部失败，将回退普通文件消息: {upload_error}")

            if not file_sent:
                try:
                    await event.send(event.chain_result([File(name=send_name, file=file_path)]))
                    file_sent = True
                    logger.info(f"[MatsukoCover发送Debug] QQ文件消息发送成功: {send_name}")
                except Exception as e:
                    logger.error(f"以文件形式发送翻唱结果失败: {e}")
        if record_sent and self.enable_send_file and not file_sent:
            await event.send(event.plain_result(
                "⚠️ 语音已发送，但 LLBot 群文件上传失败；"
                "已完成多次重试，请查看 LLBot 日志中的 upload_group_file 错误。"
            ))
        if not record_sent and not file_sent:
            # 0.17.0：发不出去时把成品存下来，给个编号让用户事后取回，别直接丢
            archive_id = self._archive_cover_file(result_path, song_name)
            if archive_id:
                raise RuntimeError(
                    f"QQ语音和文件均发送失败（{record_error or '未知错误'}）；"
                    f"成品已存为 {archive_id}，发 /取回翻唱 {archive_id} 可以重新取"
                )
            raise RuntimeError(f"QQ语音和文件均发送失败: {record_error or '未知错误'}")
        if not record_sent and file_sent:
            await event.send(event.plain_result("⚠️ QQ语音上传失败，已自动改为发送音频文件。"))

    async def _send_progress_notice(
        self, event: AstrMessageEvent, step: str = "", song: str = "", **facts: Any
    ) -> None:
        """进度提示统一走 outbound 的 progress 出口。

        默认静音（缺省 notify_progress=False），要发也只发一句人话；
        出口层自己吞异常，这里不再包 try/except，也不会打断推理流程。
        """
        await self.outbound.say(event, "progress", step=step, song=song, **facts)

    def _load_preferences(self):
        """加载用户偏好数据"""
        if not self.enable_preference_learning:
            return
            
        try:
            pref_path = Path(self.preference_storage_path)
            if pref_path.exists():
                with open(pref_path, 'r', encoding='utf-8') as f:
                    self.user_preferences = json.load(f)
                logger.info(f"已加载用户偏好数据: {len(self.user_preferences)} 个用户")
        except Exception as e:
            logger.error(f"加载偏好数据失败: {e}")
            self.user_preferences = {}

    def _recent_covered_songs(self, limit: int = 12) -> List[str]:
        """从使用记录里取最近唱过的歌（所有人合并，去重，保留最近的）。"""
        if limit <= 0:
            return []
        seen: List[str] = []
        for pref in (self.user_preferences or {}).values():
            songs = pref.get("favorite_songs") or []
            for entry in reversed(songs):  # 列表是追加顺序，倒着取才是最近
                text = str(entry or "").strip()
                if text and text not in seen:
                    seen.append(text)
                if len(seen) >= limit:
                    return seen
        return seen

    def _persona_song_rule(self) -> str:
        """拼出「按人格选歌」的规则文本；没开启时返回空串。"""
        if not getattr(self, "persona_song_pick_enable", False):
            return ""
        recent = self._recent_covered_songs(getattr(self, "persona_song_pick_avoid_count", 12))
        parts = [
            "【自动选歌规则】",
            "当用户没有明确说出歌名（例如只说「唱首歌」「来一首」「随便唱」），不要反问、也不要默认挑最热门的歌；",
            "请结合你当前的人格设定（年龄、性格、喜好、时代感、口头习惯）自己挑一首，",
            "避开近两年短视频和榜单上最烂大街的那些，优先与人设气质相符、可以偏冷门的作品。",
            "挑定后直接调用 smart_cover 翻唱，并顺口说一句为什么这首歌符合你。",
        ]
        if recent:
            parts.append("最近已经唱过下面这些，别再重复：" + "、".join(recent) + "。")
        hint = str(getattr(self, "persona_song_pick_hint", "") or "").strip()
        if hint:
            parts.append("额外的选歌偏好：" + hint)
        return "".join(parts)

    @filter.on_llm_request()
    async def inject_persona_song_rule(self, event: AstrMessageEvent, req):
        """把选歌规则拼进系统提示词；只影响这一次请求，不改全局人格。"""
        try:
            rule = self._persona_song_rule()
            if not rule:
                return
            # 只在「像是在点唱」的消息上加，普通聊天不背这段提示词
            text = str(getattr(event, "message_str", "") or "")
            if not re.search(r"唱|翻唱|来一首|点歌|来首", text):
                return
            current = getattr(req, "system_prompt", "") or ""
            if "【自动选歌规则】" in current:
                return
            req.system_prompt = (current + "\n\n" + rule).strip()
            logger.debug("已注入按人格选歌规则（最近 %d 首不重复）", getattr(self, "persona_song_pick_avoid_count", 12))
        except Exception as error:
            logger.debug(f"注入选歌规则失败，忽略: {error}")

    async def _save_preferences(self):
        """保存用户偏好数据（异步安全）"""
        if not self.enable_preference_learning:
            return
            
        async with self._pref_lock:
            try:
                pref_path = Path(self.preference_storage_path)
                pref_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(pref_path, 'w', encoding='utf-8') as f:
                    json.dump(self.user_preferences, f, ensure_ascii=False, indent=2)
                logger.info(f"已保存用户偏好数据: {len(self.user_preferences)} 个用户")
            except Exception as e:
                logger.error(f"保存偏好数据失败: {e}")
    
    def _cleanup_old_temp_files(self):
        """清理超过24小时的旧临时文件"""
        try:
            temp_dir = _temp_audio_dir()
            if not os.path.isdir(temp_dir):
                return
            now = time.time()
            for fname in os.listdir(temp_dir):
                fpath = os.path.join(temp_dir, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 86400:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"清理旧临时文件失败: {e}")

    def _is_audio_file(self, filename: Optional[str]) -> bool:
        """判断文件名是否为支持的音频格式"""
        if not filename:
            return False
        ext = os.path.splitext(filename.lower())[1]
        return ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".wma", ".aac", ".mp4"}

    def _reject_music_request(self, event: AstrMessageEvent, tool_name: str) -> Optional[str]:
        """翻唱入口的统一闸门：这条消息是点歌需求时不执行翻唱。

        判定逻辑和词表都在 ``intent_gate.py``，这里只负责记日志、把提示句交回模型。
        返回 ``None`` 表示放行；返回字符串表示拒绝（字符串会作为工具结果给模型）。
        """
        hint = cover_refusal(event)
        if hint:
            logger.info(
                f"[IntentGate] {tool_name} 未执行：这条消息是点歌需求 | "
                f"text={event_message_text(event)!r}"
            )
        return hint

    def _infer_key_shift_from_filename(self, filename: str, model_display: str = "") -> Optional[int]:
        """根据音频文件名自动推断合适的 key_shift（本地兜底逻辑，不依赖LLM）
        当 LLM 未传入 key_shift 时，优先使用用户配置的映射表和数值进行推断。
        返回 None 表示无法推断（使用默认配置）。
        """
        if not filename:
            return None
        
        basename = os.path.splitext(os.path.basename(filename))[0].lower()
        model_lower = model_display.lower() if model_display else ""
        
        # ===== Step 1: 使用用户配置的歌手性别映射表 =====
        song_gender = None
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in basename:
                song_gender = gender
                logger.info(f"[AutoKeyShift] 文件名匹配歌手映射表: '{artist}' → {gender}")
                break
        
        # ===== Step 2: 如果映射表未命中，用关键词推断 =====
        if not song_gender:
            male_indicators = [
                "周杰伦", "林俊杰", "陈奕迅", "李荣浩", "薛之谦", "毛不易", "张学友",
                "刘德华", "周深", "五月天", "beyond", "陶喆", "方大同", "王力宏",
                "萧敬腾", "杨宗纬", "张敬轩", "胡彦斌", "许嵩", "汪苏泷",
                "听妈妈的话", "晴天", "稻香", "七里香", "青花瓷", "告白气球",
                "夜曲", "简单爱", "不能说的秘密", "蒲公英的约定", "一路向北",
                "江南", "曹操", "背对背拥抱", "可惜没如果", "修炼爱情",
                "十年", "浮夸", "k歌之王", "富士山下",
                "模特", "年少有为", "不将就", "戒烟",
                "演员", "绅士", "认真的雪", "刚刚好",
                "消愁", "像我这样的人", "借", "无问",
                "海阔天空", "光辉岁月", "真的爱你", "喜欢你",
                "普通朋友", "小镇姑娘", "流沙",
                "爱爱爱", "三人游", "特别的人",
                "唯一", "爱你等于爱自己", "龙的传人",
            ]
            female_indicators = [
                "邓紫棋", "王菲", "蔡依林", "张惠妹", "孙燕姿", "田馥甄",
                "梁静茹", "刘若英", "林忆莲", "蔡健雅", "徐佳莹", "周笔畅",
                "张靓颖", "李宇春", "谭维维", "萨顶顶", "阿兰",
                "霉霉", "taylor", "adele", "beyonce", "rihanna",
                "光年之外", "泡沫", "喜欢你", "句号", "倒数",
                "红豆", "传奇", "如愿", "匆匆那年",
                "日不落", "舞娘", "说爱你", "看我72变",
                "听海", "我可以抱你吗", "记得",
                "遇见", "天黑黑", "开始懂了", "我怀念的",
                "小幸运", "你就不要想起我", "寂寞寂寞就好",
                "勇气", "暖暖", "分手快乐", "宁夏",
                "后来", "很爱很爱你", "成全",
            ]
            is_male_song = any(ind.lower() in basename for ind in male_indicators)
            is_female_song = any(ind.lower() in basename for ind in female_indicators)
            if is_male_song:
                song_gender = "male"
            elif is_female_song:
                song_gender = "female"
        
        # 如果仍无法推断歌曲性别，返回 None
        if not song_gender:
            return None
        
        # ===== Step 3: 推断模型性别（优先用户配置的模型性别映射表）=====
        model_gender = None
        for alias, gender in self.model_gender_map.items():
            if alias.lower() in model_lower:
                model_gender = gender
                logger.info(f"[AutoKeyShift] 模型匹配映射表: '{alias}' → {gender}")
                break
        
        # 映射表未命中，用关键词推断
        if not model_gender:
            female_model_keywords = ["甘城", "猫猫", "塔菲", "tafei", "nyaa", "miku", "初音", "镜音", "洛天依", "reol", "ado", "yoasobi", "yorushika", "ayase", "ikura", " LiSA", "美波", "aimer", "花谱", "可不", "星界", "星尘", "心华", "乐正", "墨清弦", "言和", "战音", "绯村", "鹿乃", "hanser", "泠鸢", "冰糖", "七海", "阿梓", "小可", "永雏", "猫雷", "東雪蓮", "明前", "兰音", "扇宝", "虚研", "四禧", "丸子", "EOE", "ASOUL", "乃琳", "贝拉", "珈乐", "嘉然", "向晚", "星瞳", "雫lulu", "真白花音", "早稻叽", "白神遥", "花园", "hiiro", "希月", "奈奈", "米诺", "虞莫", "莞儿", "露早", "柚恩"]
            male_model_keywords = ["kano", "shinichi", "男声", "少年", "大叔", "青年", "正太", "低音", "王爷", "葛平", "波澜", "面筋", "元首", "哲♂学", "比利", "van样", "香蕉君", "刘醒", "梁非凡", "金坷垃", "庞麦郎", "波澜哥", "李云龙", "张涵予", "姜文", "于谦", "郭德纲", "赵本山", "范伟", "沈腾", "黄渤", "徐峥", "王宝强", "刘烨", "张译", "朱亚文", "靳东", "胡歌", "王凯", "肖战", "王一博", "易烊千玺", "王俊凯", "王源", "华晨宇", "毛不易", "周深", "张杰", "薛之谦", "李荣浩", "许嵩", "汪苏泷", "陈奕迅", "林俊杰", "周杰伦"]
            if any(kw in model_lower for kw in female_model_keywords):
                model_gender = "female"
            elif any(kw in model_lower for kw in male_model_keywords):
                model_gender = "male"
        
        # 如果无法推断模型性别，假设为女声模型（大多数AI翻唱模型偏女声）
        if not model_gender:
            model_gender = "female"
        
        # ===== Step 4: 根据性别匹配关系返回配置中的数值 =====
        if song_gender == model_gender:
            return 0  # 同性匹配，保持原调
        elif model_gender == "female" and song_gender == "male":
            return self.male_to_female_shift  # 男→女，使用配置的升调值
        elif model_gender == "male" and song_gender == "female":
            return self.female_to_male_shift  # 女→男，使用配置的降调值
        
        return None

    async def _cleanup_expired_audio_cache(self):
        """清理过期的近期音频缓存"""
        now = time.time()
        expired = []
        async with self._audio_cache_lock:
            for key, (path, ts) in list(self._recent_audio_cache.items()):
                if now - ts > self._recent_audio_cache_ttl:
                    expired.append(key)
                    temp_dir = _temp_audio_dir()
                    if path.startswith(temp_dir) and os.path.isfile(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            for key in expired:
                self._recent_audio_cache.pop(key, None)

    async def _extract_audio_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息事件中提取音频文件的本地路径"""
        msg_chain = getattr(event.message_obj, "message", [])
        if not msg_chain:
            logger.debug("[LocalAudio] 消息链为空")
            return None
        
        for idx, comp in enumerate(msg_chain):
            comp_type = type(comp).__name__
            logger.debug(f"[LocalAudio] 检查消息组件 {idx}: {comp_type}")
            
            if isinstance(comp, Record):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_url = getattr(comp, "url", None)
                file_name = getattr(comp, "name", "")
                
                logger.info(f"[LocalAudio] 发现语音/音频消息: file={file_path}, url={_mask_url(file_url)}")
                
                # 优先使用本地路径
                if file_path and os.path.isfile(file_path):
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    # 尝试从 url 或 filename 提取后缀
                    suffix = ".mp3"
                    if file_name and "." in file_name:
                        suffix = os.path.splitext(file_name)[1]
                    elif file_url and "." in file_url.split("?")[0]:
                        url_ext = os.path.splitext(file_url.split("?")[0])[1]
                        if len(url_ext) <= 5: # 确保是正常的后缀
                            suffix = url_ext
                            
                    downloaded = await self._download_audio(file_url, suffix)
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取
                if file_path:
                    try:
                        record_info = await event.bot.call_action("get_record", file=file_path, out_format="mp3")
                        logger.info(f"[LocalAudio] get_record 返回: {record_info}")
                        if isinstance(record_info, dict):
                            actual_path = record_info.get("file") or record_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                return actual_path
                            actual_url = record_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 获取语音文件失败: {e}")
            
            elif isinstance(comp, File):
                file_path = getattr(comp, "file_", None) or getattr(comp, "file", None)
                file_name = getattr(comp, "name", "") or ""
                file_url = getattr(comp, "url", None)
                logger.info(f"[LocalAudio] 发现文件消息: name={file_name}, file={file_path}, url={_mask_url(file_url)}")
                
                if not self._is_audio_file(file_name):
                    logger.debug(f"[LocalAudio] 跳过非音频文件: {file_name}")
                    continue
                
                # 优先本地路径
                if file_path and os.path.isfile(file_path):
                    size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    if size_mb > self.max_local_audio_size_mb:
                        logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                        return None
                    return file_path
                
                # 尝试 URL 直接下载
                if file_url:
                    downloaded = await self._download_audio(file_url, os.path.splitext(file_name)[1] or ".mp3")
                    if downloaded:
                        return downloaded
                
                # 尝试通过 OneBot API 获取文件
                if file_path:
                    try:
                        file_info = await event.bot.call_action("get_file", file_id=file_path)
                        logger.info(f"[LocalAudio] get_file 返回: {file_info}")
                        if isinstance(file_info, dict):
                            actual_path = file_info.get("file") or file_info.get("path")
                            if actual_path and os.path.isfile(actual_path):
                                size_mb = os.path.getsize(actual_path) / (1024 * 1024)
                                if size_mb > self.max_local_audio_size_mb:
                                    logger.warning(f"[LocalAudio] 音频文件过大: {size_mb:.1f}MB")
                                    return None
                                return actual_path
                            actual_url = file_info.get("url")
                            if actual_url:
                                downloaded = await self._download_audio(actual_url, os.path.splitext(file_name)[1] or ".mp3")
                                if downloaded:
                                    return downloaded
                    except Exception as e:
                        logger.warning(f"[LocalAudio] 通过API获取文件失败: {e}")
        
        logger.debug(f"[LocalAudio] 未从消息中提取到音频。消息链组件数: {len(msg_chain)}")
        return None

    async def _download_audio(self, url: str, suffix: str = ".mp3") -> Optional[str]:
        """下载音频文件到临时目录"""
        try:
            import aiohttp
            temp_dir = _temp_audio_dir()
            os.makedirs(temp_dir, exist_ok=True)
            dest = os.path.join(temp_dir, f"dl_{uuid.uuid4().hex[:8]}{suffix}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        max_bytes = int(float(self.max_local_audio_size_mb) * 1024 * 1024)
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if content_length > max_bytes:
                            logger.warning(
                                f"下载的音频过大: {content_length / 1024 / 1024:.1f}MB，已拒绝"
                            )
                            return None
                        written = 0
                        with open(dest, "wb") as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                written += len(chunk)
                                if written > max_bytes:
                                    raise ValueError(
                                        f"下载的音频超过 {self.max_local_audio_size_mb}MB 限制"
                                    )
                                f.write(chunk)
                        size_mb = os.path.getsize(dest) / (1024 * 1024)
                        if size_mb > self.max_local_audio_size_mb:
                            logger.warning(f"下载的音频过大: {size_mb:.1f}MB，已丢弃")
                            os.remove(dest)
                            return None
                        return dest
                    else:
                        logger.warning(f"下载音频失败: HTTP {resp.status}")
        except Exception as e:
            if 'dest' in locals() and os.path.isfile(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
            logger.error(f"下载音频异常: {e}")
        return None

    # ==================== 网易云取歌（0.5.0 起照搬点歌插件 astrbot_plugin_music） ====================

    def _netease_temp_dir(self) -> str:
        """插件自己的临时音频目录（与本地音频下载共用，退出时会清理）。"""
        temp_dir = _temp_audio_dir()
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir

    def _netease_quality_br(self) -> int:
        """配置里的音质（128/192/320）换算成接口要的码率。"""
        return int(self.netease_quality) * 1000


    def _short_failure_reason(self, detail: str) -> str:
        """把技术性失败详情压成一句人话（批量报告用，细节只进日志）。"""
        text = str(detail or "")
        if any(word in text for word in ("音源", "取歌", "搜不到", "未找到", "拿不到")):
            return "这首拿不到音源"
        if "超时" in text:
            return "生成超时了"
        if "禁用" in text:
            return "RVC 没开"
        if "模型" in text and ("没有可用" in text or "序号" in text):
            return "没有可用的音色"
        if any(word in text for word in ("生成失败", "未返回有效", "没出成品")):
            return "后端没出成品"
        if "取消" in text:
            return "被取消了"
        return "这首没做成"

    def _get_admin_ids(self) -> List[str]:
        """从 AstrBot 全局配置读管理员 QQ 号（插件自己的 config 里没有）。"""
        try:
            global_config = self.context.get_config()
        except Exception as exc:
            logger.warning(f"读取 AstrBot 全局配置失败，无法拿到管理员: {exc}")
            return []
        admin_ids = None
        try:
            admin_ids = global_config.get("admins_id", [])
        except Exception as exc:
            logger.warning(f"全局配置里读 admins_id 失败: {exc}")
            return []
        if isinstance(admin_ids, str):
            admin_ids = [admin_ids]
        result: List[str] = []
        for item in admin_ids or []:
            text = str(item or "").strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _friend_session(umo: str, user_id: str) -> str:
        """由当前事件的 umo 推出"同一平台、私聊某个 QQ"的 session。"""
        platform = str(umo or "").split(":", 1)[0].strip()
        if not platform or not str(user_id or "").strip():
            return ""
        return f"{platform}:FriendMessage:{str(user_id).strip()}"

    def _cookie_alert_target(self, event: AstrMessageEvent):
        """提醒管理员时发到哪：优先管理员私聊，找不到管理员就发回触发点歌的会话。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        for admin_id in self._get_admin_ids():
            session = self._friend_session(umo, admin_id)
            if session:
                logger.info(f"网易云 cookie 失效提醒将私聊管理员: {session}")
                return _OutboundTarget(self.context, session, umo)
        logger.warning("找不到管理员 admins_id，cookie 失效提醒改发到触发点歌的会话")
        return event

    def _reset_netease_cookie_state(self) -> None:
        """取歌成功后清空"连续失败"和提醒去重状态。"""
        if self._netease_cookie_fail_streak or self._cookie_alert_sent_at:
            logger.info("网易云取歌已恢复，清空 cookie 失效状态")
        self._netease_cookie_fail_streak = 0
        self._cookie_alert_sent_at = 0.0

    async def _notify_cookie_expired(self, event: AstrMessageEvent, reason: str) -> None:
        """连续 3 次 cookie 相关失败后提醒管理员；同一失效状态按间隔去重。"""
        if not self.alert_admin_on_cookie_expired:
            return
        now = time.time()
        interval_seconds = max(1, int(self.cookie_alert_interval_hours)) * 3600
        if self._cookie_alert_sent_at and now - self._cookie_alert_sent_at < interval_seconds:
            logger.info(
                "网易云 cookie 失效提醒在去重窗口内，跳过 "
                f"（{int((now - self._cookie_alert_sent_at) / 60)} 分钟前已提醒过）"
            )
            return
        facts = {
            "reason": "网易云的 cookie 好像失效了，取不到整首歌",
            "detail": (
                f"连续 {self._netease_cookie_fail_streak} 次取歌都因为这个失败，"
                f"最近一次：{reason}"
            ),
        }
        target = self._cookie_alert_target(event)
        sent = await self.outbound.say(target, "alert", **facts)
        if not sent and target is not event:
            # 管理员会话发不出去（平台/session 对不上）时，退回触发点歌的会话
            logger.warning("管理员会话发送失败，cookie 失效提醒改发到触发点歌的会话")
            sent = await self.outbound.say(event, "alert", **facts)
        if sent:
            # 只有真发出去了才占用去重窗口
            self._cookie_alert_sent_at = now
        else:
            logger.warning("cookie 失效提醒一条都没发出去，下次失败会再试")

    async def _record_netease_cookie_failure(
        self, event: AstrMessageEvent, reason: str, is_trial: bool
    ) -> None:
        """记一次 cookie 相关失败；连续 3 次才提醒，非 cookie 失败打断连续计数。"""
        if not netease_fetch.is_cookie_failure(reason, is_trial):
            if self._netease_cookie_fail_streak:
                logger.info(
                    f"网易云取歌失败原因不是 cookie（{reason}），连续失败计数清零"
                )
            self._netease_cookie_fail_streak = 0
            return
        self._netease_cookie_fail_streak += 1
        logger.warning(
            f"网易云 cookie 相关失败第 {self._netease_cookie_fail_streak} 次: "
            f"reason={reason} trial={is_trial}"
        )
        if self._netease_cookie_fail_streak >= 3:
            await self._notify_cookie_expired(event, reason)

    # ==================== 统一取歌入口（0.7.0：多音源路由） ====================


    @staticmethod
    def _song_identity(song: Any) -> tuple[str, str, str]:
        """从歌曲对象里取出 ``(id/mid, 歌名, 歌手)``，兼容 dict 和裸字符串。

        卡片 / 命令选歌传的是 dict（有 name / artists / id / songmid），
        LLM 工具和批量翻唱传的是歌名裸字符串，两种都要能走同一个入口。
        """
        if isinstance(song, str):
            text = song.strip()
            return text, text, ""
        if not isinstance(song, dict):
            text = str(song or "").strip()
            return text, text, ""
        raw_id = str(song.get("songmid") or song.get("id") or "").strip()
        name = str(song.get("name") or song.get("title") or "").strip()
        artists = str(song.get("artists") or song.get("artist") or "").strip()
        return raw_id, name, artists


    @staticmethod
    def _pick_best_song(songs: list, query: str, artist: str = "") -> dict:
        """在搜索结果里挑**最确定**的一首；一个都不像就返回空 dict。

        0.8.0 起匹配统一走 ``source_router.pick_best_song``：歌名整串原样搜、
        别名只按「标题+别名」拼接比较、伴奏类先淘汰、歌手只在明确给出时当过滤条件；
        没有任何打分或模糊规则。这里保留这个入口，只是把 dict 列表转成候选再转回来。
        """
        return source_router.pick_best_song_dict(songs, query, artist)

    @staticmethod
    def _drop_file(path: str) -> None:
        """删掉一个临时文件；删不掉只记日志，不影响主流程。"""
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.debug(f"删除临时文件失败: {path} ({exc})")

    def _is_temp_audio(self, path: str) -> bool:
        """判断文件是不是插件自己下到 ``temp_audio/`` 的（只有这种才在 finally 删）。"""
        try:
            temp_dir = os.path.abspath(self._netease_temp_dir())
            return os.path.abspath(str(path)).startswith(temp_dir + os.sep)
        except Exception:
            return False

    async def _prepare_song_audio(
        self, event: AstrMessageEvent, song: Any
    ) -> tuple[Optional[str], str, bool]:
        """统一取歌入口：把"歌曲对象 / 歌名 / 歌曲 id"变成本地音频文件。

        0.7.0 起所有取歌路径都交给 :class:`source_router.SourceRouter`：

        1. 已经是存在的本地文件 → 直接返回，不联网；
        2. 其它情况交给路由器按 ``source_order`` 依次尝试各平台：
           搜歌（song_name 整串）→ 标题/标题+别名拼接匹配 → 排除伴奏类
           → 取播放链接 → 下载 → 80 秒闸门；
        3. 前一个平台搜不到 / 选不出 / 下不下来就自动换下一个，日志里每个平台一行；
        4. 全部失败时把每个平台的原因串起来返回；如果所有候选都只是伴奏版，
           原因就是「只有伴奏版」。

        Returns:
            ``(本地文件路径 或 None, 失败原因, 是否试听)``。失败原因是简短中文，
            堆栈和技术细节只进 logger。
        """
        raw_id, name, artists = self._song_identity(song)
        label = name or raw_id or "这首歌"
        source_hint = ""
        if isinstance(song, dict):
            source_hint = str(song.get("source") or "").strip().casefold()
        # 酷我 / QQ 的 id 也是纯数字，不能把别家来源的数字 id 当网易云 id 用
        if raw_id.isdigit() and source_hint not in ("", "netease"):
            source_hint = ""

        # 1) 已经是本地文件：直接交给中间层，别再搜一遍
        for candidate in (raw_id, name):
            if candidate and os.path.isfile(candidate):
                logger.info(f"[取歌] {label} 已经是本地文件，直接使用: {candidate}")
                return candidate, "", False

        # 2) 标题/别名带伴奏标记的，明确选了也不拿去翻唱（会出静音）
        alias_text = ""
        if isinstance(song, dict):
            alias_text = str(song.get("alias") or "").strip()
        if source_router.is_accompaniment(name, alias_text):
            logger.info(f"[取歌] {label} 标题/别名带伴奏标记，判为没有人声")
            return None, source_router.REASON_ONLY_ACCOMPANIMENT, False

        if not raw_id and not name:
            logger.info("[取歌] 既没有 id 也没有歌名，放弃")
            return None, "没有歌名也没有 id", False

        await self._send_progress_notice(
            event, step="正在按顺序找音源", song=label
        )
        result = await self.song_router.fetch_audio(
            {
                "id": raw_id,
                "name": name or raw_id,
                "artists": artists,
                "source": source_hint,
            },
            dest_dir=self._netease_temp_dir(),
        )

        # 网易云那条失败照旧计入 cookie 失效提醒（其它源失败不掺和）
        for attempt in result.attempts:
            if attempt.source == "netease" and not attempt.ok:
                await self._record_netease_cookie_failure(
                    event, attempt.reason, attempt.is_trial
                )

        if result.path:
            self._reset_netease_cookie_state()
            candidate = result.candidate
            logger.info(
                f"[取歌] 整首取到: 来源={result.source} "
                f"id={candidate.id if candidate else '?'} "
                f"歌名={candidate.name if candidate else label} "
                f"时长={(candidate.duration_ms / 1000) if candidate else 0:.1f} 秒 "
                f"file={result.path}"
            )
            return result.path, "", False

        logger.info(f"[取歌] {label} 所有音源都没拿到: {result.reason}")
        return None, result.reason, result.is_trial


    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建被追踪的异步任务，防止任务丢失和异常静默"""
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "background")
        task = asyncio.create_task(coro, name=f"matsuko_cover:{coro_name}")
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        logger.info(
            f"[MatsukoCover任务Debug] 已创建后台任务: {task.get_name()} | "
            f"pending={len(self._pending_tasks)}"
        )

        def _log_exception(t: asyncio.Task):
            if t.cancelled():
                logger.warning(f"[MatsukoCover任务Debug] 后台任务已取消: {t.get_name()}")
            elif t.exception() is not None:
                logger.error(f"[MatsukoCover] 异步任务异常: {t.exception()}", exc_info=t.exception())
            else:
                logger.info(f"[MatsukoCover任务Debug] 后台任务已完成: {t.get_name()}")

        task.add_done_callback(_log_exception)
        return task
    
    @asynccontextmanager
    async def _get_gradio_client(self, base_url: str):
        """安全获取Gradio Client，确保正确关闭"""
        client = None
        try:
            # Client 初始化会同步读取 /config；放到线程池，避免阻塞 AstrBot 事件循环。
            client = await asyncio.to_thread(
                Client, base_url, verbose=False, analytics_enabled=False
            )
            yield client
        finally:
            if client is not None:
                try:
                    await asyncio.to_thread(client.close)
                except Exception as e:
                    logger.debug(f"关闭Gradio Client时出错: {e}")

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识"""
        return str(event.get_sender_id())

    def _get_user_pref(self, user_id: str) -> Dict:
        """获取用户偏好，如果不存在则创建默认值"""
        if user_id not in self.user_preferences:
            self.user_preferences[user_id] = {
                "model_index": FALLBACK_MODEL_INDEX,
                "default_key_shift": self.default_key_shift,
                "favorite_songs": [],
                "usage_count": 0,
                "last_used_time": None,
                "preferred_artists": {},
                "artist_model_map": {}
            }
        pref = self.user_preferences[user_id]
        if isinstance(pref.get("preferred_artists"), list):
            old_list = pref["preferred_artists"]
            pref["preferred_artists"] = {a: {"count": 1, "last_time": None, "model": None} for a in old_list}
        if "artist_model_map" not in pref:
            pref["artist_model_map"] = {}
        if pref.get("model_index", 0) < 1:
            pref["model_index"] = FALLBACK_MODEL_INDEX
        return pref

    async def _async_predict(self, client, *args, timeout=300, event=None, detect_cache_hit=False, **kwargs):
        loop = asyncio.get_running_loop()
        job = await asyncio.to_thread(client.submit, *args, **kwargs)
        self._bind_active_gradio_job(event, job)
        timeout_seconds = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout_seconds

        def cancel_job_best_effort():
            """Ask Gradio to stop queued/running work without masking the timeout."""
            try:
                cancel = getattr(job, "cancel", None)
                if callable(cancel):
                    cancel()
            except Exception as cancel_error:
                logger.debug(f"推理超时后取消 Gradio 任务失败: {cancel_error}")

        def raise_inference_timeout(cause=None):
            cancel_job_best_effort()
            message = (
                f"推理任务在 {timeout_seconds:g} 秒内未完成，"
                "已尝试取消后端任务，请检查推理服务状态或调大任务超时时间。"
            )
            if cause is None:
                raise TimeoutError(message)
            raise TimeoutError(message) from cause
        
        cache_hit_detected = False

        # 0.10.1：原先这里有一套「直接发 QQ 进度条」的分支（enable_progress_bar /
        # progress_update_interval），它绕过 outbound 统一出口、发固定 emoji 文案，
        # 与 notify_progress 重复。设置页已删除这两个开关，这里也一并去掉：
        # 进度只走 outbound（默认静音），超时由下面的 deadline 逻辑兜住。

        remaining = deadline - time.monotonic()
        if remaining <= 0 and not job.done():
            raise_inference_timeout()

        # A completed Gradio job returns immediately.  Otherwise both
        # job.result() and asyncio.wait_for() share the same monotonic budget.
        result_timeout = max(0.0, remaining) if not job.done() else 0
        fn = partial(job.result, timeout=result_timeout)
        result_future = loop.run_in_executor(None, fn)
        try:
            if job.done():
                result = await result_future
            else:
                result = await asyncio.wait_for(result_future, timeout=remaining)
        except TimeoutError as exc:
            raise_inference_timeout(exc)
        
        # 解析中间层返回的缓存标记（元组格式: (file_path, "true"/"false")）
        cache_hit_from_api = False
        if detect_cache_hit and isinstance(result, (list, tuple)) and len(result) >= 2:
            cache_flag = str(result[1]).lower()
            cache_hit_from_api = cache_flag == "true"
            result = result[0]
        
        # 如果之前未检测到缓存命中，再尝试从最终状态确认（备用方案）
        if detect_cache_hit and not cache_hit_detected and not cache_hit_from_api:
            try:
                final_status = job.status()
                if hasattr(final_status, 'progress_data') and final_status.progress_data:
                    for pd in final_status.progress_data:
                        desc = getattr(pd, 'desc', None)
                        if desc is None and isinstance(pd, dict):
                            desc = pd.get('desc')
                        if desc and ('cache hit' in desc.lower() or '缓存命中' in desc):
                            cache_hit_detected = True
                            break
            except Exception as e:
                logger.debug(f"最终状态检测缓存时出错: {e}")
        
        if detect_cache_hit:
            self._clear_active_gradio_job(event, job)
            return result, cache_hit_from_api or cache_hit_detected
        self._clear_active_gradio_job(event, job)
        return result

    async def _predict_cover(
        self,
        event: AstrMessageEvent,
        song_name_src: str,
        model_name: str,
        key_shift: Optional[int],
    ) -> tuple[Any, bool]:
        """把一首（或一段）音频提交给 RVC 中间层，等它返回成品路径。"""
        base_url = self.rvc_base_url
        async with self._get_gradio_client(base_url) as client:
            msst_kwargs = await self._get_optional_msst_kwargs(client, base_url)
            render_kwargs = self._get_render_kwargs(client)
            # 0.16.2：指定起点。老中间层没有这个参数，按参数表探测后再传
            start_kwargs = {}
            if getattr(self, "chorus_start_seconds", 0) > 0:
                names = self._client_api_parameter_names(client, "/convert")
                if names and "chorus_start" in names:
                    start_kwargs["chorus_start"] = int(self.chorus_start_seconds)
                elif not getattr(self, "_chorus_start_warned", False):
                    self._chorus_start_warned = True
                    logger.warning(
                        "当前中间层不支持 chorus_start（需要 ≥0.9.4），本次仍按自动挑段处理"
                    )
            mix_kwargs = self._get_mix_kwargs(client)
            legacy_kwargs = {
                "song_name_src": song_name_src,
                "key_shift": int(key_shift or 0),
                "vocal_vol": 0,
                "inst_vol": 0,
                "model_dropdown": model_name,
                "shift_accompaniment": self.shift_accompaniment,
                "f0_method": self.f0_method,
                "index_rate": self.index_rate,
                # 0.10.1：reverb_intensity / delay_intensity / vocal_postprocess /
                # filter_radius / uvr5_* / msst_batch_size 等已从设置页删除，
                # 它们只是 /convert 的形参占位，中间层不消费，不再提交。
                **mix_kwargs,
                **msst_kwargs,
                **render_kwargs,
                **start_kwargs,
            }
            result, cache_hit = await self._async_predict(
                client,
                **legacy_kwargs,
                api_name="/convert",
                timeout=self.inference_timeout,
                event=event,
                detect_cache_hit=True,
            )
            return result, cache_hit

    # ==================== 方案A+B：上下文注入 + 任务追踪辅助方法 ====================

    def _get_task_key(self, event: AstrMessageEvent) -> str:
        """生成任务追踪的唯一键（按会话维度）"""
        return str(event.unified_msg_origin)

    def _cleanup_expired_tasks(self):
        """清理超时的翻唱任务"""
        if not self.enable_task_tracking:
            return
        now = time.time()
        expired_keys = []
        for key, task_info in self._active_cover_tasks.items():
            start_time = task_info.get("start_time", 0)
            if now - start_time > self.task_timeout_seconds:
                expired_keys.append(key)
        for key in expired_keys:
            task_info = self._active_cover_tasks.pop(key, None) or {}
            job = task_info.get("gradio_job")
            task = task_info.get("asyncio_task")
            try:
                if job is not None:
                    job.cancel()
            except Exception as exc:
                logger.debug(f"清理超时 Gradio 任务失败: {exc}")
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
            logger.warning(f"[MatsukoCover] 任务超时已清理: {key}")

    def _register_active_task(self, event: AstrMessageEvent, song_name: str, model_display: str = "") -> bool:
        """注册正在进行的翻唱任务"""
        if not self.enable_task_tracking:
            return True
        self._cleanup_expired_tasks()
        key = self._get_task_key(event)
        if key in self._active_cover_tasks:
            return False
        self._active_cover_tasks[key] = {
            "song_name": song_name,
            "model_display": model_display,
            "status": "处理中",
            "start_time": time.time(),
            "asyncio_task": None,
            "gradio_job": None,
        }
        logger.info(f"[MatsukoCover] 注册翻唱任务: {key} -> 《{song_name}》")
        return True
    def _bind_active_asyncio_task(self, event: AstrMessageEvent, task: Optional[asyncio.Task] = None) -> None:
        """把真实 asyncio 任务绑定到会话状态，供取消命令使用。"""
        if not self.enable_task_tracking:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None:
            info["asyncio_task"] = task or asyncio.current_task()

    def _bind_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None:
            info["gradio_job"] = job

    def _clear_active_gradio_job(self, event: Optional[AstrMessageEvent], job: Any) -> None:
        if not self.enable_task_tracking or event is None:
            return
        info = self._active_cover_tasks.get(self._get_task_key(event))
        if info is not None and info.get("gradio_job") is job:
            info["gradio_job"] = None

    def _finish_active_task(self, event: AstrMessageEvent, status: str = "已完成"):
        """标记翻唱任务结束"""
        if not self.enable_task_tracking:
            return
        key = self._get_task_key(event)
        if key in self._active_cover_tasks:
            self._active_cover_tasks[key]["status"] = status
            self._active_cover_tasks.pop(key, None)
            logger.info(f"[MatsukoCover] 翻唱任务结束: {key} -> {status}")
        # 0.15.0：这一首结束后，把排队里的下一首接上
        try:
            self._start_next_queued_cover(event)
        except Exception:
            logger.warning(f"启动排队翻唱失败: {traceback.format_exc()}")

    def _queue_cover_request(self, event: AstrMessageEvent, song_name: str,
                             model_index: int, key_shift: Optional[int],
                             model_display: str, artist_name: str = "",
                             model_explicit: bool = False,
                             max_waiting: int = 3) -> Optional[int]:
        """当前会话有任务在跑时，把这次请求排进队列；返回排队序号，满了返回 None。"""
        key = self._get_task_key(event)
        queues = getattr(self, "_cover_queues", None)
        if queues is None:
            queues = {}
            self._cover_queues = queues
        items = queues.setdefault(key, [])
        if len(items) >= max_waiting:
            return None
        items.append({
            "song_name": song_name,
            "model_index": model_index,
            "key_shift": key_shift,
            "model_display": model_display,
            "artist_name": artist_name,
            "model_explicit": model_explicit,
        })
        logger.info(f"[MatsukoCover] 排队翻唱: {key} | song={song_name} | 队列长度={len(items)}")
        return len(items)

    def _start_next_queued_cover(self, event: AstrMessageEvent) -> None:
        """把队列里的下一首拉起来；没有排队就什么都不做。"""
        key = self._get_task_key(event)
        queues = getattr(self, "_cover_queues", {})
        items = queues.get(key) or []
        if not items:
            return
        item = items.pop(0)
        if not self._register_active_task(event, item["song_name"], item["model_display"]):
            items.insert(0, item)
            return
        task = self._create_tracked_task(self._smart_cover_async(
            event,
            item["song_name"],
            item["model_index"],
            item["key_shift"],
            item["model_display"],
            artist_name=item["artist_name"],
            model_explicit=item["model_explicit"],
        ))
        self._bind_active_asyncio_task(event, task)
        logger.info(f"[MatsukoCover] 开始排队翻唱: {key} | song={item['song_name']} | 剩余={len(items)}")

    def _get_active_task_status(self, event: AstrMessageEvent) -> Optional[Dict]:
        """获取当前会话的活跃任务状态"""
        if not self.enable_task_tracking:
            return None
        self._cleanup_expired_tasks()
        key = self._get_task_key(event)
        return self._active_cover_tasks.get(key)

    async def _inject_message_to_context(self, event: AstrMessageEvent, content: str, role: str = "assistant"):
        """将消息注入到 LLM 的对话上下文中（方案A）"""
        if not self.enable_inject_context:
            return
        try:
            conv_mgr = self.context.conversation_manager
            unified_msg_origin = event.unified_msg_origin
            cid = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
            if not cid:
                logger.debug("[MatsukoCover] 未找到当前对话ID，无法注入上下文")
                return
            conv = await conv_mgr.get_conversation(unified_msg_origin, cid)
            if not conv:
                logger.debug("[MatsukoCover] 未找到对话对象，无法注入上下文")
                return
            import json
            history = json.loads(conv.history) if conv.history else []
            history.append({"role": role, "content": content})
            await conv_mgr.update_conversation(unified_msg_origin, cid, history=history)
            logger.debug(f"[MatsukoCover] 已注入{role}消息到上下文: {content[:50]}...")
        except Exception as e:
            logger.error(f"[MatsukoCover] 注入上下文失败: {e}")

    async def _notify_llm_with_context(self, event: AstrMessageEvent, song_name: str, status_type: str, result_detail: str = ""):
        """通知 LLM 翻唱结果，并将通知内容注入上下文（方案A+B整合）"""
        if not self.enable_llm_success_notify:
            return None
        
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.debug("[MatsukoCover] 未找到 provider，无法通知 LLM")
                return None
            
            if status_type == "failed":
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）处理失败了！失败原因：{result_detail}。请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个坏消息，并建议用户重试或换一首歌（不要包含系统通知字样）。"
            elif status_type == "cache_hit":
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）命中了缓存，音频已经直接发送给用户了！请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个好消息，可以俏皮地说'这首歌我之前就唱过啦'（不要包含系统通知字样）。"
            else:  # success
                prompt = f"系统通知：刚才用户要求翻唱的歌曲（{song_name}）已经由后台处理完成，并且音频已经发送给用户了！请用你当前的人设（简短、可爱或符合角色的语气）直接告诉用户这个好消息（不要包含系统通知字样）。"
            
            llm_resp = await provider.text_chat(prompt=prompt)
            reply_text = llm_resp.completion_text if llm_resp else ""
            
            if reply_text:
                await event.send(event.plain_result(reply_text))
                # 方案A：将通知内容注入 LLM 上下文
                await self._inject_message_to_context(event, reply_text, role="assistant")
            
            return reply_text
        except Exception as e:
            logger.error(f"[MatsukoCover] 通知 LLM 失败: {e}")
            return None

    def _set_engine_models(self, models: list) -> None:
        """把刷新拿到的模型列表（含别名）写回内存和插件配置。"""
        self.rvc_models_keywords = models
        self.config["rvc_models_keywords"] = models

    def get_models_display_list(self):
        models_keywords = self.rvc_models_keywords
        display_names, key_list = [], []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            display_name = " ".join((alias or os.path.splitext(model_name)[0]).split())
            display_names.append(f"{index}. {display_name}")
            key_list.append(model_name)
        return "\n".join(display_names), key_list

    @staticmethod
    def _format_cache_bytes(value: Any) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "未知"
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TiB"

    async def _backend_cache_request(self, clear: bool = False) -> Any:
        async with self._get_gradio_client(self.rvc_base_url) as client:
            if clear:
                return await self._async_predict(
                    client, scope="all", api_name="/clear_cache", timeout=60
                )
            return await self._async_predict(
                client, api_name="/cache_info", timeout=30
            )

    @staticmethod
    def _parse_cache_target(message: str) -> list[str]:
        """缓存命令只认 RVC 一个后端；传别的词返回空列表，让调用方提示用法。"""
        normalized = str(message or "").strip().lower()
        for command in ("查看翻唱缓存", "清理翻唱缓存"):
            normalized = re.sub(rf"^/?{command}\s*", "", normalized).strip()
        if normalized in ("", "rvc", "all", "全部"):
            return ["rvc"]
        return []

    def get_models_detailed_list(self):
        """获取包含完整信息的模型列表（用于LLM匹配）"""
        models_keywords = self.rvc_models_keywords
        detailed_info = []
        for index, item_str in enumerate(models_keywords, start=1):
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            model_name = parts[0]
            alias = parts[1] if len(parts) > 1 and parts[1] else ""
            name_without_ext = os.path.splitext(model_name)[0]
            detailed_info.append({
                "index": index,
                "filename": model_name,
                "name": name_without_ext,
                "alias": alias,
                "display": alias or name_without_ext
            })
        return detailed_info

    def _find_model_index_by_name(self, model_name_input: str) -> Optional[int]:
        """根据模型名称或别名查找模型序号（支持模糊匹配）"""
        if not model_name_input:
            return None

        models = self.get_models_detailed_list()
        input_lower = model_name_input.lower().strip()

        exact_matches = []
        partial_matches = []

        for model in models:
            filename_lower = model["filename"].lower()
            name_lower = model["name"].lower()
            alias_lower = model["alias"].lower()

            if input_lower == filename_lower or input_lower == name_lower or (alias_lower and input_lower == alias_lower):
                exact_matches.append(model)
            elif input_lower in filename_lower or input_lower in name_lower or (alias_lower and input_lower in alias_lower):
                partial_matches.append(model)

        if exact_matches:
            return exact_matches[0]["index"]
        elif partial_matches:
            best_match = max(partial_matches, key=lambda m: (
                len(m["name"]) if input_lower in m["name"] else 0,
                len(m["alias"]) if m["alias"] and input_lower in m["alias"] else 0
            ))
            return best_match["index"]

        return None

    def _configured_rvc_model_index(self) -> Optional[int]:
        """把配置里下拉选的 rvc_default_model 换成当前 RVC 模型列表里的序号。

        只在调用方确认「用户没有显式指定模型名或序号」之后调用。
        下拉选的模型不在当前列表里（还没 /刷新rvc模型、模型被删了）时返回 None，
        调用方回落到第 1 个模型 / 偏好序号；这里只记一条 warning。
        """
        configured = str(getattr(self, "rvc_default_model", "") or "").strip()
        if not configured:
            return None
        target = configured.lower()
        for model in self.get_models_detailed_list():
            if target in (model["filename"].lower(), model["name"].lower(), model["alias"].lower()):
                return model["index"]
        logger.warning(
            f"rvc_default_model={configured} 不在当前 RVC 模型列表里，"
            f"回落到第 {FALLBACK_MODEL_INDEX} 个模型；"
            "可以用 /刷新rvc模型 重新获取模型列表"
        )
        return None

    def _language_model_index(self, lang: str) -> Optional[int]:
        """把「某语言配的音色」换成当前列表里的序号；没配或找不到都返回 None。

        只负责查表，不负责回落——调用方拿到 None 后按默认音色继续，
        这样「语言没配」不会打断翻唱。
        """
        configured = str((getattr(self, "language_models", {}) or {}).get(lang, "") or "").strip()
        if not configured:
            return None
        target = configured.lower()
        for model in self.get_models_detailed_list():
            if target in (model["filename"].lower(), model["name"].lower(), model["alias"].lower()):
                return model["index"]
        logger.warning(
            f"language_model_{lang}={configured} 不在当前 RVC 模型列表里，"
            f"回落到默认音色；可以用 /刷新rvc模型 重新获取模型列表"
        )
        return None

    def _language_cache_path(self) -> str:
        return os.path.join(_plugin_data_dir(), language_detect.CACHE_FILE_NAME)

    def _language_lyric_bases(self) -> List[str]:
        """歌词先问插件自己配的网易云接口，再问本地点歌服务（3003）。"""
        bases: List[str] = []
        fetcher = getattr(self, "netease_fetcher", None)
        api_base = str(getattr(fetcher, "api_base", "") or "").strip()
        if api_base:
            bases.append(api_base)
        bases.append("http://127.0.0.1:3003")
        return bases

    async def _resolve_language_model(
        self, song: dict, flow_id: str
    ) -> tuple:
        """判断歌曲语言并查出该语言配的音色序号。

        返回 ``(lang, evidence, index, configured_name)``：
        ``lang`` 为空串表示判不出来（纯音乐等），``index`` 为 None 表示这一档没配或模型没了。
        整个过程只读缓存在 plugin_data 里，判不出来也绝不打断翻唱。
        """
        if not getattr(self, "language_model_enable", True):
            return "", "disabled", None, ""
        song_name = song.get("name") or "-"
        try:
            lang, evidence = await asyncio.wait_for(
                language_detect.detect_language(
                    song,
                    self._language_cache_path(),
                    self._language_lyric_bases(),
                ),
                timeout=15,
            )
        except Exception as error:
            logger.warning(f"[CoverFlow:{flow_id}] stage=language_detect | song={song_name} | 判定失败（{error}），用默认音色")
            return "", f"error:{error}", None, ""

        if not lang:
            logger.info(
                f"[CoverFlow:{flow_id}] stage=language_detect | song={song_name} | "
                f"lang=unknown | evidence={evidence} | model=default"
            )
            return "", evidence, None, ""

        configured_name = str((getattr(self, "language_models", {}) or {}).get(lang, "") or "").strip()
        index = self._language_model_index(lang)
        resolved = ""
        if index:
            try:
                resolved = self.get_models_detailed_list()[index - 1]["display"]
            except IndexError:
                resolved = configured_name
        logger.info(
            f"[CoverFlow:{flow_id}] stage=language_detect | song={song_name} | "
            f"lang={lang} | evidence={evidence} | configured={configured_name or '-'} | "
            f"model={resolved or 'default'}"
        )
        return lang, evidence, index, configured_name

    def _parse_gender_map(self, entries: list) -> Dict[str, str]:
        result = {}
        for entry in entries:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            parts = entry.split(":", 1)
            name = parts[0].strip()
            gender = parts[1].strip().lower()
            if name and gender in ("male", "female"):
                result[name] = gender
        return result

    def _add_to_gender_cache(self, cache_key: str, value: str):
        """安全地写入性别缓存，防止无限增长"""
        if len(self._gender_cache) >= self._gender_cache_max_size:
            keys_to_remove = list(self._gender_cache.keys())[:self._gender_cache_max_size // 2]
            for k in keys_to_remove:
                del self._gender_cache[k]
        self._gender_cache[cache_key] = value

    def _detect_artist_gender(self, artist_name: str) -> Optional[str]:
        if not artist_name:
            return None
        artist_lower = artist_name.strip().lower()
        for mapped_name, gender in self.artist_gender_map.items():
            if mapped_name.strip().lower() == artist_lower:
                return gender
        return None

    async def _detect_artist_gender_llm(self, artist_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not artist_name:
            return None
        cache_key = f"artist:{artist_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌手「{artist_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_artist_gender(artist_name)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：歌手「{artist_name}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider（event={'有' if event else '无'}），跳过歌手性别判断")
                return None
            prompt = (
                f"请判断歌手或组合「{artist_name}」的主唱性别。"
                f"如果是组合，请判断主唱（唱主要部分的人）的性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果不确定，请优先根据该名字在日语/华语/欧美流行乐坛的常见用法进行推断，"
                f"不要仅凭字面含义判断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌手「{artist_name}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"「{artist_name}」的主唱是男性还是女性？"
                        f"这是一位歌手或音乐组合，请结合流行音乐常识判断。"
                        f"只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌手「{artist_name}」性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌手「{artist_name}」性别")
        except Exception as e:
            logger.error(f"LLM 判断歌手性别失败: {e}")
        return None

    def _detect_model_gender(self, model_display: str) -> Optional[str]:
        if not model_display:
            return None
        model_lower = model_display.strip().lower()
        for mapped_name, gender in self.model_gender_map.items():
            if mapped_name.strip().lower() == model_lower:
                logger.info(f"自动升降调：模型「{model_display}」在映射表中找到，性别={gender}")
                return gender
        return None

    async def _detect_model_gender_llm(self, model_display: str, event: AstrMessageEvent = None) -> Optional[str]:
        if not model_display:
            return None
        cache_key = f"model:{model_display.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：模型「{model_display}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        mapped = self._detect_model_gender(model_display)
        if mapped:
            self._add_to_gender_cache(cache_key, mapped)
            logger.info(f"自动升降调：模型「{model_display}」在映射表中找到，性别={mapped}")
            return mapped
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过模型性别判断")
                return None
            prompt = (
                f"AI语音模型「{model_display}」模拟的角色是男性还是女性？"
                f"只回答'男'或'女'。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断模型「{model_display}」性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断模型「{model_display}」性别")
        except Exception as e:
            logger.error(f"LLM 判断模型性别失败: {e}")
        return None

    async def _detect_gender_pair(
        self,
        source_name: str,
        model_display: str,
        event: AstrMessageEvent = None,
        source_is_song: bool = False,
    ) -> tuple[Optional[str], Optional[str]]:
        """并发识别来源与模型性别；LLM 迟迟不返回时快速回退。"""
        source_label = "歌曲" if source_is_song else "歌手"

        async def _with_timeout(coro, label: str) -> Optional[str]:
            try:
                return await asyncio.wait_for(
                    coro, timeout=self.gender_detection_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[自动升降调Debug] {label}识别超过 "
                    f"{self.gender_detection_timeout} 秒，已跳过并继续翻唱"
                )
                return None
            except Exception as exc:
                logger.warning(f"[自动升降调Debug] {label}识别失败，已回退: {exc}")
                return None

        source_coro = (
            self._detect_song_gender_llm(source_name, event)
            if source_is_song
            else self._detect_artist_gender_llm(source_name, event)
        )
        logger.info(
            f"[自动升降调Debug] 开始并发识别{source_label}与模型性别 | "
            f"timeout={self.gender_detection_timeout}s | "
            f"source={source_name} | model={model_display}"
        )
        source_gender, model_gender = await asyncio.gather(
            _with_timeout(source_coro, source_label),
            _with_timeout(
                self._detect_model_gender_llm(model_display, event), "模型"
            ),
        )
        logger.info(
            f"[自动升降调Debug] 性别识别结束 | {source_label}={source_gender} | "
            f"模型={model_gender}"
        )
        return source_gender, model_gender

    async def _detect_song_gender_llm(self, song_name: str, event: AstrMessageEvent = None) -> Optional[str]:
        """根据歌曲名判断原唱歌手性别（用于本地音频翻唱的自动升降调）"""
        if not song_name:
            return None
        cache_key = f"song:{song_name.lower().strip()}"
        if cache_key in self._gender_cache:
            logger.info(f"自动升降调：歌曲「{song_name}」性别从缓存获取 → {self._gender_cache[cache_key]}")
            return self._gender_cache[cache_key]
        
        # Step 1: 先尝试从 artist_gender_map 中匹配歌曲名中的歌手名
        song_lower = song_name.lower()
        for artist, gender in self.artist_gender_map.items():
            if artist.lower() in song_lower:
                logger.info(f"自动升降调：歌曲「{song_name}」匹配歌手映射表 '{artist}' → {gender}")
                self._add_to_gender_cache(cache_key, gender)
                return gender
        
        # Step 2: 本地关键词快速推断（常见女声/男声歌曲标记）
        female_song_markers = ["believers", "so'fly", "miku", "初音", "洛天依", "宅舞", " anime", "动漫", "acg", "女声", "女版", "翻唱版(女)", "cover by 女", "version by 女"]
        male_song_markers = ["男声", "男版", "cover by 男", "version by 男"]
        if any(m in song_lower for m in female_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配女声关键词 → female")
            self._add_to_gender_cache(cache_key, "female")
            return "female"
        if any(m in song_lower for m in male_song_markers):
            logger.info(f"自动升降调：歌曲「{song_name}」匹配男声关键词 → male")
            self._add_to_gender_cache(cache_key, "male")
            return "male"
        
        # Step 3: LLM 判断（兜底）
        try:
            if event:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            else:
                provider = self.context.get_using_provider()
            if not provider:
                logger.warning(f"自动升降调：无法获取LLM provider，跳过歌曲性别判断")
                return None
            prompt = (
                f"请判断歌曲「{song_name}」的原唱歌手性别。"
                f"只回答'男'或'女'，不要回答其他任何内容。"
                f"如果是日语/动漫/JPOP歌曲，通常女声演唱者较多；如果是华语流行摇滚，男性演唱者较多。"
                f"如果不确定，请优先根据该歌曲在JPOP/动漫/华语流行乐坛的常见演唱者进行推断。"
            )
            resp = await provider.text_chat(prompt=prompt)
            if resp and resp.completion_text:
                answer = resp.completion_text.strip()
                logger.info(f"自动升降调：LLM判断歌曲「{song_name}」原唱性别 → {answer}")
                if "女" in answer:
                    self._add_to_gender_cache(cache_key, "female")
                    return "female"
                elif "男" in answer:
                    self._add_to_gender_cache(cache_key, "male")
                    return "male"
                else:
                    prompt2 = (
                        f"歌曲「{song_name}」的原唱是男性还是女性？"
                        f"这是一首JPOP/动漫/流行歌曲，请结合音乐常识判断。只回答'男'或'女'。"
                    )
                    resp2 = await provider.text_chat(prompt=prompt2)
                    if resp2 and resp2.completion_text:
                        answer2 = resp2.completion_text.strip()
                        logger.info(f"自动升降调：LLM二次判断歌曲「{song_name}」原唱性别 → {answer2}")
                        if "女" in answer2:
                            self._add_to_gender_cache(cache_key, "female")
                            return "female"
                        elif "男" in answer2:
                            self._add_to_gender_cache(cache_key, "male")
                            return "male"
            else:
                logger.warning(f"自动升降调：LLM返回为空，无法判断歌曲「{song_name}」原唱性别")
        except Exception as e:
            logger.error(f"LLM 判断歌曲原唱性别失败: {e}")
        return None

    async def _calc_auto_key_shift(self, artist_name: str, model_display: str, user_key_shift: int, artist_gender: Optional[str] = None, model_gender: Optional[str] = None, event: AstrMessageEvent = None, detection_attempted: bool = False) -> int:
        if not self.enable_auto_key_shift:
            return user_key_shift
        if user_key_shift != 0:
            return user_key_shift

        if not artist_gender and not detection_attempted:
            artist_gender = await self._detect_artist_gender_llm(artist_name, event)
        if not model_gender and not detection_attempted:
            model_gender = await self._detect_model_gender_llm(model_display, event)

        gender_label = {"male": "男", "female": "女", None: "未知"}
        final_shift = user_key_shift
        debug_reason = ""

        if not artist_gender or not model_gender:
            logger.info(f"自动升降调：无法确定性别（歌手={artist_gender}, 模型={model_gender}），跳过")
            debug_reason = "无法确定性别，跳过自动升降调"
        elif artist_gender == model_gender:
            logger.info(f"自动升降调：歌手和模型性别相同（{artist_gender}），无需调整")
            debug_reason = f"性别相同({gender_label.get(artist_gender, '?')})，无需调整"
        elif model_gender == "female" and artist_gender == "male":
            final_shift = self.male_to_female_shift
            logger.info(f"自动升降调：男→女，升调 {final_shift}")
            debug_reason = f"男歌手 → 女模型，自动升调 {final_shift:+d}"
        elif model_gender == "male" and artist_gender == "female":
            final_shift = self.female_to_male_shift
            logger.info(f"自动升降调：女→男，降调 {final_shift}")
            debug_reason = f"女歌手 → 男模型，自动降调 {final_shift:+d}"
        else:
            debug_reason = "未匹配到自动升降调规则"

        if self.enable_auto_key_shift_debug and event:
            try:
                debug_msg = (
                    f"🎵 [自动升降调Debug]\n"
                    f"歌手：{artist_name} → {gender_label.get(artist_gender, '未知')}\n"
                    f"模型：{model_display} → {gender_label.get(model_gender, '未知')}\n"
                    f"结果：{debug_reason}"
                )
                await event.send(event.plain_result(debug_msg))
            except Exception:
                pass

        return final_shift

    async def _fetch_models_info(self):
        """拉一次 /model_info，返回 (info, models)。

        中间层 0.9.0 才有 ``/model_info``（带目录、数量、索引目录）；老版本没有就退回
        ``/show_model``，功能不受影响，只是拿不到目录信息。
        """
        info = {}
        models = None
        async with self._get_gradio_client(self.rvc_base_url) as client:
            if self._client_supports_api(client, "/model_info") is not False:
                try:
                    info = await self._async_predict(
                        client,
                        models_dir=self.rvc_models_dir,
                        api_name="/model_info",
                        timeout=60,
                    )
                except Exception as e:
                    logger.warning(f"读取模型目录失败，改用 /show_model: {e}")
                    info = {}

            if isinstance(info, dict) and isinstance(info.get("models"), list):
                models = info["models"]
            else:
                try:
                    models = await self._async_predict(client, api_name="/show_model")
                except Exception as e:
                    logger.warning(f"第一次获取模型列表失败，重试中: {e}")
                    models = await self._async_predict(client, api_name="/show_model")

        if not isinstance(models, list):
            raise ValueError(f"获取模型列表失败: {models}")

        if isinstance(info, dict) and info:
            self._models_dir_info = info
        return (info if isinstance(info, dict) else {}), models

    async def _startup_refresh_models(self) -> None:
        """启动后延迟几秒再拉模型，给中间层留出就绪时间；失败不影响启动。"""
        try:
            await asyncio.sleep(8)
            await self._update_models_from_api()
            logger.info("启动时已同步模型下拉选项")
        except Exception as error:
            logger.warning(f"启动同步模型列表失败（不影响使用）：{error}")

    async def _update_models_from_api(self):
        """从 RVC 后端拉一遍模型列表，保留已配的别名后写回插件配置。"""
        info, model_list_from_api = await self._fetch_models_info()

        if info.get("dir"):
            logger.info(
                "RVC 模型目录：%s（%d 个 .pth，来源：%s）",
                info.get("dir"),
                int(info.get("count") or 0),
                info.get("source") or "中间层",
            )
            if info.get("warning"):
                logger.warning("RVC 模型目录有疑点：%s", info["warning"])

        old_aliases = {}
        for item_str in self.rvc_models_keywords:
            parts = item_str.split(MODEL_ALIAS_SEPARATOR, 1)
            if len(parts) > 1:
                old_aliases[parts[0]] = parts[1]

        new_models_list = [
            f"{m}{MODEL_ALIAS_SEPARATOR}{old_aliases.get(m, '') or ''}"
            for m in model_list_from_api
        ]

        self._set_engine_models(new_models_list)
        self._sync_schema_model_options(
            [item.split(MODEL_ALIAS_SEPARATOR, 1)[0] for item in new_models_list]
        )

        self.config.save_config()
        logger.info(f"RVC 模型列表已更新并成功保存，共 {len(new_models_list)} 个")

    def _sync_schema_model_options(self, filenames: List[str]) -> None:
        """把当前模型列表写回 ``_conf_schema.json`` 的下拉选项（0.13.0）。

        AstrBot 的设置页只认 schema 里静态的 ``options``，运行时刷新只更新内存列表
        是不够的：用户刷新完打开设置页还是旧选项。这里在 /刷新rvc模型 之后顺带把
        ``rvc_default_model`` 和三个语言音色下拉的选项一起重写，下次重载插件即可看到。
        只改选项，不动任何配置值和说明文字。
        """
        if not filenames:
            return
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_conf_schema.json")
        try:
            with open(schema_path, "r", encoding="utf-8") as handle:
                schema = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"更新设置下拉选项失败（读不到 schema）：{error}")
            return
        if not isinstance(schema, dict):
            return
        changed = False
        for key in ("rvc_default_model", "language_model_zh", "language_model_ja", "language_model_en"):
            item = schema.get(key)
            if not isinstance(item, dict):
                continue
            options = list(filenames)
            if item.get("options") != options:
                item["options"] = options
                changed = True
        if not changed:
            return
        try:
            tmp_path = schema_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(schema, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, schema_path)
            logger.info(
                f"已把 {len(filenames)} 个模型写入设置里的四个音色下拉选项；"
                "重载插件后设置页即可看到新模型"
            )
        except OSError as error:
            logger.warning(f"写入设置下拉选项失败（不影响本次翻唱）：{error}")

    @staticmethod
    def _client_supports_api(client, api_name: str) -> Optional[bool]:
        """Inspect the fetched Gradio config without issuing an extra request."""
        endpoints = getattr(client, "endpoints", None)
        if not endpoints:
            return None
        values = endpoints.values() if isinstance(endpoints, dict) else endpoints
        names = []
        for endpoint in values:
            name = getattr(endpoint, "api_name", None)
            if name:
                names.append("/" + str(name).lstrip("/"))
        if not names:
            return None
        return "/" + api_name.lstrip("/") in names

    @staticmethod
    def _client_api_parameter_names(client, api_name: str) -> Optional[Set[str]]:
        """Return keyword parameter names advertised for one Gradio endpoint."""
        endpoints = getattr(client, "endpoints", None)
        if not endpoints:
            return None
        values = endpoints.values() if isinstance(endpoints, dict) else endpoints
        expected = "/" + api_name.lstrip("/")
        for endpoint in values:
            name = getattr(endpoint, "api_name", None)
            if not name or "/" + str(name).lstrip("/") != expected:
                continue
            parameters = getattr(endpoint, "parameters_info", None)
            if parameters is None:
                return None
            return {
                str(item["parameter_name"])
                for item in parameters
                if isinstance(item, dict) and item.get("parameter_name")
            }
        return None

    def _get_render_kwargs(self, client) -> Dict[str, Any]:
        """把「唱多长」的模式传给支持它的后端（chorus-mw 0.7.0 起）。

        老后端没有 render_mode 这个参数，按名字探测一下：缺了就整组不传，
        由后端按默认「副歌 45 秒」处理，不会因为多传参数报 invalid keyword。
        """
        parameter_names = self._client_api_parameter_names(client, "/convert")
        if not parameter_names or "render_mode" not in parameter_names:
            if self.render_mode != "chorus" and not getattr(self, "_render_mode_warned", False):
                self._render_mode_warned = True
                logger.warning(
                    f"当前 RVC 中间层不支持 render_mode，{self.render_mode} 未生效"
                    "（按副歌模式处理）；把 chorus-mw 升级到 0.7.0 并重启 3333 后可用"
                )
            return {}
        values = {"render_mode": self.render_mode}
        if "chorus_seconds" in parameter_names:
            values["chorus_seconds"] = self.chorus_seconds
        return values

    def _get_mix_kwargs(self, client) -> Dict[str, Any]:
        """把采样/混音参数按名字传给支持它们的后端（chorus-mw 0.8.0 起）。

        这几个是 0.8.0 才加到 /convert 的：按名字探测，后端没有就不传，
        由后端用自己的默认值，避免老后端因为「不认识的参数」整次失败。
        传过去的都是真实配置值，不是占位符。
        """
        values = {
            "rms_mix_rate": self.rms_mix_rate,
            "protect": self.protect,
            "inst_volume_db": self.inst_volume_db,
            "vocal_volume_db": self.vocal_volume_db,
        }
        parameter_names = self._client_api_parameter_names(client, "/convert")
        if parameter_names is None:
            if not getattr(self, "_mix_kwargs_warned", False):
                self._mix_kwargs_warned = True
                logger.warning(
                    "读不到后端 /convert 的参数表，本次不提交采样/混音参数"
                    "（后端会用自身默认值）；确认 rvc_base_url 可访问后重试"
                )
            return {}
        missing = [key for key in values if key not in parameter_names]
        if missing:
            if not getattr(self, "_mix_kwargs_warned", False):
                self._mix_kwargs_warned = True
                logger.warning(
                    f"当前 RVC 中间层不支持 {', '.join(missing)}，这些设置本次不生效；"
                    "把 chorus-mw 升级到 0.8.0 并重启 3333 后可用"
                )
            return {key: value for key, value in values.items() if key in parameter_names}
        return values


    async def _get_msst_models_from_client(self, client) -> List[Dict[str, str]]:
        result = await self._async_predict(
            client, api_name="/show_msst_models", timeout=30
        )
        if not isinstance(result, list):
            raise ValueError(f"后端返回格式错误: {result}")

        models = []
        for item in result:
            if isinstance(item, dict) and item.get("id"):
                models.append({
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                })
            elif isinstance(item, str):
                models.append({"id": item, "name": item})
        if not models:
            raise ValueError("后端没有发现已安装的 MSST 模型")
        return models

    async def _get_optional_msst_kwargs(self, client, base_url: str) -> Dict[str, Any]:
        """Only send the new parameter to an MSST-capable backend."""
        supports = self._client_supports_api(client, "/show_msst_models")
        if supports is True:
            return {"msst_model": self.msst_default_model}
        if supports is False:
            logger.info(f"后端 {base_url} 没有 MSST 模型接口，本次不提交 MSST 参数")
            return {}

        try:
            await self._get_msst_models_from_client(client)
            return {"msst_model": self.msst_default_model}
        except Exception:
            logger.info(f"后端 {base_url} 无法确认 MSST 能力，省略 msst_model 参数")
            return {}

    async def _get_msst_models_from_api(self) -> List[Dict[str, str]]:
        """读取 RVC 后端公开的 MSST 分离模型白名单。"""
        base_url = self.rvc_base_url
        async with self._get_gradio_client(base_url) as client:
            supports = self._client_supports_api(client, "/show_msst_models")
            if supports is False:
                raise RuntimeError("当前 RVC 后端没有 MSST 模型列表接口")
            models = await self._get_msst_models_from_client(client)
        logger.info(
            f"[MSST模型列表Debug] 插件已从 {base_url} 获取 "
            f"{len(models)} 个模型: {', '.join(item['id'] for item in models)}"
        )
        return models

    async def _sync_msst_model_selection(self, model_id: str) -> Optional[str]:
        """通知 RVC 中间层切换分离模型，返回确认的地址（没确认就给 None）。"""
        base_url = self.rvc_base_url
        try:
            async with self._get_gradio_client(base_url) as client:
                supports = self._client_supports_api(client, "/select_msst_model")
                if supports is False:
                    return None
                result = await self._async_predict(
                    client,
                    model_name=model_id,
                    api_name="/select_msst_model",
                    timeout=30,
                )
                if isinstance(result, dict) and result.get("success"):
                    logger.info(
                        f"[MSST模型切换Debug] 中间层 {base_url} 已确认切换为 {model_id}"
                    )
                    return base_url
        except Exception as exc:
            logger.debug(f"同步 MSST 模型到后端 {base_url} 失败，已忽略: {exc}")
        return None

    # ==================== 命令处理（支持LLM强制模式） ====================

    @filter.command("翻唱帮助")
    async def cover_help(self, event: AstrMessageEvent):
        """Show the RVC-only entry points for the cover workflow.

        Args:
            event: Message requesting the help card.
        """
        await self.selection_ui.send_card(
            event,
            "🎵 RVC 翻唱",
            "翻唱：/rvc <歌名> [升降调]\n"
            "本地音频：发送音频后使用 /本地翻唱\n\n"
            "取歌按插件配置的 source_order 依次试\n"
            "网易云 → 酷狗 → 酷我 → QQ 音乐。\n"
            "点歌后依次选择歌曲和音色，翻唱完成后发送音频；\n"
            "选择菜单支持按钮、序号、翻页和取消。\n"
            "LLM 强制模式下，请直接说“帮我翻唱《歌名》”。\n\n"
            "装完跑一次 /环境自检，哪里红一眼看到。",
            actions=[
                ("RVC 音色", "刷新rvc模型"),
                ("环境自检", "环境自检"),
                ("分离模型", "列出msst模型"),
                ("翻唱任务", "查看翻唱任务"),
                ("我的统计", "我的翻唱统计"),
            ],
        )
    @filter.command("查看翻唱缓存")
    async def show_cover_cache(self, event: AstrMessageEvent):
        """查看 RVC 中间层当前缓存占用。"""
        if not self._parse_cache_target(event.message_str):
            yield event.plain_result("用法：/查看翻唱缓存")
            return
        yield event.plain_result("🔎 正在读取中间层缓存状态，请稍候...")
        lines = []
        try:
            info = await self._backend_cache_request(clear=False)
            if not isinstance(info, dict):
                raise RuntimeError(f"后端返回了无法识别的数据: {info!r}")
            lines.append(
                f"RVC: {int(info.get('total_files', 0))} 个文件 / "
                f"{self._format_cache_bytes(info.get('total_bytes', 0))}"
            )
            for area, stats in (info.get("areas") or {}).items():
                lines.append(
                    f"  - {area}: {int(stats.get('files', 0))} 个 / "
                    f"{self._format_cache_bytes(stats.get('bytes', 0))}"
                )
        except Exception as exc:
            lines.append(f"RVC: 无法读取（{exc}）")
        await self.selection_ui.send_card(
            event, "📦 翻唱缓存", "📦 翻唱缓存状态：\n" + "\n".join(lines)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清理翻唱缓存")
    async def clear_cover_cache(self, event: AstrMessageEvent):
        """清理 RVC 中间层的结果、分离和下载缓存。"""
        if not self._parse_cache_target(event.message_str):
            yield event.plain_result("用法：/清理翻唱缓存")
            return
        yield event.plain_result("🧹 正在安全清理中间层缓存，请稍候...")
        lines = []
        try:
            result = await self._backend_cache_request(clear=True)
            if not isinstance(result, dict):
                raise RuntimeError(f"后端返回了无法识别的数据: {result!r}")
            lines.append(
                f"RVC: 删除 {int(result.get('deleted_files', 0))} 个文件，"
                f"释放 {self._format_cache_bytes(result.get('freed_bytes', 0))}"
            )
        except Exception as exc:
            lines.append(f"RVC: 清理失败（{exc}）")
        await self.selection_ui.send_card(
            event, "🧹 缓存清理结果", "✅ 缓存清理完成：\n" + "\n".join(lines)
        )
    @filter.command("列出msst模型")
    async def list_msst_models(self, event: AstrMessageEvent):
        """列出当前 RVC 中间层安装的人声分离模型。"""
        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            yield event.plain_result(f"ℹ️ 当前后端没有可列出的 MSST 模型：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        await self.selection_ui.send_card(
            event,
            "🎛️ MSST 分离模型",
            "可用的 MSST 人声分离模型：\n"
            + "\n".join(lines)
            + "\n\n管理员可使用：/切换msst模型 <序号或模型名>",
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("切换msst模型")
    async def switch_msst_model(self, event: AstrMessageEvent):
        """查看或切换 RVC 中间层的默认人声分离模型。"""
        raw = event.message_str.strip()
        requested = re.sub(r"^/?切换msst模型\s*", "", raw, flags=re.IGNORECASE).strip()

        yield event.plain_result("🔄 正在读取 MSST 模型列表，请稍候...")
        try:
            models = await self._get_msst_models_from_api()
        except Exception as exc:
            logger.error(f"获取 MSST 模型列表失败: {exc}")
            yield event.plain_result(f"❌ 无法读取 MSST 模型列表：{exc}")
            return

        lines = [
            f"{index}. {item['name']}\n   {item['id']}"
            + ("  ← 当前默认" if item["id"] == self.msst_default_model else "")
            for index, item in enumerate(models, 1)
        ]
        if not requested:
            yield event.plain_result(
                "当前 MSST 默认模型："
                f"{self.msst_default_model}\n\n可用模型：\n"
                + "\n".join(lines)
                + "\n\n用法：/切换msst模型 <序号或模型名>"
            )
            return

        selected = None
        if requested.isdigit():
            model_index = int(requested) - 1
            if 0 <= model_index < len(models):
                selected = models[model_index]
        else:
            lowered = requested.lower()
            exact = [
                item for item in models
                if lowered in (item["id"].lower(), item["name"].lower())
            ]
            partial = [
                item for item in models
                if lowered in item["id"].lower() or lowered in item["name"].lower()
            ]
            candidates = exact or partial
            if len(candidates) == 1:
                selected = candidates[0]

        if selected is None:
            yield event.plain_result(
                f"❌ 没有唯一匹配的 MSST 模型：{requested}\n\n" + "\n".join(lines)
            )
            return

        self.msst_default_model = selected["id"]
        synced_backend = await self._sync_msst_model_selection(self.msst_default_model)
        sync_text = f"\n中间层确认：{synced_backend}" if synced_backend else ""
        await self.selection_ui.send_card(
            event,
            "✅ MSST 模型设置",
            f"✅ 已通知中间层切换分离模型：{selected['name']}\n"
            f"模型 ID：{selected['id']}\n"
            "注意：chorus-mw 0.8.0 的分离模型由后端启动配置固定，"
            "这个选择只在声明支持 msst_model 参数的中间层上生效。" + sync_text,
        )

    def _models_dir_note(self) -> str:
        """把「音色模型 / 索引放在哪个文件夹」压成几行给人看（0.12.0 起带索引目录）。"""
        info = getattr(self, "_models_dir_info", None) or {}
        directory = info.get("dir") or self.rvc_models_dir
        if not directory:
            return ""
        lines = [f"\n📁 音色模型：{directory}"]
        if info:
            lines.append(
                f"（{info.get('count', 0)} 个 .pth，来源：{info.get('source', '中间层')}）"
            )
        elif self.rvc_models_dir:
            lines.append("（还没核对过，发一次 /刷新rvc模型 可以确认中间层是否在用这个目录）")
        index_dirs = info.get("index_dirs") or []
        if index_dirs:
            lines.append("🗂 索引文件：" + "；".join(index_dirs))
        warning = info.get("warning")
        if warning:
            lines.append(f"\n⚠️ {warning}")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rvc模型文件夹")
    async def open_rvc_models_dir(self, event: AstrMessageEvent):
        """显示音色模型和索引的真实目录，并直接在跑机器人的这台机器上打开模型文件夹。"""
        try:
            info, _models = await self._fetch_models_info()
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"❌ 拿不到模型目录：{e}")
            return

        directory = info.get("dir") or self.rvc_models_dir
        if not directory:
            yield event.plain_result("❌ 中间层没返回模型目录，先确认 3333 在跑、版本 ≥0.9.0。")
            return

        lines = [f"📁 音色模型：{directory}"]
        if info:
            lines.append(f"（{info.get('count', 0)} 个 .pth，来源：{info.get('source', '中间层')}）")
        index_dirs = info.get("index_dirs") or []
        if index_dirs:
            lines.append("🗂 索引文件：" + "；".join(index_dirs))
        lines.append("把 .pth 丢进上面那个目录，.index 放进索引目录，然后发 /刷新rvc模型。")

        opened = False
        try:
            if os.name == "nt" and os.path.isdir(directory):
                os.startfile(directory)  # noqa: S606  只在管理员命令里打开，路径来自中间层
                opened = True
        except Exception as e:
            logger.warning(f"打开模型文件夹失败: {e}")
        if opened:
            lines.append("（已在这台机器上把文件夹打开）")
        if info.get("warning"):
            lines.append(f"⚠️ {info['warning']}")

        yield event.plain_result("\n".join(lines))

    def _check_paths(self, models_dir: str) -> List[str]:
        """按模型目录反推 RVC 整合包根目录，检查运行时、ffmpeg 和中间层二进制。"""
        lines: List[str] = []
        root = ""
        if models_dir:
            root = os.path.dirname(os.path.dirname(os.path.abspath(models_dir)))
        if not root or not os.path.isdir(root):
            lines.append("❌ 推断不出 RVC 整合包目录（模型目录还不可用）")
            return lines
        lines.append(f"✅ RVC 整合包：{root}")

        runtime_python = os.path.join(root, "runtime", "python.exe")
        lines.append(("✅ " if os.path.isfile(runtime_python) else "❌ ") + f"RVC 运行时：{runtime_python}")

        ffmpeg = os.path.join(root, "ffmpeg.exe")
        lines.append(("✅ " if os.path.isfile(ffmpeg) else "❌ ") + f"ffmpeg：{ffmpeg}")

        # 中间层一般和整合包并排（或再上一层）；rubberband 在它的 bin 目录里
        parent = os.path.dirname(root)
        grandparent = os.path.dirname(parent)
        for candidate in (
            os.path.join(parent, "chorus-mw"),
            os.path.join(grandparent, "chorus-mw"),
            os.path.join(root, "chorus-mw"),
            os.environ.get("CHORUS_DIR", ""),
        ):
            if not candidate:
                continue
            rubber = os.path.join(candidate, "bin", "rubberband.exe")
            if os.path.isfile(rubber):
                lines.append(f"✅ 升降调：{rubber}")
                break
        else:
            lines.append("⚠️ 没找到 rubberband.exe（chorus-mw\\bin 下）；升降调功能会不可用，翻唱仍可跑")
        return lines

    @staticmethod
    def _scan_model_library(root: str) -> List[dict]:
        """扫分类库（纯逻辑在 model_library.py，这里只是转发）。"""
        return model_library.scan_model_library(root)

    def _link_into_assets(self, source: str, target: str) -> str:
        """同盘硬链接、跨盘复制（纯逻辑在 model_library.py）。"""
        return model_library.link_into_assets(source, target)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("同步音色库")
    async def sync_model_library(self, event: AstrMessageEvent):
        """把分类库里的模型硬链接进 RVC 的 assets，再刷新列表（0.14.0）。"""
        root = str(getattr(self, "model_library_dir", "") or "").strip()
        if not root:
            yield event.plain_result(
                "❌ 还没配分类音色库目录。\n"
                "去插件设置里把「分类音色库目录」填成你的库根目录"
                "（例如 ...\\GPT SoVITS-RCV 语音模型\\RVC），再来发这条命令。"
            )
            return
        if not os.path.isdir(root):
            yield event.plain_result(f"❌ 目录不存在：{root}\n检查一下路径，或先在设置里改成正确的。")
            return

        try:
            info, _models = await self._fetch_models_info()
        except Exception as error:
            yield event.plain_result(f"❌ 中间层不可达，先让它跑起来再同步：{error}")
            return
        weights_dir = str(info.get("dir") or self.rvc_models_dir or "")
        index_dirs = list(info.get("index_dirs") or [])
        if not weights_dir:
            yield event.plain_result("❌ 中间层没返回模型目录，先把中间层升级到 ≥0.9.0 再来。")
            return
        indices_dir = index_dirs[0] if index_dirs else ""

        files = self._scan_model_library(root)
        weights = [item for item in files if item["name"].lower().endswith(".pth")]
        indices = [item for item in files if item["name"].lower().endswith(".index")]
        if not weights:
            yield event.plain_result(f"❌ 库里一个 .pth 都没找到：{root}")
            return

        linked = copied = skipped = 0
        warnings: List[str] = []
        for item in weights:
            target = os.path.join(weights_dir, item["name"])
            try:
                action = self._link_into_assets(item["path"], target)
            except Exception as error:
                warnings.append(f"{item['name']}：{error}")
                continue
            if action == "linked":
                linked += 1
            elif action == "copied":
                copied += 1
            else:
                skipped += 1

        index_linked = 0
        if indices_dir:
            for item in indices:
                target = os.path.join(indices_dir, item["name"])
                try:
                    if self._link_into_assets(item["path"], target) in ("linked", "copied"):
                        index_linked += 1
                except Exception as error:
                    warnings.append(f"{item['name']}：{error}")
            index_stems = {os.path.splitext(item["name"])[0].lower() for item in weights}
            for item in indices:
                stem = os.path.splitext(item["name"])[0].lower()
                if not any(stem in known or known in stem for known in index_stems):
                    warnings.append(
                        f"索引 {item['name']} 的文件名里没有对应模型名，RVC 可能匹配不到"
                    )
        else:
            warnings.append("中间层没给索引目录，索引这次没同步")

        try:
            await self._update_models_from_api()
            refreshed = f"✅ 已刷新模型列表，当前 {len(self.rvc_models_keywords)} 个"
        except Exception as error:
            refreshed = f"⚠️ 链接建好了，但刷新模型列表失败：{error}"

        lines = [
            "🗂 同步音色库",
            f"库目录：{root}",
            f"扫描到 {len(weights)} 个模型、{len(indices)} 个索引",
            f"新建硬链接 {linked} 个、跨盘复制 {copied} 个、已存在跳过 {skipped} 个",
            f"索引链接 {index_linked} 个",
            refreshed,
        ]
        if warnings:
            lines.append("")
            lines.append("需要注意：")
            lines.extend(f"· {item}" for item in warnings[:8])
        yield event.plain_result("\n".join(lines))

    def _find_chorus_dir(self, models_dir: str = "") -> str:
        """推断中间层目录：环境变量优先，其次整合包的上两级、上三级。"""
        root = ""
        if models_dir:
            root = os.path.dirname(os.path.dirname(os.path.abspath(models_dir)))
        candidates: List[str] = []
        if os.environ.get("CHORUS_DIR"):
            candidates.append(os.environ["CHORUS_DIR"])
        if root:
            candidates.append(os.path.join(os.path.dirname(root), "chorus-mw"))
            candidates.append(os.path.join(os.path.dirname(os.path.dirname(root)), "chorus-mw"))
            candidates.append(os.path.join(root, "chorus-mw"))
        for candidate in candidates:
            if candidate and os.path.isfile(os.path.join(candidate, "app.py")):
                return candidate
        return ""

    def _backend_port(self) -> int:
        try:
            return int(str(self.rvc_base_url).split(":")[-1].strip("/ "))
        except (ValueError, IndexError):
            return 3333

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重启中间层")
    async def restart_middleware(self, event: AstrMessageEvent):
        """停掉再拉起 chorus-mw（0.14.0），省得小白去命令行。"""
        if os.name != "nt":
            yield event.plain_result("这条命令只在运行机器人的 Windows 主机上可用。")
            return
        info: dict = {}
        try:
            info, _models = await self._fetch_models_info()
        except Exception:
            info = {}
        models_dir = str((info or {}).get("dir") or self.rvc_models_dir or "")
        chorus = self._find_chorus_dir(models_dir)
        if not chorus:
            yield event.plain_result(
                "❌ 没找到中间层目录（里面要有 app.py 和 scripts\\start.ps1）。\n"
                "把它放在 RVC 整合包旁边，或设环境变量 CHORUS_DIR 指向它。"
            )
            return
        starter = os.path.join(chorus, "scripts", "start.ps1")
        if not os.path.isfile(starter):
            yield event.plain_result(f"❌ 中间层里没有启动脚本：{starter}")
            return

        port = self._backend_port()
        yield event.plain_result(f"🔄 正在重启中间层（端口 {port}）…")

        def _run(script_args: List[str]) -> str:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", starter] + script_args,
                cwd=chorus,
                capture_output=True,
                timeout=180,
                creationflags=flags,
            )
            out = (proc.stdout or b"").decode("utf-8", "ignore")
            err = (proc.stderr or b"").decode("utf-8", "ignore")
            return (out + "\n" + err).strip()[-400:]

        try:
            await asyncio.to_thread(_run, ["-Stop"])
        except Exception as error:
            logger.warning(f"停止中间层时报错（继续尝试启动）：{error}")

        for _ in range(15):
            if not await self._probe_local_port(f"http://127.0.0.1:{port}", timeout=1.5):
                break
            await asyncio.sleep(1)

        try:
            await asyncio.to_thread(_run, [])
        except Exception as error:
            yield event.plain_result(f"❌ 启动脚本执行失败：{error}")
            return

        for _ in range(60):
            if await self._probe_local_port(f"http://127.0.0.1:{port}", timeout=1.5):
                yield event.plain_result(f"✅ 中间层已重启，{port} 在听。发 /环境自检 复核一下。")
                return
            await asyncio.sleep(3)
        yield event.plain_result(
            f"⚠️ 已执行重启，但 180 秒内 {port} 没起来。去中间层目录看 _work\\mw.log 的报错。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("环境自检")
    async def diagnose_environment(self, event: AstrMessageEvent):
        """一次性检查后端、模型、运行时、ffmpeg、取歌服务，给小白一句结论。"""
        lines = ["🔍 环境自检"]
        problems = 0

        info: dict = {}
        models: list = []
        try:
            info, models = await self._fetch_models_info()
            lines.append(f"✅ 中间层 {self.rvc_base_url} 可达，模型 {len(models)} 个")
        except Exception as error:
            problems += 1
            lines.append(f"❌ 中间层不可达（{self.rvc_base_url}）：{error}")
            lines.append("   → 去插件目录双击 install.bat，或手动跑 chorus-mw\\scripts\\start.ps1")

        models_dir = str((info or {}).get("dir") or self.rvc_models_dir or "")
        if info:
            lines.extend(self._check_paths(models_dir))

        if models:
            names = {str(item) for item in models}
            configured = [
                ("中文", self.language_models.get("zh", "")),
                ("日语", self.language_models.get("ja", "")),
                ("英文", self.language_models.get("en", "")),
            ]
            for label, value in configured:
                if not value:
                    lines.append(f"⚠️ {label}音色没配：这类歌会用默认音色")
                elif value not in names:
                    problems += 1
                    lines.append(f"❌ {label}音色配的是 {value}，但模型列表里没有它")
                else:
                    lines.append(f"✅ {label}音色：{value}")
            default_model = str(getattr(self, "rvc_default_model", "") or "")
            if default_model and default_model not in names:
                problems += 1
                lines.append(f"❌ 默认音色 {default_model} 不在模型列表里，发一次 /刷新rvc模型")
        else:
            problems += 1
            lines.append("❌ 一个音色模型都没有：把 .pth 放进模型目录后发 /刷新rvc模型")

        netease_ok = await self._probe_local_port("http://127.0.0.1:3003")
        if netease_ok:
            lines.append("✅ 取歌服务 3003 在线")
        else:
            lines.append("⚠️ 取歌服务 3003 不在线（可选）：没有它就只能用公共接口取歌，会员歌和高音质可能失败")

        lines.append("")
        lines.append("✅ 结论：可以翻唱" if problems == 0 else f"⚠️ 有 {problems} 项要修，按上面带 ❌ 的提示处理")
        yield event.plain_result("\n".join(lines))

    @staticmethod
    async def _probe_local_port(url: str, timeout: float = 4.0) -> bool:
        def _probe() -> bool:
            import urllib.request

            try:
                request = urllib.request.Request(url, headers={"User-Agent": "chorus-cover-check"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return 200 <= response.status < 500
            except Exception:
                return False

        try:
            return await asyncio.to_thread(_probe)
        except Exception:
            return False

    @filter.command("刷新rvc模型")
    async def refresh_rvc_models(self, event: AstrMessageEvent):
        yield event.plain_result("正在刷新 RVC 模型列表，请稍候...")
        try:
            await self._update_models_from_api()
            yield event.plain_result("刷新成功！")
            display_str, _ = self.get_models_display_list()
            display_str = display_str or "未发现任何模型。"
            note = self._models_dir_note()
            await self.selection_ui.send_list(event, f"当前 RVC 可用模型：\n{display_str}{note}")
        except Exception as e:
            logger.error(traceback.format_exc())
            yield event.plain_result(f"刷新 RVC 模型出错了: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置rvc后端链接")
    async def set_rvc_url(self, event: AstrMessageEvent):
        args = event.message_str.replace("设置rvc后端链接", "").strip().split()
        if not args:
            yield event.plain_result(f"当前 RVC 后端: {self.rvc_base_url}\n用法: /设置rvc后端链接 <URL>")
            return
        _url = args[0]
        if not _url.endswith("/"): _url += "/"
        self.rvc_base_url = _url
        self.config["rvc_base_url"] = _url
        self.config.save_config()
        yield event.plain_result(f"RVC 后端链接已设置为: {_url}")

    @filter.command("rvc")
    async def rvc(self, event: AstrMessageEvent):
        if not self.enable_rvc:
            yield event.plain_result("❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。")
            return
        if self.llm_force_mode:
            yield event.plain_result("当前已开启 LLM 强制模式，/rvc 命令已被禁用。\n请直接对我说'翻唱《歌名》'，我会自动帮您处理！")
            return
        async for result in self._handle_cover(event):
            yield result

    @filter.command("本地翻唱")
    async def local_audio_cover_cmd(self, event: AstrMessageEvent):
        if not self.enable_local_audio_cover:
            yield event.plain_result("❌ 本地音频翻唱功能未启用。")
            return

        audio_path = await self._extract_audio_from_event(event)

        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]

        if not audio_path:
            yield event.plain_result(
                "❌ 未检测到音频文件或语音消息。\n\n用法：\n"
                "1. 在同一条消息中发送音频+文字 /本地翻唱\n"
                "2. 先发送语音/音频，然后在5分钟内发送 /本地翻唱\n\n"
                "可选参数：[模型名/序号] [升降调]\n"
                "示例：/本地翻唱 官方示例音色 0"
            )
            return

        args = event.message_str.replace("本地翻唱", "").strip().split()
        model_name = None
        key_shift = self.default_key_shift

        for arg in args:
            if arg.lstrip('-').isdigit() and -12 <= int(arg) <= 12:
                key_shift = int(arg)
            elif len(arg) >= 1:
                model_name = arg

        await self._execute_local_cover(
            event, audio_path, model_name, key_shift=key_shift
        )


    async def _handle_cover(self, event: AstrMessageEvent):
        args = re.sub(
            r"^/?rvc\s*", "", str(event.message_str or "").strip(), flags=re.IGNORECASE
        ).strip().split()

        if not args:
            await self.selection_ui.send_card(
                event,
                "🎵 RVC 翻唱",
                "用法：/rvc <歌名> [升降调]\n"
                "示例：/rvc 优しい詩。\n\n"
                "输入歌名后，按卡片选择歌曲，再选择音色。\n"
                "发送 /翻唱帮助 查看所有入口。",
            )
            return

        key_shift, song_name = 0, " ".join(args)
        if args and args[-1].lstrip("-").isdigit():
            try:
                val = int(args[-1])
                if -12 <= val <= 12:
                    key_shift = val
                    song_name = " ".join(args[:-1]) if len(args) > 1 else ""
            except ValueError:
                pass

        if not song_name:
            yield event.plain_result("请输入歌名！")
            return

        # 0.7.0：按 source_order 搜歌，哪个平台先有结果就用哪个（默认网易云）
        candidates, search_reason = await self.song_router.search_candidates(
            song_name, limit=10
        )
        if not candidates:
            logger.info(f"所有音源搜歌都没结果: {song_name}（{search_reason}）")
            yield event.plain_result("没能找到这首歌~")
            return
        # 菜单里也不展示纯伴奏版本：选它翻唱只会出静音
        playable = [
            candidate
            for candidate in candidates
            if not source_router.is_accompaniment(candidate.name, candidate.alias)
        ]
        if not playable:
            logger.info(f"搜到的候选全是伴奏版: {song_name}")
            await self.outbound.say(
                event,
                "failed",
                song=song_name,
                reason="只有伴奏版，找不到有人声的版本",
            )
            return
        candidates = playable
        songs = [candidate.as_dict() for candidate in candidates]
        source_display = source_router.SOURCE_DISPLAY.get(
            candidates[0].source, "网易云音乐"
        )

        def menu_label(song: dict) -> str:
            label = f"{song['name']} - {song['artists']}"
            if song.get("alias"):
                label += f"（{song['alias']}）"
            return label

        try:
            selected_song_index, event = await self.selection_ui.choose(
                event,
                f"🎵 RVC · 选择歌曲\n来源：{source_display}",
                [menu_label(song) for song in songs],
                self.timeout,
            )
            if selected_song_index is None:
                await event.send(event.plain_result("已取消选歌。"))
                return
            selected_song = songs[selected_song_index]
            display_str, keys = self.get_models_display_list()
            if not keys:
                yield event.plain_result(
                    "当前没有可用的 RVC 模型，请先使用 /刷新rvc模型。"
                )
                return
            selected_model_index, event = await self.selection_ui.choose(
                event,
                f"🎙️ RVC · 选择音色\n已选歌曲：{selected_song['name']}\n歌手：{selected_song['artists']}",
                [line.split(". ", 1)[1] for line in display_str.splitlines()],
                self.timeout,
            )
        except TimeoutError:
            yield event.plain_result("选择超时，操作已取消。")
            return
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        if selected_model_index is None:
            await event.send(event.plain_result("已取消音色选择。"))
            return

        selected_model = keys[selected_model_index]

        await self.selection_ui.send_card(
            event,
            "🎙️ 开始翻唱",
            f"歌曲：{selected_song['name']}\n歌手：{selected_song['artists']}\n"
            f"音色：{selected_model}\n来源：{source_display}\n"
            f"升降调：{key_shift}\n\n"
            "已开始生成，完成后会发送音频。",
        )
        await self._send_song(
            event=event,
            song=selected_song,
            model_name=selected_model,
            key_shift=key_shift,
        )


    async def initialize(self):
        """Bind QQ callback handlers to platform clients already loaded."""
        self.selection_ui.bind_platforms(self.context.platform_manager.platform_insts)
        # 启动时后台拉一次模型列表，把设置页下拉的选项同步成最新的
        try:
            asyncio.create_task(self._startup_refresh_models())
        except Exception as error:
            logger.debug(f"启动同步模型列表失败：{error}")

    @filter.on_platform_loaded(priority=sys.maxsize)
    async def on_qq_platform_loaded(self):
        """Bind callbacks before newly loaded QQ clients connect."""
        await self.initialize()

    @filter.command("翻唱选择")
    async def expired_selection(
        self, event: AstrMessageEvent, token: str = "", choice: str = ""
    ):
        """Explain command buttons that have no active selection waiter.

        Args:
            event: Message containing a stale or unauthorized button command.
            token: Menu identifier retained by the old button.
            choice: Selection action retained by the old button.
        """
        yield event.plain_result(
            "这个选择菜单已过期，或不属于你。请重新发送点歌命令，再使用新菜单。"
        )

    async def _send_song(self, event: AstrMessageEvent, song: dict, model_name: str, key_shift: Optional[int]):
        result_path = None
        temp_audio_file = None
        song_name = song.get("name", "翻唱")
        if not self._register_active_task(event, song_name, model_name):
            await event.send(event.plain_result("⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)
        try:
            await self._send_progress_notice(event, step="正在取歌", song=song_name)
            # 统一取歌入口：卡片/命令选中的歌曲（各平台 id、甚至本地文件）
            # 都在这里变成"中间层能直接吃的本地音频"，绝不把 id 原样传下去。
            song_input, prepare_reason, _prepare_trial = await self._prepare_song_audio(
                event, song
            )
            if not song_input:
                logger.error(f"❌ 取歌失败（song={song_name}）: {prepare_reason}")
                # 只把事实交给模型：所有音源都只剩伴奏版时，让它说「只有伴奏版」
                reason = (
                    "只有伴奏版，找不到有人声的版本"
                    if prepare_reason == source_router.REASON_ONLY_ACCOMPANIMENT
                    else f"这首拿不到音源（{prepare_reason}）"
                )
                await self.outbound.say(
                    event,
                    "failed",
                    song=song_name,
                    model=model_name,
                    reason=reason,
                )
                return
            if self._is_temp_audio(song_input):
                # 插件自己下的临时文件交给 finally 清理；用户给的本地文件不能删
                temp_audio_file = song_input
            await self._send_progress_notice(
                event,
                step=(
                    f"音源准备好了，正在提交中间层"
                    f"（{os.path.getsize(song_input) / 1024 / 1024:.1f} MiB）"
                ),
                song=song_name,
            )

            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=song_input,
                model_name=model_name,
                key_shift=key_shift,
            )

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=song_name, cache_hit=cache_hit)
            else:
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason="生成失败，后端没有返回有效的音频文件")
        except Exception as e:
            logger.error(traceback.format_exc())
            if "Timeout" in str(e):
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason=f"生成超时，后端 {self.inference_timeout} 秒内没做完")
            else:
                await self.outbound.say(event, "failed", song=song_name, model=model_name, reason=str(e))
        finally:
            self._finish_active_task(event)
            if self.enable_send_file:
                await asyncio.sleep(3)
            if result_path and os.path.isfile(result_path):
                try: os.remove(result_path)
                except OSError as e: logger.debug(f"删除临时文件失败（文件可能仍被占用）: {e}")
            if temp_audio_file and os.path.isfile(temp_audio_file):
                try: os.remove(temp_audio_file)
                except OSError as e: logger.error(f"删除临时音频文件失败: {e}")


    def _guess_local_audio_language(self, text: str, file_name: str) -> str:
        """从消息文字和文件名猜语言，用于本地音频选音色。

        只做保守判断：明确说了「日语/日文」或文件名里成片假名 → ja；说「中文」→ zh；
        说「英文」或文件名几乎全是拉丁字母 → en；其余返回空串，交给默认音色。
        """
        blob = f"{text or ''} {file_name or ''}"
        lowered = blob.lower()
        if any(word in lowered for word in ("日语", "日文", "日語", "japanese", "jpop")):
            return "ja"
        if any(word in lowered for word in ("中文", "国语", "华语", "chinese")):
            return "zh"
        if any(word in lowered for word in ("英文", "英语", "english")):
            return "en"
        kana = len(re.findall(r"[\u3040-\u30ff]", blob))
        han = len(re.findall(r"[\u4e00-\u9fff]", blob))
        if kana >= 2:
            return "ja"
        if han >= 2 and kana == 0:
            return "zh"
        return ""

    async def _execute_local_cover(self, event: AstrMessageEvent, audio_path: str,
                                    model_name: Optional[str] = None,
                                    model_index: Optional[int] = None,
                                    key_shift: Optional[int] = 0):
        """本地音频翻唱核心执行逻辑（只用 RVC）"""
        if not os.path.exists(audio_path):
            await event.send(event.plain_result("❌ 音频文件不存在或已过期，请重新发送音频。"))
            return

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            await event.send(event.plain_result(f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"))
            return

        if not self.enable_rvc:
            await event.send(event.plain_result("❌ RVC 功能已在配置中禁用。"))
            return

        models_keywords = self.rvc_models_keywords
        if not models_keywords:
            await event.send(event.plain_result(
                "❌ 当前没有可用的 RVC 模型，请先使用 /刷新rvc模型。"
            ))
            return

        # 调用方没点名音色时：先按语言挑，再回落配置里的默认音色
        if not model_name and model_index is None and getattr(self, "language_model_enable", True):
            guessed = self._guess_local_audio_language(
                getattr(event, "message_str", "") or "",
                os.path.basename(audio_path),
            )
            if guessed:
                language_index = self._language_model_index(guessed)
                if language_index:
                    model_index = language_index
                    logger.info("本地音频按语言选音色: lang=%s -> 第%s个模型", guessed, language_index)
        # 配置下拉选的默认 RVC 音色：调用方没给模型名也没给序号时优先用它
        if not model_name and model_index is None:
            model_index = self._configured_rvc_model_index() or 1

        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                model_index = matched
            else:
                await event.send(event.plain_result(
                    f"❌ 未找到匹配的 RVC 模型：'{model_name}'"
                ))
                return

        if not (1 <= model_index <= len(models_keywords)):
            await event.send(event.plain_result(f"❌ 模型序号无效，当前共有 {len(models_keywords)} 个模型。"))
            return

        if key_shift is not None and not (-12 <= key_shift <= 12):
            await event.send(event.plain_result("❌ 音调调整值无效，请在 -12 到 12 之间选择。"))
            return

        _, keys = self.get_models_display_list()
        selected_model = keys[model_index - 1]
        models_info = self.get_models_detailed_list()
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break

        song_display_name = os.path.splitext(os.path.basename(audio_path))[0]
        if song_display_name.startswith("qq_") or song_display_name.startswith("dl_"):
            song_display_name = "本地音频"

        if not self._register_active_task(event, song_display_name, model_display):
            await event.send(event.plain_result("⏳ 当前会话已有翻唱任务，请等待完成或先取消任务。"))
            return
        self._bind_active_asyncio_task(event)

        result_path = None
        try:
            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=audio_path,
                model_name=selected_model,
                key_shift=key_shift,
            )

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=song_display_name, cache_hit=cache_hit)

                result_msg = f"✅ 已用 RVC 音色【{selected_model}】唱完这段音频。"
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！{result_msg}"
                if self.enable_config_report:
                    result_msg += f"\n\n📊 本次配置：模型={model_display}, 调音={int(key_shift or 0):+d}"
                await self.selection_ui.send_card(event, "✅ 翻唱完成", result_msg)
                await self._notify_llm_with_context(event, song_display_name, "cache_hit" if cache_hit else "success")
            else:
                await event.send(event.plain_result("❌ 处理失败，后端未返回有效文件路径。"))
                await self._notify_llm_with_context(event, song_display_name, "failed", result_detail="后端未返回文件路径")

        except Exception as e:
            logger.error(traceback.format_exc())
            error_msg = f"处理时发生错误: {e}"
            if "Timeout" in str(e):
                error_msg = f"⏳ 处理超时！后端在 {self.inference_timeout} 秒内没有完成任务。"
            await event.send(event.plain_result(error_msg))
            await self._notify_llm_with_context(event, song_display_name, "failed", result_detail=str(e))
        finally:
            self._finish_active_task(event)
            temp_dir = _temp_audio_dir()
            if audio_path.startswith(temp_dir) and os.path.isfile(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            if result_path and os.path.isfile(result_path):
                try:
                    if self.enable_send_file:
                        await asyncio.sleep(3)
                    os.remove(result_path)
                except OSError:
                    pass


    # ==================== 基础LLM工具（原有功能） ====================
    
    @filter.llm_tool(name="search_music")
    async def search_music(self, event: AstrMessageEvent, keyword: str, limit: int = 5) -> str:
        '''按插件音源顺序搜歌，返回候选（歌名/歌手/来源）。
只用于用户要搜歌或确认版本；单纯想听原曲时按点歌流程走，别用翻唱工具。

Args:
    keyword(string): 搜索词，歌名或「歌名 歌手」。
    limit(number): 返回条数，默认 5，最多 10。'''
        try:
            limit = min(max(limit, 1), 10)
            candidates, reason = await asyncio.wait_for(
                self.song_router.search_candidates(keyword, limit=limit),
                timeout=self.music_api_timeout,
            )

            if not candidates:
                return f"没找到与 '{keyword}' 相关的歌曲（{reason}）。"

            if self.enable_enhanced_context:
                result_lines = [
                    f"🎵 找到 {len(candidates)} 首相关歌曲：",
                    "",
                    "【歌曲列表】"
                ]
                for i, song in enumerate(candidates, 1):
                    alias = f"（别名：{song.alias}）" if song.alias else ""
                    result_lines.append(
                        f"  {i}. 《{song.name}》 - {song.artists}{alias}"
                        f"\n     来源: {source_router.SOURCE_DISPLAY.get(song.source, song.source)}"
                    )

                result_lines.extend([
                    "",
                    "💡 快捷指令提示：",
                    "- 直接说 '翻唱第X首' 或 '用第Y个模型翻唱第X首'",
                    "- 我会自动帮您完成后续步骤！",
                    "",
                    f"📝 当前状态：已搜索 '{keyword}'，共 {len(candidates)} 首歌曲可选"
                ])
            else:
                result_lines = [f"🎵 找到 {len(candidates)} 首相关歌曲："]
                for i, song in enumerate(candidates, 1):
                    alias = f"（别名：{song.alias}）" if song.alias else ""
                    result_lines.append(
                        f"{i}. 《{song.name}》 - {song.artists}{alias}"
                    )
                result_lines.append("\n请告诉用户搜索结果，并询问他们想要翻唱哪首歌（输入序号）。")

            return "\n".join(result_lines)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"搜索歌曲时出错: {e}"


    @filter.llm_tool(name="rvc_cover")
    async def rvc_cover(self, event: AstrMessageEvent, song_name: str, artist_name: Optional[str] = None, model_index: Optional[int] = None, model_name: Optional[str] = None, key_shift: int = 0) -> str:
        '''用 RVC 音色翻唱一首歌。只在用户明确要「唱 / 翻唱 / 变声」时调用；
用户说「点歌 / 放首歌 / 听」是想放原曲，不要用本工具。

Args:
    song_name(string): 歌名原样填，用户自己说的版本词保留在歌名里。
    artist_name(string, optional): 知道原唱就填原唱名（如「晴天」→ 周杰伦），
        不确定就留空；不要填翻唱歌手，不要猜。插件只下载原唱。
    model_index(number, optional): 音色序号，从 1 开始。
    model_name(string, optional): 音色名称或别名，优先于序号。
    key_shift(number): 默认 0，一般不用传。

Returns: 任务描述；异步，立刻返回，完成后自动发音频。'''
        rejected = self._reject_music_request(event, "rvc_cover")
        if rejected:
            return rejected
        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。如需使用请在插件配置中设置 enable_rvc 为 true。"
        # 0.14.1：只有用户/模型明确给了音色才算显式；没给就让语言判定挑
        explicit_model = bool(model_name) or model_index is not None
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                model_index = matched
            else:
                return f"❌ 未找到匹配的RVC模型: '{model_name}'\n\n💡 提示：使用 get_available_models 查看可用模型"
        elif model_index is None:
            model_index = self.fallback_model_index

        models_info = self.get_models_detailed_list()
        model_display = ""
        for m in models_info:
            if m["index"] == model_index:
                model_display = m["display"]
                break

        if not self._register_active_task(event, song_name, model_display):
            position = self._queue_cover_request(
                event, song_name, model_index, key_shift,
                model_display, artist_name=artist_name or "",
                model_explicit=explicit_model,
            )
            if position is None:
                return "⏳ 当前任务已经排了三首，等这几首做完再点吧。"
            return f"排在第 {position} 个，前面那首做完我就接着唱《{song_name}》。"
        logger.info(
            f"[MatsukoCover任务Debug] 接受 rvc_cover 请求 | song={song_name} | "
            f"artist={artist_name or '-'} | model={model_display}"
        )
        task = self._create_tracked_task(self._smart_cover_async(
            event, song_name, model_index, key_shift, model_display,
            artist_name=artist_name,
            model_explicit=explicit_model,
        ))
        self._bind_active_asyncio_task(event, task)
        # 只回一句中性短句给 LLM，具体怎么说由人格模型组织；不再拼符号和参数
        return f"好，我去弄这首《{song_name}》，好了直接发你。"


    async def _do_cover(self, event: AstrMessageEvent, song_name: str, model_index: int, key_shift: Optional[int], artist_name: str = "", model_explicit: bool = False) -> str:
        """统一的翻唱执行逻辑（只用 RVC）。

        ``song_name`` 原样整串交给搜歌入口；``artist_name`` 非空才当过滤条件，
        由 :func:`source_router.pick_best_song` 统一处理（不再拼搜索串）。

        ``model_explicit`` 表示这次模型是用户自己点的（或工具里明确传了 model_name/序号），
        为 True 时不做语言判定，尊重用户选择。
        """
        flow_id = uuid.uuid4().hex[:8]
        result_path = None
        temp_audio_file = None
        try:
            logger.info(
                f"[CoverFlow:{flow_id}] stage=start | song={song_name} | "
                f"model_index={model_index}"
            )
            if not self.enable_rvc:
                return "❌ RVC 功能已在配置中禁用。"
            if key_shift is None:
                key_shift = self.default_key_shift

            models_keywords = self.rvc_models_keywords

            if not models_keywords:
                return "❌ 当前没有可用的 RVC 模型，请先使用 /刷新rvc模型 更新列表。"

            if not (1 <= model_index <= len(models_keywords)):
                return f"❌ 模型序号无效，当前共有 {len(models_keywords)} 个模型可选，请输入 1 到 {len(models_keywords)} 之间的数字。"

            if not (-12 <= key_shift <= 12):
                return "❌ 音调调整值无效，请在 -12 到 12 之间选择。"

            # === 搜歌：0.8.0 起 song_name 整串原样搜，artist 只在明确给出时当过滤条件 ===
            logger.info(
                f"[CoverFlow:{flow_id}] stage=music_search | "
                f"order={source_router.describe_order(self.source_order)} | "
                f"timeout={self.music_api_timeout}s | artist={artist_name or '-'}"
            )
            selected_song, search_reason, _search_attempts = (
                await self.song_router.find_song(song_name, artist_name or "")
            )

            logger.info(
                f"[CoverFlow:{flow_id}] stage=music_search_done | "
                f"hit={bool(selected_song)} | reason={search_reason}"
            )

            if not selected_song:
                logger.info(
                    f"[CoverFlow:{flow_id}] stage=no_confident_match | "
                    f"song={song_name} | reason={search_reason}"
                )
                if search_reason == source_router.REASON_ONLY_ACCOMPANIMENT:
                    return (
                        f"❌ 《{song_name}》只找到伴奏版（没有人声），翻唱不了，"
                        "换一个有人声的版本再试。"
                    )
                return (
                    f"❌ 所有音源都没找到能确认是《{song_name}》的版本"
                    f"（{search_reason}），换个更全的歌名或歌手试试。"
                )

            # === 0.13.0 按歌曲语言挑音色：用户显式指定模型时整段跳过 ===
            lang_hint = ""
            if not model_explicit:
                lang, evidence, language_index, configured_name = await self._resolve_language_model(
                    selected_song, flow_id
                )
                if language_index:
                    model_index = language_index
                elif lang:
                    spoken = language_detect.lang_name(lang)
                    if configured_name:
                        lang_hint = f"没找到「{spoken}」配的音色 {configured_name}，这次先用默认音色。"
                    else:
                        lang_hint = f"这首是{spoken}歌，但没配{spoken}音色，这次先用默认的。"
            if lang_hint:
                try:
                    await event.send(event.plain_result(lang_hint))
                except Exception:
                    logger.debug("语言回落提示发送失败，继续翻唱")

            _, keys = self.get_models_display_list()
            selected_model = keys[model_index - 1]

            # === 自动升降调 ===
            models_info = self.get_models_detailed_list()
            model_display = ""
            for m in models_info:
                if m["index"] == model_index:
                    model_display = m["display"]
                    break

            original_key_shift = key_shift
            artist_from_song = selected_song.get("artists", "")
            artist_gender = None
            model_gender = None
            detection_attempted = False
            if self.enable_auto_key_shift and key_shift == 0:
                detection_attempted = True
                logger.info(f"[CoverFlow:{flow_id}] stage=gender_detection")
                artist_gender, model_gender = await self._detect_gender_pair(
                    artist_from_song, model_display, event
                )
            key_shift = await self._calc_auto_key_shift(
                artist_from_song,
                model_display,
                key_shift,
                artist_gender,
                model_gender,
                event,
                detection_attempted=detection_attempted,
            )
            if key_shift != original_key_shift and self.enable_auto_key_shift:
                logger.info(f"自动升降调：{artist_from_song} → {model_display}，音调从 {original_key_shift:+d} 调整为 {key_shift:+d}")

            # === 统一取歌入口：只把本地音频交给中间层，id 一律不出插件 ===
            song_input_for_api, prepare_reason, prepare_trial = await self._prepare_song_audio(
                event, selected_song
            )
            if not song_input_for_api:
                logger.error(
                    f"[CoverFlow:{flow_id}] stage=audio_prepare_failed | "
                    f"song={selected_song.get('name', song_name)} | "
                    f"trial={prepare_trial} | reason={prepare_reason}"
                )
                return f"❌ 这首拿不到音源（{prepare_reason}）"
            if self._is_temp_audio(song_input_for_api):
                # 插件自己下的临时文件交给 finally 清理，别把整首歌留在磁盘上
                temp_audio_file = song_input_for_api
            await self._send_progress_notice(
                event,
                step=(
                    f"音源准备好了，正在提交中间层"
                    f"（{os.path.getsize(song_input_for_api) / 1024 / 1024:.1f} MiB）"
                ),
                song=selected_song.get("name", song_name),
            )

            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_connect | url={self.rvc_base_url}"
            )
            logger.info(
                f"[CoverFlow:{flow_id}] stage=backend_submit | api=/convert | "
                f"model={selected_model}"
            )
            result_path, cache_hit = await self._predict_cover(
                event=event,
                song_name_src=song_input_for_api,
                model_name=selected_model,
                key_shift=key_shift,
            )
            logger.info(f"[CoverFlow:{flow_id}] stage=backend_done")

            if result_path and os.path.exists(result_path):
                await self._send_cover_result(event, result_path, song_name=selected_song.get('name', song_name), cache_hit=cache_hit)
                song_artist = selected_song.get('artists', '未知艺人')
                if cache_hit:
                    result_msg = f"⚡ 命中缓存！已成功使用 RVC 音色【{selected_model}】发送《{selected_song['name']}》({song_artist}) 的翻唱版本！"
                else:
                    result_msg = f"已成功使用 RVC 音色【{selected_model}】生成《{selected_song['name']}》({song_artist}) 的翻唱版本！音频文件已发送。"
                if (key_shift != original_key_shift and self.enable_auto_key_shift
                        and getattr(self, "enable_config_report", False)):
                    gender_label = {"male": "男", "female": "女"}.get
                    result_msg += f"\n🎵 自动升降调：{song_artist}({gender_label(artist_gender, '?')}) → {model_display}({gender_label(model_gender, '?')})，音调 {original_key_shift:+d} → {key_shift:+d}"
            else:
                result_msg = "❌ 生成失败，后端未返回有效文件路径。"

            # === 方案E：记录偏好 ===
            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                pref["usage_count"] += 1
                pref["last_used_time"] = datetime.now().isoformat()
                pref["last_used_model"] = selected_model

                song_entry = f"{selected_song['name']} - {selected_song['artists']}"
                existing = [s for s in pref["favorite_songs"] if s.split(" - ")[0] == selected_song['name']]
                if not existing:
                    pref["favorite_songs"].append(song_entry)
                    if len(pref["favorite_songs"]) > 30:
                        pref["favorite_songs"] = pref["favorite_songs"][-30:]

                artist = selected_song['artists'].split(',')[0].strip() if ',' in selected_song['artists'] else selected_song['artists']
                artists_dict = pref.get("preferred_artists", {})
                if artist not in artists_dict:
                    artists_dict[artist] = {"count": 0, "last_time": None, "model": None}
                artists_dict[artist]["count"] += 1
                artists_dict[artist]["last_time"] = datetime.now().isoformat()
                artists_dict[artist]["model"] = selected_model
                pref["preferred_artists"] = artists_dict

                artist_model_map = pref.get("artist_model_map", {})
                if artist not in artist_model_map:
                    artist_model_map[artist] = {}
                model_key = selected_model
                if model_key not in artist_model_map[artist]:
                    artist_model_map[artist][model_key] = 0
                artist_model_map[artist][model_key] += 1
                pref["artist_model_map"] = artist_model_map

                await self._save_preferences()

            return result_msg

        except Exception as e:
            logger.error(f"[CoverFlow:{flow_id}] stage=failed\n{traceback.format_exc()}")
            if "Timeout" in str(e):
                return f"❌ 生成超时了！后端在 {self.inference_timeout} 秒内没有完成任务。"
            else:
                return f"❌ RVC 翻唱时发生错误: {e}"
        finally:
            try:
                if self.enable_send_file and result_path and os.path.isfile(result_path):
                    await asyncio.sleep(3)
                if result_path and os.path.isfile(result_path):
                    os.remove(result_path)
                if temp_audio_file and os.path.isfile(temp_audio_file):
                    os.remove(temp_audio_file)
            except OSError as cleanup_error:
                logger.debug(
                    f"删除临时文件失败（文件可能仍被占用，将在退出时清理）: {cleanup_error}"
                )


    @filter.llm_tool(name="get_available_models")
    async def get_available_models(self, event: AstrMessageEvent) -> str:
        '''列出可用的 RVC 音色模型（序号、显示名、文件名、别名）。
用户问「有哪些音色」时用；指定音色时可直接用名称，不必记序号。'''
        try:
            rvc_models = self.get_models_detailed_list()
            if not rvc_models:
                return "【RVC 模型】暂无可用模型，请使用 /刷新rvc模型"

            lines = []
            for m in rvc_models:
                alias_info = f" (别名: {m['alias']})" if m['alias'] else ""
                lines.append(f"  {m['index']}. {m['display']}【{m['name']}】{alias_info}")

            result_parts = [f"【RVC 模型】(共 {len(rvc_models)} 个)\n" + "\n".join(lines)]
            result_parts.append("\n\n💡 使用方式：")
            result_parts.append("- 用序号: model_index=2 （第2个模型）")
            result_parts.append("- 用名称: model_name='tafeim' 或 model_name='塔菲' （自动匹配）")
            result_parts.append("- 示例: '用塔菲模型翻唱《晴天》'")

            status = "RVC ✅" if self.enable_rvc else "RVC ❌(已禁用)"
            result_parts.append(f"\n🔧 引擎状态: {status}")
            note = self._models_dir_note()
            if note:
                result_parts.append(note)

            return "\n".join(result_parts)
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取模型列表时出错: {e}"


    # ==================== 方案A：智能单步翻唱 ====================
    
    @filter.llm_tool(name="smart_cover")
    async def smart_cover(self, event: AstrMessageEvent,
                         song_name: str,
                         artist_name: Optional[str] = None,
                         model_index: Optional[int] = None,
                         model_name: Optional[str] = None,
                         key_shift: Optional[int] = None) -> str:
        '''智能翻唱：搜歌、挑版本、变声一步到位，用户要「唱」时优先用这个。
用户要「听原曲 / 点歌」不要用本工具，那会变成翻唱。

Args:
    song_name(string): 歌名原样填，别拆开。
    artist_name(string, optional): 知道原唱就填，插件靠它排除同名翻唱；
        不确定留空，不要填翻唱名，不要猜。只有翻唱版本时插件会返回提示，照实转告用户。
    model_index(number, optional): 音色序号，从 1 开始。
    model_name(string, optional): 音色名称或别名，优先于序号。
    key_shift(number, optional): 一般别传，插件按原唱与音色的性别自动配；
        用户明确说「升3调」之类才传。

Returns: 任务描述；异步，立刻返回，完成后自动发音频。'''
        rejected = self._reject_music_request(event, "smart_cover")
        if rejected:
            return rejected
        if not self.enable_smart_cover:
            return "智能单步翻唱功能已在配置中禁用。请使用对应的独立翻唱工具。"

        try:
            if not self.enable_rvc:
                return "❌ RVC 功能已在配置中禁用，请先在插件配置里开启 enable_rvc。"

            # === 方案B：检查是否有进行中的翻唱任务 ===
            active_task = self._get_active_task_status(event)
            if active_task:
                elapsed = int(time.time() - active_task.get("start_time", 0))
                # 0.15.0：不再直接拒绝，排进队列等前一首做完自动接着唱
                queue_index = (
                    self._find_model_index_by_name(model_name)
                    if model_name else (model_index or self.fallback_model_index)
                )
                queue_display = ""
                for info_item in self.get_models_detailed_list():
                    if info_item["index"] == queue_index:
                        queue_display = info_item["display"]
                        break
                position = self._queue_cover_request(
                    event, song_name, queue_index,
                    key_shift if key_shift is not None else self.default_key_shift,
                    queue_display, artist_name=artist_name or "",
                    model_explicit=bool(model_name) or model_index is not None,
                )
                if position is None:
                    return (
                        f"⏳ 前面《{active_task['song_name']}》还在做（{elapsed}秒），"
                        "排队的也满了三首，等做完再点吧。"
                    )
                return (
                    f"排在《{active_task['song_name']}》后面了（第 {position} 个），"
                    f"它做完我就接着唱《{song_name}》。"
                )

            if model_name:
                matched_index = self._find_model_index_by_name(model_name)
                if matched_index:
                    actual_model_index = matched_index
                else:
                    available = await self.get_available_models(event)
                    return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用的模型：\n{available}\n\n💡 提示：可以使用 get_available_models 查看完整列表"
            else:
                actual_model_index = model_index or self.fallback_model_index

            # 0.13.0：只有用户自己点名的模型才算显式选择；偏好/默认值仍然让语言配置来挑
            model_explicit = bool(model_name) or model_index is not None

            actual_key_shift = (
                key_shift if key_shift is not None else self.default_key_shift
            )

            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)

                if not model_name and not model_index and pref.get("model_index"):
                    actual_model_index = pref["model_index"]

                if key_shift is None:
                    actual_key_shift = pref.get("default_key_shift", self.default_key_shift)

                if artist_name and not model_name and not model_index:
                    artist_model_map = pref.get("artist_model_map", {})
                    if artist_name in artist_model_map:
                        model_counts = artist_model_map[artist_name]
                        if model_counts:
                            best_model = max(model_counts, key=model_counts.get)
                            models_info_tmp = self.get_models_detailed_list()
                            for m in models_info_tmp:
                                if m["filename"] == best_model or m["display"] == best_model:
                                    actual_model_index = m["index"]
                                    break

            # 配置下拉选的默认音色：用户没显式指定模型名/序号时优先用它
            if not model_name and not model_index:
                configured_default_index = self._configured_rvc_model_index()
                if configured_default_index:
                    actual_model_index = configured_default_index

            models_info = self.get_models_detailed_list()
            actual_model_display = ""
            artist_pref_hint = ""
            for m in models_info:
                if m["index"] == actual_model_index:
                    actual_model_display = m["display"]
                    break

            if self.enable_preference_learning and artist_name and not model_name and not model_index:
                artist_model_map = pref.get("artist_model_map", {})
                if artist_name in artist_model_map:
                    model_counts = artist_model_map[artist_name]
                    best_model = max(model_counts, key=model_counts.get)
                    for m in models_info:
                        if (m["filename"] == best_model or m["display"] == best_model) and m["index"] == actual_model_index:
                            artist_pref_hint = f"，这次按老习惯挑了翻唱{artist_name}常用的音色"
                            break

            # 注册活跃任务
            if not self._register_active_task(event, song_name, actual_model_display):
                return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"

            task = self._create_tracked_task(self._smart_cover_async(
                event, song_name, actual_model_index,
                actual_key_shift, actual_model_display,
                artist_name=artist_name,
                model_explicit=model_explicit,
            ))
            self._bind_active_asyncio_task(event, task)

            # 只回一句中性短句给 LLM，具体怎么说由人格模型组织；不再拼符号和参数
            return f"好，我去弄这首《{song_name}》，好了直接发你。{artist_pref_hint}"

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"智能翻唱出错: {e}"


    async def _smart_cover_async(self, event: AstrMessageEvent, song_name: str,
                                 model_index: int, key_shift: Optional[int],
                                 model_display: str,
                                 artist_name: str = "",
                                 model_explicit: bool = False):
        """smart_cover 异步执行函数。

        ``song_name`` 是用户说的歌名整串（版本名留在里面），``artist_name`` 只在
        用户明确说出歌手时才有值，两者分开传给取歌入口，不再拼成一个搜索串。
        """
        try:
            logger.info(
                f"[MatsukoCover任务Debug] 后台翻唱流程已进入 | song={song_name} | "
                f"artist={artist_name or '-'} | model={model_display}"
            )
            result = await self._do_cover(
                event, song_name, model_index, key_shift,
                artist_name=artist_name,
                model_explicit=model_explicit,
            ) or ""

            # 失败判断只认明确标记：❌ 开头，或 FAIL: 开头（保留 ❌ 兼容 _do_cover 现有返回点）。
            # 不用关键词猜（"失败"/"错误"/"超时"…）：正常文案里带"失败"两个字就会被误判成任务失败。
            # _do_cover 的返回值协议本次不动。
            is_failed = result.startswith("❌") or result.startswith("FAIL:")

            # === 失败：技术细节只进日志，用户只听到一句话 ===
            if is_failed:
                logger.warning(
                    f"[MatsukoCover任务Debug] 翻唱失败 | song={song_name} | "
                    f"model={model_display} | detail={result}"
                )
                await self.outbound.say(
                    event, "failed", song=song_name, model=model_display, reason=result
                )
            elif self.enable_config_report:
                # 成功时根据配置决定是否发送配置报告
                report_shift = int(key_shift or 0)
                result += f"\n\n📊 本次配置：模型={model_display}, 调音={report_shift:+d}"
                await event.send(event.plain_result(result))

            # === LLM 上下文通知（方案A+B整合）===
            # 失败已经由统一出口说过一句话，这里不再重复说话，免得一次任务两条回复
            cache_hit = "命中缓存" in result or "cache hit" in result.lower()
            if not is_failed:
                if cache_hit:
                    await self._notify_llm_with_context(event, song_name, "cache_hit")
                else:
                    await self._notify_llm_with_context(event, song_name, "success")

        except Exception as e:
            logger.error(f"smart_cover 异步执行失败: {traceback.format_exc()}")
            try:
                # 异常也走统一出口：只说一句人话，异常栈留在日志里
                await self.outbound.say(
                    event, "failed", song=song_name, model=model_display, reason=str(e)
                )
            except Exception:
                pass

        finally:
            # 方案B：无论成功失败，都清理活跃任务
            self._finish_active_task(event)


    # ==================== 方案C：确认机制 ====================
    
    @filter.llm_tool(name="confirm_selection")
    async def confirm_selection(self, event: AstrMessageEvent,
                               selection_type: str,
                               selection_data: str,
                               action: str = "confirm") -> str:
        '''确认或修改上一轮的翻唱选择，用于「就这个 / 换一个 / 取消」这类回应。

Args:
    selection_type(string): song / model / config。
    selection_data(string): 当前选择或要改成的值。
    action(string): confirm / change / cancel / info，默认 confirm。'''
        if not self.enable_confirm_mechanism:
            return "确认机制已在配置中禁用。"
        
        try:
            responses = {
                "confirm": {
                    "song": f"✅ 已确认选择：{selection_data}\n接下来将执行翻唱操作。",
                    "model": f"✅ 已确认使用：{selection_data}",
                    "config": f"✅ 配置已确认：{selection_data}\n准备开始翻唱..."
                },
                "change": {
                    "song": f"🔄 需要更换歌曲选择。\n当前选择：{selection_data}\n请重新搜索或指定新的歌曲。",
                    "model": f"🔄 需要更换模型。\n当前选择：{selection_data}\n可用模型列表：\n{(await self.get_available_models(event))}",
                    "config": f"🔄 需要修改配置。\n当前配置：{selection_data}\n请告诉我新的参数值。"
                },
                "cancel": {
                    "song": "❌ 已取消歌曲选择。",
                    "model": "❌ 已取消模型选择。",
                    "config": "❌ 已取消配置，操作终止。"
                },
                "info": {
                    "song": f"📋 歌曲详情：{selection_data}",
                    "model": f"📋 模型详情：{selection_data}",
                    "config": f"⚙️ 当前配置详情：{selection_data}"
                }
            }
            
            action_lower = action.lower() if action else "confirm"
            type_lower = selection_type.lower() if selection_type else "song"
            
            if action_lower not in responses:
                return f"未知操作: {action}。支持的选项: confirm, change, cancel, info"
            
            if type_lower not in responses[action_lower]:
                return f"未知选择类型: {selection_type}。支持的类型: song, model, config"
            
            result = responses[action_lower][type_lower]
            
            if action_lower == "confirm" and type_lower == "song":
                result += "\n\n💡 提示：您也可以直接说'就用这个翻唱'，我会自动继续！"
            
            return result
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"确认操作出错: {e}"

    # ==================== 方案D：批量翻唱 ====================
    
    @filter.llm_tool(name="batch_cover")
    async def batch_cover(self, event: AstrMessageEvent,
                         songs: List[str],
                         model_index: Optional[int] = None,
                         key_shift: Optional[int] = None) -> str:
        '''批量翻唱，一次交多首，异步逐首处理，每完成一首就发音频。
只在用户明确要一次唱多首时用。

Args:
    songs(list[string]): 歌名列表，原样填。
    model_index(number, optional): 统一使用的音色序号。
    model_name(string, optional): 统一使用的音色名称，优先于序号。
    key_shift(number, optional): 一般别传，插件自动配。

只下载原唱；某些歌只有翻唱会在结果里提示。'''
        rejected = self._reject_music_request(event, "batch_cover")
        if rejected:
            return rejected
        if not self.enable_batch_cover:
            return "批量翻唱功能已在配置中禁用，请逐个使用 smart_cover。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用，请先在插件配置里开启 enable_rvc。"

        # === 方案B：检查是否有进行中的翻唱任务 ===
        active_task = self._get_active_task_status(event)
        if active_task:
            elapsed = int(time.time() - active_task.get("start_time", 0))
            return (
                f"⏳ 您已经有翻唱任务在进行中啦nya～\n"
                f"歌曲：《{active_task['song_name']}》\n"
                f"状态：{active_task['status']}\n"
                f"已进行：{elapsed}秒\n"
                f"请等当前任务完成后再发起批量翻唱喵！🐾"
            )

        try:
            if not songs or len(songs) == 0:
                return "歌曲列表为空，请提供至少一首歌名。"

            if len(songs) > self.max_batch_size:
                return f"批量翻唱数量超过限制（最大{self.max_batch_size}首）。当前提供了{len(songs)}首，请分批处理。"

            # === 参数解析 ===
            actual_model_index = model_index or self.fallback_model_index
            actual_key_shift = (
                key_shift if key_shift is not None else self.default_key_shift
            )

            if self.enable_preference_learning:
                user_id = self._get_user_id(event)
                pref = self._get_user_pref(user_id)
                if not model_index:
                    actual_model_index = pref.get("model_index", actual_model_index)
                if key_shift is None:
                    actual_key_shift = pref.get("default_key_shift", actual_key_shift)

            # 配置下拉选的默认音色：用户没显式指定序号时优先用它
            if not model_index:
                configured_default_index = self._configured_rvc_model_index()
                if configured_default_index:
                    actual_model_index = configured_default_index

            # 注册活跃任务
            batch_song_names = "、".join(songs[:3])
            if len(songs) > 3:
                batch_song_names += f" 等{len(songs)}首"
            if not self._register_active_task(event, f"[批量]{batch_song_names}"):
                return "⏳ 当前已有翻唱任务，请等待完成或先调用 cancel_cover_task。"

            # === 创建后台任务（异步执行）===
            task = self._create_tracked_task(
                self._execute_batch_cover_async(
                    event=event,
                    songs=songs,
                    model_index=actual_model_index,
                    key_shift=actual_key_shift,
                    model_explicit=model_index is not None,
                )
            )
            self._bind_active_asyncio_task(event, task)

            # === 立即返回确认消息（不会被超时取消）===
            report_shift = int(actual_key_shift or 0)
            confirm_msg = (
                f"📦 已接收批量翻唱任务！共 {len(songs)} 首歌曲\n"
                + (f"⚙️ 配置：RVC / 第{actual_model_index}个模型 / 音调{report_shift:+d}\n"
                   if getattr(self, "enable_config_report", False) else "")
                + f"⏳ 正在后台处理中，请稍候...\n"
                + "💡 每完成一首会自动发送音频，最后发送汇总报告"
            )

            return confirm_msg

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"批量翻唱启动失败: {e}"


    async def _execute_batch_cover_async(self, event: AstrMessageEvent,
                                        songs: List[str],
                                        model_index: int,
                                        key_shift: Optional[int],
                                        model_explicit: bool = False):
        """异步执行批量翻唱（后台任务）。

        ``model_explicit`` 表示模型是用户自己点的：为真时整批都用这个模型，
        不让语言判定按每首歌换音色（否则「用三月七唱这三首」会被覆盖）。
        """
        try:
            results = []
            success_count = 0
            fail_count = 0

            for idx, song_name in enumerate(songs, 1):
                try:
                    # 进度提示走统一出口（默认静音，由 notify_progress 控制）
                    await self.outbound.say(
                        event, "progress", song=song_name, count=idx, total=len(songs)
                    )

                    # 执行翻唱
                    result = await self._do_cover(
                        event, song_name, model_index, key_shift,
                        model_explicit=model_explicit,
                    ) or ""
                    # 与 _smart_cover_async 保持一致：只认明确的失败标记，不用关键词猜
                    is_failed = result.startswith("❌") or result.startswith("FAIL:")
                    if is_failed:
                        # 报告只留一句人话，技术细节（含中间层原始报错）只写日志
                        logger.warning(
                            f"批量翻唱第{idx}首失败: {song_name} | detail={result}"
                        )
                        results.append(
                            f"❌ [{idx}/{len(songs)}] 《{song_name}》："
                            f"{self._short_failure_reason(result)}"
                        )
                        fail_count += 1
                    else:
                        results.append(f"✅ [{idx}/{len(songs)}] 《{song_name}》：成功")
                        success_count += 1

                    # 歌间延迟避免过快请求
                    if idx < len(songs):
                        await asyncio.sleep(2)

                except asyncio.CancelledError:
                    results.append(f"❌ [{idx}/{len(songs)}] 《{song_name}》：被取消")
                    fail_count += 1
                    logger.warning(f"批量翻唱第{idx}首被取消")
                except Exception as e:
                    logger.error(f"批量翻唱第{idx}首出错: {e}", exc_info=True)
                    results.append(
                        f"❌ [{idx}/{len(songs)}] 《{song_name}》：这首没做成"
                    )
                    fail_count += 1

            # === 生成并发送汇总报告 ===
            summary_lines = [
                "",
                "=" * 50,
                "📊 批量翻唱任务完成报告",
                "=" * 50,
                "",
                f"总任务数: {len(songs)} 首",
                f"✅ 成功: {success_count} 首",
                f"❌ 失败: {fail_count} 首",
                "",
            ]
            # 0.15.2：「翻唱完成后发送配置信息报告」关掉时，不附模型/音调
            if getattr(self, "enable_config_report", False):
                summary_lines.extend([
                    "使用的配置:",
                    "  引擎: RVC",
                    f"  模型: 第{model_index}个",
                    f"  音调: {int(key_shift or 0):+d}",
                    "",
                ])
            summary_lines.extend([
                "-" * 50,
                "详细结果:",
            ])

            summary_lines.extend(results)

            summary_lines.extend([
                "",
                "=" * 50,
                f"完成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 50
            ])

            final_report = "\n".join(summary_lines)

            # 0.16.1：开关关掉时整份报告都不发，只写日志（每首歌的音频和完成提示照旧）
            if getattr(self, "enable_config_report", False):
                try:
                    await event.send(event.plain_result(final_report))
                except Exception as e:
                    logger.error(f"发送批量翻唱报告失败: {e}")
            else:
                logger.info(
                    "批量翻唱完成（配置报告开关关闭，未发送汇总）：成功 %d，失败 %d",
                    success_count, fail_count,
                )

            # === LLM 成功通知机制 ===
            if getattr(self, "enable_llm_success_notify", True) and success_count > 0:
                try:
                    provider = self.context.get_using_provider(event.unified_msg_origin)
                    if provider:
                        prompt = f"系统通知：刚才用户要求的批量翻唱任务已经处理完成！共成功处理了 {success_count} 首歌曲。请用你当前的人设直接告诉用户这个好消息（不要包含系统通知字样）。"
                        llm_resp = await provider.text_chat(prompt=prompt)
                        if llm_resp and llm_resp.completion_text:
                            await event.send(event.plain_result(llm_resp.completion_text))
                except Exception as llm_e:
                    logger.error(f"通知 LLM 失败: {llm_e}")

        except Exception as e:
            logger.error(traceback.format_exc())
            # 技术细节只进日志，报告里只说一句人话
            error_report = "❌ 批量翻唱没跑完，具体原因已经记到日志里了"
            try:
                await event.send(event.plain_result(error_report))
            except Exception:
                pass
        finally:
            # 方案B：批量任务结束后清理活跃任务
            self._finish_active_task(event)


    # ==================== 方案E：偏好学习与推荐系统 ====================
    
    @filter.llm_tool(name="save_preference")
    async def save_preference(self, event: AstrMessageEvent,
                             preference_type: str,
                             value: str) -> str:
        '''保存用户默认偏好，下次翻唱自动套用。

Args:
    preference_type(string): model / key_shift 等。
    value(string): 偏好值。'''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。"

        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)

            valid_types = {
                "model_index": {"type": int, "min": 1},
                "key_shift": {"type": int, "min": -12, "max": 12}
            }

            if preference_type not in valid_types:
                return f"无效的偏好类型: {preference_type}\n支持的类型: {', '.join(valid_types.keys())}"

            type_info = valid_types[preference_type]
            target_type = type_info["type"]

            try:
                converted_value = target_type(value)
            except (ValueError, TypeError):
                return f"无法将值 '{value}' 转换为 {target_type.__name__} 类型"

            if "valid_values" in type_info and converted_value not in type_info["valid_values"]:
                return f"无效的值: {converted_value}，可选值: {type_info['valid_values']}"

            if "min" in type_info and converted_value < type_info["min"]:
                return f"值太小: {converted_value}，最小值为 {type_info['min']}"

            if "max" in type_info and converted_value > type_info["max"]:
                return f"值太大: {converted_value}，最大值为 {type_info['max']}"

            pref[f"default_{preference_type}"] = converted_value
            await self._save_preferences()

            type_display = {
                "model_index": f"默认模型 → 第{converted_value}个",
                "key_shift": f"默认音调 → {converted_value:+d}"
            }

            return f"✅ 偏好已保存！\n{type_display.get(preference_type, preference_type)}\n\n下次翻唱时会自动使用此设置。如需修改，随时告诉我！"

        except Exception as e:
            logger.error(traceback.format_exc())
            return f"保存偏好时出错: {e}"


    @filter.llm_tool(name="get_recommendation")
    async def get_recommendation(self, event: AstrMessageEvent,
                                song_name: Optional[str] = None) -> str:
        '''按用户历史给出音色与参数推荐。

Args:
    song_name(string, optional): 想唱的歌；不填给通用建议。'''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法提供个性化推荐。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            recommendation = []
            recommendation.append("🤖 基于您的使用历史，我为您推荐以下配置：\n")
            
            # === 基本信息 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "无")
            
            recommendation.append(f"📊 您的使用统计：")
            recommendation.append(f"   - 总翻唱次数: {usage_count}")
            recommendation.append(f"   - 最后使用时间: {last_time[:16] if last_time != '无' else last_time}")
            
            if usage_count == 0:
                recommendation.append(f"\n💡 您还没有使用过翻唱功能，将使用系统默认配置：")
                recommendation.append(f"   - 模型: 第{self.fallback_model_index}个")
                recommendation.append(f"   - 音调: {self.default_key_shift:+d}")
                return "\n".join(recommendation)

            # === 推荐配置 ===
            rec_model_index = pref.get("model_index", self.fallback_model_index)
            rec_key_shift = pref.get("default_key_shift", self.default_key_shift)
            last_model = pref.get("last_used_model", "未知")
            
            recommendation.append(f"\n⭐ 推荐配置：")
            recommendation.append(f"   🎤 模型选择: 第{rec_model_index}个 ({last_model})")
            recommendation.append(f"   🎼 音调调整: {rec_key_shift:+d}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                recent_favorites = favorite_songs[-5:] if len(favorite_songs) > 5 else favorite_songs
                recommendation.append(f"\n❤️ 您最近常听的歌曲：")
                for song in recent_favorites:
                    recommendation.append(f"   - 《{song}》")
            
            # === 偏好歌手（按频率排序）===
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                recommendation.append(f"\n🎤 您偏爱的歌手（按频率排序）：")
                for artist, info in sorted_artists[:5]:
                    model_info = f", 常用模型: {info.get('model', '未知')}" if info.get('model') else ""
                    recommendation.append(f"   - {artist} ({info.get('count', 0)}次{model_info})")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                recommendation.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    recommendation.append(f"   - {artist} → {best} ({models[best]}次)")
            
            # === 结合当前歌曲的建议 ===
            if song_name:
                recommendation.append(f"\n🎯 针对《{song_name}》的建议：")
                
                matched_artist = None
                for artist in preferred_artists:
                    if artist and artist in song_name:
                        matched_artist = artist
                        break
                
                if matched_artist:
                    recommendation.append(f"   ✨ 检测到这是您偏爱的歌手 {matched_artist} 的作品！")
                    if matched_artist in artist_model_map:
                        best_model = max(artist_model_map[matched_artist], key=artist_model_map[matched_artist].get)
                        recommendation.append(f"   💡 建议使用模型 {best_model}（您翻唱此歌手时最常用）")
                    else:
                        recommendation.append(f"   💡 建议使用您常用的配置进行翻唱。")
                
                song_names = [s.split(" - ")[0] for s in favorite_songs]
                if song_name in song_names:
                    recommendation.append(f"   🔄 这首歌您之前翻唱过！")
                    recommendation.append(f"   💡 是否想尝试不同的模型或音调？")
            
            # === 使用建议 ===
            recommendation.append(f"\n💬 如何使用此推荐？")
            recommendation.append(f"   直接说：'翻唱《{song_name or '歌曲名'}》'")
            recommendation.append(f"   我会自动应用以上推荐的配置！")
            recommendation.append(f"\n⚙️ 如需修改偏好，可以说：")
            recommendation.append(f"   - '把默认模型改成第2个'")
            recommendation.append(f"   - '默认升2调'")
            
            return "\n".join(recommendation)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"获取推荐时出错: {e}"

    @filter.llm_tool(name="view_my_stats")
    async def view_my_stats(self, event: AstrMessageEvent) -> str:
        '''查看当前用户的翻唱统计与常用音色。'''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无法查看统计数据。"
        
        try:
            user_id = self._get_user_id(event)
            pref = self._get_user_pref(user_id)
            
            stats = []
            stats.append("=" * 60)
            stats.append("📈 个人翻唱统计报告")
            stats.append("=" * 60)
            
            # === 基础统计 ===
            usage_count = pref.get("usage_count", 0)
            last_time = pref.get("last_used_time", "从未使用")
            
            stats.append(f"\n🔢 基础数据：")
            stats.append(f"   总翻唱次数: {usage_count}")
            stats.append(f"   最后使用: {last_time[:19] if last_time != '从未使用' else last_time}")
            
            # === 当前偏好 ===
            stats.append(f"\n⚙️ 当前偏好设置：")
            stats.append(f"   默认模型: 第{pref.get('model_index', '未设置')}个")
            stats.append(f"   默认音调: {pref.get('default_key_shift', '未设置'):+d}" if isinstance(pref.get('default_key_shift'), int) else "   默认音调: 未设置")
            
            # === 上次使用 ===
            last_model = pref.get("last_used_model", "无")
            stats.append(f"\n🎯 上次使用：")
            stats.append(f"   模型: {last_model}")
            
            # === 收藏歌曲 ===
            favorite_songs = pref.get("favorite_songs", [])
            if favorite_songs:
                stats.append(f"\n❤️ 收藏歌曲 ({len(favorite_songs)}首)：")
                for i, song in enumerate(favorite_songs[-10:], 1):
                    stats.append(f"   {i}. 《{song}》")
            else:
                stats.append(f"\n❤️ 收藏歌曲：暂无")
            
            # === 偏爱歌手 ===
            preferred_artists = pref.get("preferred_artists", {})
            if preferred_artists:
                sorted_artists = sorted(preferred_artists.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                stats.append(f"\n🎤 偏爱歌手（按频率排序）：")
                for artist, info in sorted_artists[:10]:
                    model_info = f", 常用: {info.get('model', '未知')}" if info.get('model') else ""
                    stats.append(f"   • {artist} - {info.get('count', 0)}次{model_info}")
            else:
                stats.append(f"\n🎤 偏爱歌手：暂无")
            
            # === 歌手→模型映射 ===
            artist_model_map = pref.get("artist_model_map", {})
            if artist_model_map:
                stats.append(f"\n🔗 歌手专属模型映射：")
                for artist, models in sorted(artist_model_map.items()):
                    best = max(models, key=models.get)
                    stats.append(f"   • {artist} → {best} ({models[best]}次)")
            
            # === 操作建议 ===
            stats.append(f"\n" + "-" * 60)
            stats.append(f"💡 可用操作：")
            stats.append(f"   • 查看推荐配置：'给我推荐个配置'")
            stats.append(f"   • 修改偏好设置：'把默认模型改成第2个'")
            stats.append(f"   • 清空历史数据：'清空我的翻唱历史'")
            
            stats.append(f"\n" + "=" * 60)
            stats.append(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            stats.append("=" * 60)
            
            return "\n".join(stats)
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"查看统计时出错: {e}"

    @filter.llm_tool(name="clear_my_history")
    async def clear_my_history(self, event: AstrMessageEvent) -> str:
        '''清空当前用户的翻唱历史与偏好，不可恢复；执行前提一句。'''
        if not self.enable_preference_learning:
            return "偏好学习功能已在配置中禁用。无需清除数据。"
        
        try:
            user_id = self._get_user_id(event)
            
            if user_id not in self.user_preferences:
                return "没有找到您的任何历史数据。"
            
            del self.user_preferences[user_id]
            await self._save_preferences()
            
            return "✅ 已清除您的所有翻唱历史和偏好数据！\n\n包括：\n• 使用统计\n• 偏好设置\n• 收藏歌曲\n• 偏爱歌手记录\n\n下次使用时将从零开始重新学习您的喜好。"
            
        except Exception as e:
            logger.error(traceback.format_exc())
            return f"清除历史时出错: {e}"

    # ==================== 本地音频翻唱 LLM 工具 ====================
    
    async def _resolve_local_audio_shift(
        self,
        audio_path: str,
        model_display: str,
        key_shift: Optional[int],
        event: AstrMessageEvent,
    ) -> tuple[int, Optional[int], str]:
        """本地音频的升降调：LLM 传入 → LLM 推断 → 本地推断 → 默认配置。

        Returns:
            ``(最终音调, 推断出来的音调或 None, 来源说明)``。
        """
        if key_shift is not None:
            return key_shift, None, "LLM传入"

        if self.enable_auto_key_shift:
            filename_base = os.path.splitext(os.path.basename(audio_path))[0]
            # 清理常见后缀：_EM, _cover, _RVC, (Live), [HQ] 等
            song_name = re.sub(r'[_\-\s]*(?:cover|rvc|em|ai|remix|inst|伴奏|翻唱|完整版|live|现场|mv|hq|flac|mp3|wav| instrumental)?\s*$', '', filename_base, flags=re.I).strip()
            song_gender, model_gender = await self._detect_gender_pair(
                song_name, model_display, event, source_is_song=True
            )
            if song_gender and model_gender:
                shifted = await self._calc_auto_key_shift(
                    song_name, model_display, 0, song_gender, model_gender, event
                )
                logger.info(f"[MatsukoCover] LLM自动推断音调: key_shift={shifted} (歌曲: {song_name}, 模型: {model_display})")
                return shifted, shifted, "LLM推断"

        inferred = self._infer_key_shift_from_filename(audio_path, model_display)
        if inferred is not None:
            logger.info(f"[MatsukoCover] 本地推断音调: key_shift={inferred} (文件: {os.path.basename(audio_path)}, 模型: {model_display})")
            return inferred, inferred, "本地推断"
        return self.default_key_shift, None, "默认配置"

    @filter.llm_tool(name="cover_absolute_path_audio")
    async def cover_absolute_path_audio(self, event: AstrMessageEvent,
                                         audio_path: str,
                                         model_name: Optional[str] = None,
                                         model_index: Optional[int] = None,
                                         key_shift: Optional[int] = None) -> str:
        '''用服务器上已有音频文件翻唱（用户给了本机绝对路径时）。

Args:
    audio_path(string): 音频绝对路径。
    model_name(string, optional): 音色名称，优先于序号。
    model_index(number, optional): 音色序号，从 1 开始。
    key_shift(number): 默认 0。'''
        rejected = self._reject_music_request(event, "cover_absolute_path_audio")
        if rejected:
            return rejected
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"

        if not audio_path or not os.path.exists(audio_path) or not os.path.isfile(audio_path):
            return f"❌ 提供的文件路径无效或文件不存在: {audio_path}"

        if not self._is_audio_file(audio_path):
            return f"❌ 提供的文件不是受支持的音频格式: {audio_path}"

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > self.max_local_audio_size_mb:
            return f"❌ 音频文件过大 ({size_mb:.1f}MB)，超过限制 {self.max_local_audio_size_mb}MB。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。"

        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"

        # 没显式指定时：先按语言挑（本地音频也能吃语言这一套），再回落默认音色
        if actual_model_index is None and getattr(self, "language_model_enable", True):
            guessed = self._guess_local_audio_language(
                getattr(event, "message_str", "") or "",
                os.path.basename(audio_path),
            )
            if guessed:
                language_index = self._language_model_index(guessed)
                if language_index:
                    actual_model_index = language_index
                    logger.info(
                        "本地音频按语言选音色: lang=%s -> 第%s个模型", guessed, language_index
                    )
        if actual_model_index is None:
            actual_model_index = self._configured_rvc_model_index() or 1

        models_info = self.get_models_detailed_list()
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break

        actual_key_shift, inferred_key_shift, inference_source = (
            await self._resolve_local_audio_shift(
                audio_path, actual_model_display, key_shift, event
            )
        )

        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            key_shift=actual_key_shift
        ))

        shift_info = f" | 升降调: {int(actual_key_shift or 0):+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"开始处理 {os.path.basename(audio_path)}，音色 {actual_model_display}{shift_info}"


    @filter.llm_tool(name="cover_local_audio")
    async def cover_local_audio(self, event: AstrMessageEvent,
                                 model_name: Optional[str] = None,
                                 model_index: Optional[int] = None,
                                 key_shift: Optional[int] = None) -> str:
        '''用本条消息附带或引用消息里的音频/语音翻唱。

Args:
    model_name(string, optional): 音色名称，优先于序号。
    model_index(number, optional): 音色序号，从 1 开始。
    key_shift(number): 默认 0。'''
        rejected = self._reject_music_request(event, "cover_local_audio")
        if rejected:
            return rejected
        if not self.enable_local_audio_cover:
            return "❌ 本地音频翻唱功能未启用。"

        audio_path = await self._extract_audio_from_event(event)

        if not audio_path:
            await self._cleanup_expired_audio_cache()
            cached = self._recent_audio_cache.get(str(event.unified_msg_origin))
            if cached and os.path.exists(cached[0]):
                audio_path = cached[0]
            else:
                return "❌ 未检测到音频文件或语音消息。请用户先发送要处理的音频文件或语音，然后再调用此工具。"

        if not self.enable_rvc:
            return "❌ RVC 功能已在配置中禁用。"

        actual_model_index = model_index
        actual_model_display = ""
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if matched:
                actual_model_index = matched
            else:
                available = await self.get_available_models(event)
                return f"❌ 未找到匹配的模型: '{model_name}'\n\n可用模型:\n{available}"

        # 没显式指定序号时，优先用配置下拉选的默认 RVC 音色
        if actual_model_index is None:
            actual_model_index = self._configured_rvc_model_index() or 1

        models_info = self.get_models_detailed_list()
        for m in models_info:
            if m["index"] == actual_model_index:
                actual_model_display = m["display"]
                break

        actual_key_shift, inferred_key_shift, inference_source = (
            await self._resolve_local_audio_shift(
                audio_path, actual_model_display, key_shift, event
            )
        )

        self._create_tracked_task(self._execute_local_cover(
            event, audio_path,
            model_name=None, model_index=actual_model_index,
            key_shift=actual_key_shift
        ))

        shift_info = f" | 升降调: {int(actual_key_shift or 0):+d}"
        if inferred_key_shift is not None:
            shift_info += f" ({inference_source})"
        return f"已提交本地音频：{os.path.basename(audio_path)}，音色 {actual_model_display}{shift_info}"


    # ==================== 方案A+B：任务状态查询与取消 ====================
    
    @filter.llm_tool(name="get_task_status")
    async def get_task_status(self, event: AstrMessageEvent) -> str:
        '''查询当前会话翻唱任务的进度或队列位置。'''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"
        
        active_task = self._get_active_task_status(event)
        if not active_task:
            return "✅ 当前没有正在进行的翻唱任务nya～您可以随时发起新的翻唱请求喵！🐾"
        
        elapsed = int(time.time() - active_task.get("start_time", 0))
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes}分{seconds}秒" if minutes > 0 else f"{seconds}秒"
        
        return (
            f"⏳ 有翻唱任务正在进行中nya～\n"
            f"歌曲：《{active_task['song_name']}》\n"
            f"模型：{active_task.get('model_display', '未知')}\n"
            f"状态：{active_task['status']}\n"
            f"已进行：{time_str}\n"
            f"请稍等，完成后会自动发送音频喵！如果卡住了可以用 cancel_cover_task 取消🐾"
        )

    @filter.llm_tool(name="cancel_cover_task")
    async def cancel_cover_task(self, event: AstrMessageEvent) -> str:
        '''取消当前会话正在排队或进行中的翻唱任务。'''
        if not self.enable_task_tracking:
            return "任务追踪功能已在配置中禁用。"
        
        key = self._get_task_key(event)
        if key not in self._active_cover_tasks:
            return "✅ 当前没有正在进行的翻唱任务，无需取消nya～"
        
        task_info = self._active_cover_tasks.pop(key, None)
        gradio_job = task_info.get("gradio_job")
        asyncio_task = task_info.get("asyncio_task")
        if gradio_job is not None:
            try:
                await asyncio.to_thread(gradio_job.cancel)
            except Exception as exc:
                logger.warning(f"取消 Gradio 后端任务失败: {exc}")
        if (
            isinstance(asyncio_task, asyncio.Task)
            and asyncio_task is not asyncio.current_task()
            and not asyncio_task.done()
        ):
            asyncio_task.cancel()
        return (
            f"🛑 已向插件和后端发送取消请求nya～\n"
            f"歌曲：《{task_info['song_name']}》\n"
            f"已进行：{int(time.time() - task_info['start_time'])}秒\n"
            f"您可以重新发起翻唱请求喵！🐾"
        )

    @filter.command("查看翻唱任务")
    async def view_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看翻唱任务'即可！")
            return
        result = await self.get_task_status(event)
        await self.selection_ui.send_card(
            event, "⏳ 翻唱任务", result, actions=[("刷新状态", "查看翻唱任务")]
        )

    @filter.command("取消翻唱任务")
    async def cancel_task_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'取消翻唱任务'即可！")
            return
        result = await self.cancel_cover_task(event)
        await self.selection_ui.send_card(event, "🛑 任务取消结果", result)

    # ==================== 辅助命令 ====================
    
    @filter.command("我的翻唱统计")
    async def view_stats_cmd(self, event: AstrMessageEvent):
        if self.llm_force_mode:
            yield event.plain_result("当前为LLM强制模式，请直接问我'查看我的翻唱统计'即可！")
            return
        result = await self.view_my_stats(event)
        await self.selection_ui.send_card(event, "📈 我的翻唱统计", result)

    # ==================== 本地音频翻唱：事件监听 ====================
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message_with_audio(self, event: AstrMessageEvent):
        '''监听包含音频的消息，自动触发本地翻唱'''
        if not self.enable_local_audio_cover:
            return

        msg_text = event.message_str.strip()

        # 排除命令消息（让命令处理器自己处理）
        if msg_text.startswith("/") or msg_text.startswith("\\"):
            return

        # 始终尝试提取并缓存音频（无论是否有触发词，方便后续 /本地翻唱 命令使用）
        audio_path = await self._extract_audio_from_event(event)
        if audio_path:
            async with self._audio_cache_lock:
                self._recent_audio_cache[str(event.unified_msg_origin)] = (audio_path, time.time())
            await self._cleanup_expired_audio_cache()
            logger.info(f"[LocalAudio] 已缓存音频: {audio_path}")

        # 检查是否触发自动翻唱
        if not self.local_audio_auto_trigger:
            return

        # 点歌类消息一律不自动翻唱：判定和词表在 intent_gate.py。
        # 缓存音频的那段在前面，所以之后手动发 /本地翻唱 仍然能用最近这条音频。
        if is_music_request(msg_text):
            logger.info("[IntentGate] 点歌类消息，跳过本地音频自动翻唱")
            return

        # 检查消息文字是否包含触发关键词
        has_trigger = any(kw in msg_text for kw in self.local_audio_trigger_keywords)

        # 如果文字没有触发，检查文件名是否包含触发关键词（如上传"处理.mp3"）
        if not has_trigger:
            for comp in getattr(event.message_obj, "message", []):
                if isinstance(comp, (File, Record)):
                    file_name = getattr(comp, "name", "") or ""
                    if any(kw in file_name for kw in self.local_audio_trigger_keywords):
                        has_trigger = True
                        logger.info(f"[LocalAudio] 文件名 '{file_name}' 匹配触发关键词")
                        break

        if not has_trigger:
            return

        if not audio_path:
            logger.info("[LocalAudio] 消息含触发词但无音频，跳过")
            return

        # 解析参数：只认「模型名/序号」和「升降调」，引擎固定 RVC
        words = msg_text.replace("，", " ").replace(",", " ").replace("。", " ").split()
        model_name = self.local_audio_default_model if self.local_audio_default_model else None
        key_shift = self.local_audio_default_shift

        for w in words:
            if w.lstrip('-').isdigit() and -12 <= int(w) <= 12:
                key_shift = int(w)
            elif w not in self.local_audio_trigger_keywords and len(w) >= 2:
                model_name = w

        # 验证模型名，不匹配则使用默认
        if model_name:
            matched = self._find_model_index_by_name(model_name)
            if not matched:
                model_name = None

        # 后台执行，不阻塞事件传播
        self._create_tracked_task(self._execute_local_cover(
            event, audio_path, model_name, key_shift=key_shift
        ))


    async def terminate(self):
        """插件卸载时清理资源"""
        logger.info("[MatsukoCover] 正在清理资源...")
        await self.selection_ui.close()
        
        # 取消所有待处理的任务
        if hasattr(self, '_pending_tasks') and self._pending_tasks:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
            self._pending_tasks.clear()
        
        # 保存偏好数据
        try:
            if hasattr(self, '_pref_lock') and self.enable_preference_learning:
                await self._save_preferences()
        except Exception as e:
            logger.error(f"[MatsukoCover] 卸载时保存偏好失败: {e}")
        
        # 清理API session
        try:
            if hasattr(self, 'song_router') and self.song_router:
                await self.song_router.close()
        except Exception as e:
            logger.debug(f"[MatsukoCover] 关闭音源路由 session: {e}")

        try:
            if hasattr(self, 'netease_fetcher') and self.netease_fetcher:
                await self.netease_fetcher.close()
        except Exception as e:
            logger.debug(f"[MatsukoCover] 关闭取歌 session: {e}")

        logger.info("[MatsukoCover] 资源清理完成")
