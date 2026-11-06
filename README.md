# AstrBot 整合包 — 全量版

本地 AstrBot 实例的完整快照：**29 个插件** + **31 份配置**。

其中 **10 个自建插件**（`zz_` 前缀，**夜煞** 开发），**19 个第三方插件**。

---

## 目录结构

```
astrbot-pack-full/
├── README.md
├── install.ps1                  ← 一键安装（自动备份）
├── plugins-meta.json            ← 全部插件的元数据清单
├── plugins/                     ← 29 个插件
│   ├── astrbot_plugin_zz_*      ← 自建（10 个）
│   └── 其他                     ← 第三方（19 个）
└── config/                      ← 31 份插件配置
```

---

## 自建插件（10 个）

**作者：夜煞**

命名统一用 `astrbot_plugin_zz_` 前缀（`zz` = 作者标识）。源码就是本仓库 `plugins/` 下的对应目录。

| 插件 | 版本 | 功能 |
|---|---|---|
| `astrbot_plugin_zz_bangumi` | 1.0.0 | 命令 新番（本周番组表，可带条数）/ 今天新番（今日更新列表）。配置 push_enabled + push_time + push_tar… |
| `astrbot_plugin_zz_cosplay` | 1.1.0 | 命令 cos / 随机cos / 来张cos / cos图 [1-5] 抓 cosplay8 图集；命令里带 r18 则走绅士会所。图片先下… |
| `astrbot_plugin_zz_draw` | v1.1.0 | 生图/画图=txt2img，带图或说「图生图/改图/重绘」=img2img；本地 ComfyUI，两段式精修（手指细节）+ 手部加权词 |
| `astrbot_plugin_zz_fortune` | 1.0.0 | 命令 运势卡 / 抽运势 / 今日运势卡 [名字]。渲染 900×1440 图片：随机二次元壁纸、吉凶大字、五星制、爱情/事业/学业/财运/… |
| `astrbot_plugin_zz_hanime` | 1.0.0 | 命令 里番 [N]、随机里番 [N]、里番新作 [N]、里番 搜索 关键词。配置 push_enabled + push_time + pu… |
| `astrbot_plugin_zz_ja_lock` | 1.2.0 | 在 on_llm_request 末尾按会话类型注入语言规则：FriendMessage 且发送者在白名单时强制日语；群聊与非白名单私聊不注… |
| `astrbot_plugin_zz_jm` | v1.0.0 | JM 漫画下载并转成 PDF 发送 |
| `astrbot_plugin_zz_novel` | 2.0.0 | 命令 小说 / 随机小说 / 来本小说 / 看小说 [源] [章节数]；源可选 linovel（默认，linovel.net）或 wenku… |
| `astrbot_plugin_zz_setu_auto` | v1.0.0 | 消息里出现「色图/涩图/瑟图」就自动丢一张；带冷却、可限 R18、不抢别的命令 |
| `astrbot_plugin_zz_yuyu` | v1.0.0 | 反向图搜 · 谷歌 Lens 认人 + Yandex 找原帖；真人照片走 Lens，Yandex 补位与给出处 |

---

## 第三方插件（19 个）

版权归各原作者，LICENSE 保留在各自插件目录。请以原始仓库为最新版本来源。

| 插件 | 版本 | 说明 |
|---|---|---|
| `astrbot_plugin_bilibili_ai_bot` | 1.7.2 | B站评论/私信/直播弹幕自动回复、好感度系统、语义记忆、主动看视频、动态发布、联网查询 |
| `astrbot_plugin_bug_companion` | 0.3.5 | 为开发者群提供问题收集、人工审批、待办跟踪、仓库播报和可靠知识答疑。 |
| `astrbot_plugin_content_companion` | 0.2.2 | 独立创作与作品管理扩展。 |
| `astrbot_plugin_custom_menu` | 1.9.1 # 插件版本号。格式：v1.1.1 或者 v1.1 | 一个web菜单在线diy插件 |
| `astrbot_plugin_image_companion` | 0.4.5 | 陪伴插件的生图、改图、参考图库与多后端扩展。 |
| `astrbot_plugin_master_aware` | 4.2.2 | >- |
| `astrbot_plugin_memewiki` | 1.6.0 | 查梗：先查本地全量字典（毫秒级），未命中回落 cnmeme.wiki 公开 API；/梗 指令可出图片卡片，支持每天自动… |
| `astrbot_plugin_memory_companion` | 1.10.5 | 为 AstrBot 提供主链记忆注入、历史聊天整理导入、连续对话记忆、陪伴插件协同、主动记忆/回忆工具、知识图谱、权限边… |
| `astrbot_plugin_persona_canvas` | 0.5.14 | >- |
| `astrbot_plugin_pokepro` | v2.1.2 | 【专业的戳一戳插件】支持触发（反戳：LLM：face：图库：语音：禁言：命令），戳@某人，跟戳 |
| `astrbot_plugin_private_companion` | 6.5.6 | 可独立运行的 AstrBot 陪伴核心；记忆核心缺失时相关能力降级，可选扩展缺失时对应功能关闭。 |
| `astrbot_plugin_reality_companion` | 0.4.0 | 让 Bot 在明确授权和任务边界内通过米家设备、本机音频、摄像头单帧、Android 移动端和现实提醒来到用户身边。 |
| `astrbot_plugin_relationship_manager` | v5.2.14 | 支持加好友/加群/处理好友申请/修改好友备注/修改bot的qq昵称/处理好友申请/处理群申请/抽查群或好友聊天记录/拉黑… |
| `astrbot_plugin_rollpig_plus` | 3.11.11 | 基於 AstrBot RollPig 持續維護並擴展；部分後續功能參考或移植自 Felis2026/nonebot-pl… |
| `astrbot_plugin_screen_companion` | 3.3.1 | - |
| `astrbot_plugin_setu` | v2.2.0 | 随机福利图与随机本子文件插件，支持标签与数量、图片分级控制，以及 PDF/ZIP 两种本子发送格式。 |
| `astrbot_plugin_together_companion` | 0.8.4 | 和 Bot 语音或视频通话、一起看视频，或结合可选屏幕伙伴进行工作协同，并延续现有陪伴人格与生活状态。 |
| `astrbot_plugin_tts_rvc` | 1.1.0 | | |
| `meme_manager` | 5.2.11 | >- |

> 第三方插件的 `author` 字段里可能含非 ASCII 字符（部分作者用了中文/日文 ID），显示为乱码属正常，具体以各插件原始仓库为准。

---

## 安装

```powershell
# AstrBot 的 core 目录，例如：
# C:\Users\<你>\.astrbot_launcher\instances\<uuid>\core
powershell -ExecutionPolicy Bypass -File .\install.ps1 -AstrBotDir "<core>"
```

脚本会：检查目录 → **自动备份** `plugins` / `config` → 复制 → 提示重启。

手动：

```
plugins\*  ->  <AstrBot>\data\plugins\
config\*   ->  <AstrBot>\data\config\
```

---

## 注意事项

- 插件目录名保持 `astrbot_plugin_*` 前缀（`meme_manager` 例外）
- 所有插件需要**重启 AstrBot** 才生效
- 部分插件有外部依赖（TTS-RVC / ffmpeg / playwright / 代理）
- 配置里可能有写死的路径和端口，装完按需调整
- 代理默认 `127.0.0.1:7890`

---

## 统计

| 项 | 数量 |
|---|---|
| 插件总数 | 29 |
| 自建（夜煞） | 10 |
| 第三方 | 19 |
| 配置 | 31 |

打包日期：2026-11-06
