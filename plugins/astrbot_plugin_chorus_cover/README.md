# astrbot_plugin_chorus_cover · RVC 副歌翻唱

给 AstrBot：https://github.com/AstrBotDevs/AstrBot 用的 **RVC 翻唱插件**：说一句「翻唱《歌名》」，它自己去找歌、切成副歌（或整首）、交给 RVC 后端换声，完事把音频发回群聊。

本项目的取歌与下载逻辑借用了 astrbot_plugin_music：https://github.com/Zhalslar/astrbot_plugin_music 的实现思路。

- 版本：`0.19.11`
- 作者：void-HaoNan
- 仓库：<https://github.com/void-HaoNan/astrbot-plugin-chorus-cover>
- **配套中间层（必需）**：`chorus-mw`：https://github.com/void-HaoNan/chorus-mw —— 本插件只负责找歌和发语音，切副歌、分离、移调、变声、混音都由它干；没有它插件连不上 `3333`，翻唱会直接失败
- 许可：AGPL-3.0（见文末「许可与出处」）
- **安全说明**：[`SECURITY.md`](SECURITY.md) —— 插件会碰哪些边界、安装脚本做什么、误报怎么核对
- **设置项清单**：[`SETTINGS.md`](SETTINGS.md) —— 每个设置由哪一行代码读取，防止「假设置」

---

## 推荐：让 DSH / AI 全自动安装

手工装这一套要碰 AstrBot、RVC 整合包、中间层、模型目录、Python 依赖五件事，人很容易漏。推荐把下面这句话直接交给 DSH（或任何能执行命令的 AI 助手）：

```
帮我安装并配置 astrbot_plugin_chorus_cover：
插件仓库 https://github.com/void-HaoNan/astrbot-plugin-chorus-cover
中间层仓库 https://github.com/void-HaoNan/chorus-mw
先读仓库里的 INSTALL_AGENT.md，按里面的检查项一步步做，装完在群里发 /环境自检，把结果贴给我。
```

它会照 `INSTALL_AGENT.md` 里的顺序检查：AstrBot 版本、插件目录、中间层 3333、RVC 整合包路径、ffmpeg、取歌服务、音色目录，缺哪步补哪步，最后用 `/环境自检` 给出结论。

## 更新：插件市场点更新，不等于全部更新

插件市场点「更新」只会换掉**插件本身的代码**（`astrbot_plugin_chorus_cover`）。配套的中间层 `chorus-mw`：https://github.com/void-HaoNan/chorus-mw 和 RVC 整合包不在插件市场里，不会跟着变。

所以每次插件大版本更新后，建议把下面这句丢给 DSH：

```
把 chorus-mw 更新到最新版：https://github.com/void-HaoNan/chorus-mw
拉完依赖、重启中间层，然后在群里发 /环境自检，把结果贴给我。
```

判断要不要更新中间层的简单办法：插件更新日志里出现「需要中间层 x.y.z 起」的说明时，就必须更。

更新完一定跑一次自查：群里发 `/环境自检`（管理员），或者双击 `scripts\install.bat -CheckOnly`，它会报后端、模型、运行时、ffmpeg、音色目录、取歌服务逐项状态。

---

## 它是怎么干活的

```
用户：翻唱《雨爱》
  │
  ├─ 取歌（source_order 依次尝试）
  │    网易云 ─→ 酷狗 ─→ 酷我 ─→ QQ 音乐
  │    · 歌名整串原样搜，别名按「标题+别名」拼接匹配
  │    · 伴奏/纯享/消音类候选先淘汰
  │    · 取直链 → 下载 → 量真实时长，过 80 秒闸门
  │
  ├─ 交后端（rvc_base_url，Gradio /convert）
  │    · 按 render_mode 切副歌 / 整首 / 自定义时长
  │    · RVC 换声 + 伴奏同步移调 + 混音
  │
  └─ 回群：音频；失败只说一句话（台词由人格模型现编）
```

插件本身**不做推理**，只负责取歌、调后端、回消息；推理由 RVC 后端完成（与本仓库配套的是 `chorus-mw`）。

## 功能

- **只做 RVC**：一个推理引擎、一条链路；没有 SVC、没有参考音色，设置页里也不会出现无效开关。
- **音色/音量全在设置页**：`f0_method`、`index_rate`、`rms_mix_rate`、`protect`、`inst_volume_db`、`vocal_volume_db` 都会按名字传给中间层（chorus-mw 0.8.0 起真的生效），调完下一次翻唱就能听出来。
- **多音源取歌**：`source_order` 决定顺序与启停，默认网易云 → 酷狗 → 酷我 → QQ 音乐；前一个源没成自动换下一个，日志里每个源一行结论。
- **匹配规则确定**：歌名整串原样搜；别名只在「标题+别名 == 歌名」时命中；歌手只在用户明确说出时当过滤条件；伴奏类候选直接淘汰。
- **80 秒闸门**：下载后量真实时长，太短的片段/试听不给翻唱。
- **时长模式**：`chorus`（默认，45 秒副歌）/ `full`（整首）/ `custom`（自定义 20~180 秒）。
- **本地音频翻唱**：发语音/音频后 `/本地翻唱`，或把音频和文字放同一条消息；也能让 LLM 直接处理某个绝对路径。
- **批量翻唱**：一次多首，逐首发送 + 最后给汇总报告。
- **任务与统计**：`/查看翻唱任务`、`/取消翻唱任务`、`/我的翻唱统计`；LLM 侧有 `get_task_status` / `cancel_cover_task` / `view_my_stats`。
- **自动升降调**（可选）：识别原曲歌手与模型的性别，男→女升调、女→男降调，人声与伴奏一起移调。
- **统一出口**：`outbound.py` 是插件唯一的对话出口——进度、重试默认静音，失败只发一句人话，技术细节只写日志。0.10.1 起不再有绕过出口、直接往群里发固定进度文案的分支。
- **缓存管理**：`/查看翻唱缓存`、`/清理翻唱缓存`（管理员）。
- **按语言自动换音色**（0.13.0）：先看歌词字形判中文/日语/英文，再套用 `language_model_zh/ja/en` 里配的音色；用户自己点名的音色优先，判不出来或没配就回落默认音色并提示一句。
- **库同步与自愈**（0.14.0）：`/同步音色库` 把分类库里的模型自动硬链接进 `assets` 并刷新；`/重启中间层` 一条命令拉活中间层；`/环境自检` 一次报完环境缺什么。
- **连唱两首会排队**（0.15.0）：同一会话已有任务时，后面的请求排进队列（最多 3 首），前一首做完自动接着唱、自动发送，不再直接拒绝。
- **点歌 / 翻唱分开判定**：所有翻唱入口先过 `intent_gate.py` 的统一闸门，点歌类消息不会被翻唱接走。

## 唱多长：三种翻唱模式

在插件配置里用 `render_mode` 选，默认 `chorus`：

| 模式 | 唱多长 | 后端怎么处理 | 实测（4:39 的歌） |
| --- | --- | --- | --- |
| `chorus`（默认） | 副歌片段，默认 45 秒 | 先切 60 秒去分离，再在人声里精挑 45 秒 | **32.6 秒** 出成品 |
| `custom` | 自定义 20~180 秒（`chorus_seconds`） | 先切「时长 + 15 秒」去分离，再精挑 | `chorus_seconds=30` → **27.1 秒** |
| `full` | 整首 | 分离整首、变声整首，不做副歌挑选 | 100 秒素材 **52.7 秒**；三四分钟的歌要几分钟 |

`full` 的耗时可能是副歌模式的 5~10 倍，显存占用也更高（长音频会把显存吃到接近上限后降速）。要整首翻唱，请同时调大 `inference_timeout` 与 `task_timeout_seconds`。

插件会自动探测后端是否支持 `render_mode`：后端没有这个参数就整组不传，按副歌模式处理，升级中间层前后都能用。换 `render_mode` 或 `chorus_seconds` 会换缓存 key，不同模式不会互相命中。

## 音色与音量怎么调

这六项在插件设置里改，中间层每次请求都按名字收，调完下一次翻唱就生效（不用重启）：

| 配置项 | 默认 | 范围 | 调大 / 调小分别会怎样 | 推荐 |
| --- | --- | --- | --- | --- |
| `f0_method` | `rmvpe` | `rmvpe` / `pm` / `crepe` / `harvest` | `rmvpe` 最稳、适合干净人声；`pm` 最快、质量一般；`crepe` 更吃显卡；`harvest` 跑调时再试 | 先用 `rmvpe` |
| `index_rate` | `0.75` | 0~1 | 调大更贴模型音色、但容易生硬；调小模型自由发挥更多、可能偏离音色 | 0.6~0.85；沙哑/电音时试 0.3~0.5 |
| `rms_mix_rate` | `0.35` | 0~1 | 调大更贴原唱的音量起伏、更自然，人声可能被压小；调小音量更平稳、人声更冲，太小会发闷 | 0.3~0.5 |
| `protect` | `0.33` | 0~0.5 | 调大保护齿音/换气/气声，撕裂和沙音更少；调小更贴音色，但容易出电流味 | 0.33 左右；失真时 0.4~0.5 |
| `inst_volume_db` | `-6` | -20~0 | 调大（往 0）伴奏更响、现场感强；调小（往 -20）伴奏更弱、几乎只剩人声 | -6；人声被盖住就 -10 |
| `vocal_volume_db` | `0` | -12~+12 | 调大人声更靠前更抢耳；调小人声往后退、像混在伴奏里 | 0；人声太尖 -3，太弱 +3 |

设置页只保留真正能改变成品或流程的项。`filter_radius`、混响/延迟强度、人声后处理、整组 `uvr5_*` 与 `msst_*` 已在 0.10.1 删除——chorus-mw 0.8.0 的 `/convert` 收下这些形参后并不消费，留着只会让人以为能调。分离模型由后端启动配置（`CHORUS_SEP_MODEL`）决定，插件设置页不再提供这个假开关。

## 环境与安装

- AstrBot `>= 4.x`
- Python 依赖：`gradio_client`、`aiohttp`（见 `requirements.txt`）

**装完插件先双击 `scripts\install.bat`**：它会找 AstrBot 数据目录、RVC 整合包、chorus-mw，
缺什么补什么（中间层、Python 依赖、rubberband），最后启动 3333。只想看当前状态就加 `-CheckOnly`。
完整步骤和排障见 [`INSTALL.md`](INSTALL.md)。

装好以后在群里发 `/环境自检`（管理员）看结果：后端、模型、运行时、ffmpeg、三档语言音色、取歌服务
一行一个结论，全绿才能唱。

```bash
# 装到 AstrBot 的插件目录
cd AstrBot/data/plugins
git clone https://github.com/void-HaoNan/astrbot-plugin-chorus-cover.git astrbot_plugin_chorus_cover
pip install -r astrbot_plugin_chorus_cover/requirements.txt
```

装完在 WebUI 的插件管理里重载，然后：

1. 打开插件配置，把 `rvc_base_url` 填成你的 RVC 后端地址（默认 `http://127.0.0.1:3333/`）。
2. 发 `/刷新rvc模型`，确认能读到模型列表。
3. 想指定默认音色就选 `rvc_default_model`（下拉里是模型文件名），不选就用模型列表第 1 个。

可选：想让酷狗/酷我/QQ 音乐参与取歌，装上点歌插件 `astrbot_plugin_music` 并配好对应平台；不装也能用，只是这三源会被跳过、只走网易云。

## 后端准备

**后端就是姊妹仓库 `chorus-mw`：https://github.com/void-HaoNan/chorus-mw**（不是随便一个 Gradio 服务）。先把它跑起来：`http://127.0.0.1:3333/`，然后本插件才能工作。装法和启动脚本都在那个仓库的 README 里。

插件按 Gradio API 说话，默认端口 `3333`，需要后端提供：

| 接口 | 用途 | 是否必需 |
|---|---|---|
| `/convert` | 提交翻唱任务 | 必需 |
| `/show_model` | 模型列表（`/刷新rvc模型` 用） | 必需 |
| `/model_info` | 模型文件夹 + 列表（0.11.0 起优先用；后端没有就退回 `/show_model`） | 可选 |
| `/cache_info` / `/clear_cache` | 缓存查看 / 清理 | 可选 |
| `/show_msst_models` / `/select_msst_model` | 分离模型列表 / 切换 | 可选（没有就自动跳过） |

参数是**按名字探测**的：后端 `/convert` 里没有 `render_mode` 就整组不传，没有 MSST 参数就沿用后端配置，没有 0.8.0 新增的 `rms_mix_rate` / `protect` / `inst_volume_db` / `vocal_volume_db` 也不传，不会因为多传参数报错。

本仓库配套的中间层是 `chorus-mw`：https://github.com/void-HaoNan/chorus-mw：它自己拉起 RVC 整合包、切副歌、分离、移调、混音，插件只要给它一个音频路径。

> 注意：哪些参数真正生效由后端决定。`chorus-mw` **0.8.0 起**会真的使用 `f0_method`、`index_rate`、`rms_mix_rate`、`protect`、`inst_volume_db`、`vocal_volume_db`（传进 RVC 推理和混音），日志里每次请求打一行 `params ...` 可核对；`filter_radius`、混响/延迟、UVR5、MSST 那几组虽然还在 `/convert` 形参里，但中间层不消费，0.10.1 已从设置页删除。

## 音色模型放哪（0.12.0 起有一键打开）

两个目录，别搞混：

| 放什么 | 默认目录 | 说明 |
| --- | --- | --- |
| 音色模型 `.pth` | `<RVC 整合包>\assets\weights\` | 每个音色一个文件，放进去就能在列表里看到 |
| 特征索引 `.index` | `<RVC 整合包>\assets\indices\` | 可选；按模型名模糊匹配，找不到也能唱，只是音色贴合度差一点 |

具体位置以 `/rvc模型文件夹` 回显为准。

**别去设置里找路径，直接发命令**：`/rvc模型文件夹`（管理员）会把两个真实目录回显出来，
并在跑机器人的这台机器上**直接把模型文件夹打开**，丢文件进去、再发一次 `/刷新rvc模型` 就行。
这条命令的目录是中间层 `/model_info` 现查的，不是插件瞎猜。

**想换成别的目录**，三步，缺一不可：

1. 把 `.pth` 放到新目录（例如 `D:\models\rvc`）；
2. 给中间层设环境变量 `CHORUS_WEIGHTS_DIR=D:\models\rvc`，重启中间层（启动日志会打
   `音色模型目录=...`；中间层会把这个路径作为 RVC 的 `weight_root` 传给 RVC 进程，
   RVC 才真的去那儿加载）；
3. 在插件设置里把 `rvc_models_dir` 填成同一个路径（不填也能跑，填了会拿来做一致性核对），
   再发一次 `/刷新rvc模型`。

只改插件设置里的 `rvc_models_dir` **不会**让 RVC 换目录——RVC 只认它自己进程里的
`weight_root`。填了但和中间层不一致时，插件会在结果里明确告警，不会静默用错模型。

索引目录不跟 `CHORUS_WEIGHTS_DIR` 走，始终是 RVC 的 `assets\indices`（中间层 `/model_info`
的 `index_dirs` 会把实际搜索路径列出来）。

### 想按语言/角色分类存放：用硬链接，别直接搬走（0.13.1）

RVC 只扫 `assets\weights` 这一层，**不认子目录**。想整理成 `中文\安可\`、`日语\Yuuka\` 这种树，
就把真实文件放库里，再在 `assets\weights` 建同名硬链接——对 RVC 来说它还是一个普通文件，
对你就多了一份按语言分类的库。本机库在 `Documents\GPT SoVITS-RCV 语音模型\RVC\`，
结构是 `中文/角色/角色.pth + 角色.index`、`日语/...`、`未分类/...`。

注意事项：

- 硬链接只能在同一块硬盘上建（库和整合包都在 `D:` 才行）；跨盘只能用复制或软链接。
- 在库里改名、移动文件夹不会断链；**删库里的文件等于删模型的真实数据**，会连链接一起失效。
- 新增模型时先放进库，再建链接，最后 `/刷新rvc模型`；链接名必须含模型名，索引才会被匹配到。
- 索引也一起进库（同目录），`assets\indices` 里建对应的硬链接，链接名保留模型名。

## 命令表

| 命令 | 权限 | 说明 |
|---|---|---|
| `/rvc <歌名> [升降调]` | 所有人 | 主入口：搜歌 → 选歌 → 选音色 → 翻唱。升降调 -12~12 |
| `/本地翻唱 [模型名/序号] [升降调]` | 所有人 | 翻唱刚发的语音/音频；同一条消息里带音频也可以 |
| `/翻唱帮助` | 所有人 | 用法卡片 + 常用入口按钮 |
| `/刷新rvc模型` | 所有人 | 从后端拉模型列表，写回配置（保留已配的别名），并回显模型 / 索引目录 |
| `/rvc模型文件夹` | 管理员 | 回显音色模型与索引的真实目录，并直接打开模型文件夹（0.12.0） |
| `/查看翻唱任务` | 所有人 | 当前会话有没有在跑的任务 |
| `/取消翻唱任务` | 所有人 | 取消当前任务（同时通知后端停） |
| `/我的翻唱统计` | 所有人 | 个人使用统计与偏好 |
| `/查看翻唱缓存` | 所有人 | 后端缓存占用 |
| `/列出msst模型` | 所有人 | 后端装了的分离模型（列不出来说明后端没这接口） |
| `/设置rvc后端链接 <URL>` | 管理员 | 改后端地址并写回配置 |
| `/切换msst模型 <序号或模型名>` | 管理员 | 切换后端默认分离模型 |
| `/清理翻唱缓存` | 管理员 | 安全清理后端缓存 |
| `/翻唱选择 <token> <choice>` | 所有人 | 选择菜单按钮的回调入口（菜单过期时也走到这里） |

`llm_force_mode=true` 时手动命令会被拦下，改由对话触发。

## LLM 工具表

交给模型自己决定调用；docstring 里写清了「什么时候能调、什么时候不能调」。

| 工具 | 说明 |
|---|---|
| `smart_cover` | 一步到位：搜歌 + 选音色 + 翻唱（最常用） |
| `rvc_cover` | 指定歌名翻唱，参数比 `smart_cover` 直白 |
| `search_music` | 按音源顺序搜歌，返回候选列表 |
| `get_available_models` | 列出可用 RVC 模型（序号 / 显示名 / 文件名 / 别名） |
| `batch_cover` | 批量翻唱多首，异步后台跑 |
| `confirm_selection` | 自然语言确认/更换/取消选择 |
| `save_preference` | 记住默认模型序号、默认音调 |
| `get_recommendation` | 按历史推荐配置 |
| `view_my_stats` | 个人统计 |
| `clear_my_history` | 清空个人历史与偏好 |
| `cover_local_audio` | 翻唱用户刚发的语音/音频 |
| `cover_absolute_path_audio` | 翻唱本机某个绝对路径的音频 |
| `get_task_status` | 查当前任务状态 |
| `cancel_cover_task` | 取消当前任务 |

## 配置表

### 连接与模型

| 配置项 | 默认 | 说明 |
|---|---|---|
| `rvc_base_url` | `http://127.0.0.1:3333/` | RVC 后端（Gradio）地址，必须以 `/` 结尾 |
| `enable_rvc` | `true` | 总开关；关掉后 `/rvc`、各工具、本地音频入口都会提示已关闭 |
| `rvc_models_dir` | 空 | 音色模型（`.pth`）文件夹。留空 = 自动用中间层实际目录（默认 `<RVC>\assets\weights`）；填了会拿去和中间层核对，不一致时 `/刷新rvc模型` 会告警。换目录的正确做法见「音色模型放哪」 |
| `model_library_dir` | 空 | 分类音色库根目录（`中文/角色/`、`日语/角色/` 这种）。填了才能用 `/同步音色库`，自动把库里的模型硬链接进 `assets` 并刷新列表 |
| `rvc_models_keywords` | `[]` | 模型列表（`文件名\|\|\|别名`），由 `/刷新rvc模型` 自动写入 |
| `rvc_default_model` | 空 | 下拉选默认音色；留空用模型列表第 1 个 |
| `language_model_enable` | `true` | 按歌曲语言自动选音色（中文/日语/英文三档），关掉就只用 `rvc_default_model` |
| `language_model_zh` | 空 | 中文歌用哪个音色；留空走默认音色 |
| `language_model_ja` | 空 | 日语歌用哪个音色；留空走默认音色 |
| `language_model_en` | 空 | 英文歌用哪个音色；留空走默认音色 |
| `timeout` | `30` | 等用户选歌/选音色的超时（秒） |
| `inference_timeout` | `300` | 单次推理超时（秒）；整首模式建议调大 |
| `music_api_timeout` | `20` | 搜歌接口超时（秒） |

### 取歌

| 配置项 | 默认 | 说明 |
|---|---|---|
| `source_order` | `[netease, kugou, kuwo, qqmusic]` | 音源顺序，同时决定启停；删掉某项等于停用该源 |
| `netease_quality` | `320` | 网易云音质，只认 128 / 192 / 320 |
| `netease_meting_base` | `https://api.qijieya.cn/meting/` | meting 取直链服务（主路） |
| `netease_api_base` | `https://zm.wwoyun.cn` | NodeJS 网易云 API（备用路），留空则只用 meting |
| `netease_api_token` | 空 | 备用路需要鉴权时填令牌 |
| `netease_cookie` | 空 | 网易云 `MUSIC_U`，会员歌/高音质要用 |
| `alert_admin_on_cookie_expired` | `true` | cookie 连续失效时私聊管理员 |
| `cookie_alert_interval_hours` | `6` | 同一失效状态的提醒去重窗口（小时） |

### 时长与音质

| 配置项 | 默认 | 说明 |
|---|---|---|
| `render_mode` | `chorus` | `chorus` 副歌 / `full` 整首 / `custom` 自定义 |
| `chorus_seconds` | `45` | `custom` 模式的时长，20~180 秒 |
| `f0_method` | `rmvpe` | RVC 音高提取算法（`rmvpe` / `pm` / `crepe` / `harvest`） |
| `index_rate` | `0.75` | 特征检索比率 0~1，越高越贴模型音色 |
| `rms_mix_rate` | `0.35` | 0~1，越高越贴原唱的音量包络 |
| `protect` | `0.33` | 0~0.5，越大越保护清音/气声，越不容易撕裂 |
| `inst_volume_db` | `-6` | -20~0，伴奏音量（dB，负数是压小） |
| `vocal_volume_db` | `0` | -12~+12，人声音量（dB，0 为原样） |
| `shift_accompaniment` | `true` | 升降调时伴奏跟着一起移调 |

### 自动升降调

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enable_auto_key_shift` | `false` | 用户没说升降调时，按歌手/模型性别自动调 |
| `enable_auto_key_shift_debug` | `false` | 把推断过程发到群聊（调试用） |
| `gender_detection_timeout` | `15` | 性别识别超时（秒） |
| `male_to_female_shift` | `12` | 男声原曲 → 女声模型时的升调值 |
| `female_to_male_shift` | `-12` | 女声原曲 → 男声模型时的降调值 |
| `artist_gender_map` | `[]` | 歌手性别映射（`歌手:male` / `歌手:female`） |
| `model_gender_map` | `[]` | 模型性别映射（`模型别名:male` / `模型别名:female`） |
| `default_key_shift` | `0` | 默认音调，用户没说时用它 |

### 输出与通知

| 配置项 | 默认 | 说明 |
|---|---|---|
| `notify_progress` | `false` | 发进度提示到群里（默认静音，走统一出口） |
| `notify_retry` | `false` | 发重试提示到群里（默认静音） |
| `enable_config_report` | `true` | 完成后回一条「本次配置」 |
| `enable_send_file` | `false` | 除了音频再以文件形式发一份 |
| `enable_llm_success_notify` | `true` | 完成后让 LLM 说一句 |
| `enable_inject_context` | `true` | 把任务结果注入对话上下文 |
| `llm_force_mode` | `false` | 关掉手动命令，只能靠对话触发 |
| `qq_official_cards` | `true` | QQ 官方机器人用 Markdown 卡片 |
| `qq_official_buttons` | `true` | QQ 官方机器人用交互按钮 |

### 本地音频

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enable_local_audio_cover` | `true` | 允许本地音频翻唱 |
| `max_local_audio_size_mb` | `20` | 单个音频大小上限（MB） |
| `local_audio_cache_ttl` | `300` | 发过的音频在这么久内可直接 `/本地翻唱` 复用（秒） |
| `local_audio_auto_trigger` | `false` | 带音频的消息自动翻唱（不用发命令） |
| `local_audio_trigger_keywords` | `[翻唱, 变声]` | 自动触发关键词 |
| `local_audio_default_model` | 空 | 自动触发时用的模型 |
| `local_audio_default_shift` | `0` | 自动触发时的音调 |

### 偏好与任务

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enable_smart_cover` | `true` | 允许 `smart_cover` 工具 |
| `enable_batch_cover` | `true` | 允许 `batch_cover` 工具 |
| `enable_confirm_mechanism` | `true` | 允许 `confirm_selection` 工具 |
| `enable_enhanced_context` | `true` | `search_music` 输出更详细的结果 |
| `enable_preference_learning` | `true` | 偏好学习（默认模型/音调、歌手→模型映射） |
| `preference_storage_path` | `data/user_preferences.json` | 偏好存放路径 |
| `max_batch_size` | `5` | 一次批量最多几首 |
| `enable_task_tracking` | `true` | 任务状态追踪（查看/取消靠它） |
| `task_timeout_seconds` | `9000` | 任务超时自动释放（秒） |

## 使用示例

**对话触发（推荐）**

```
用户：翻唱《晴天》
机器人：（搜歌 → 选歌 → 选音色 → 后台跑完发音频）

用户：用塔菲的模型唱《雨爱司凤版》
机器人：（song_name='雨爱司凤版'，模型走别名匹配）

用户：升 3 调唱《起风了》
机器人：（key_shift=3）

用户：帮我翻唱这三首：《晴天》《起风了》《告白气球》
机器人：（批量，逐首发送 + 汇总）
```

**命令触发**

```
/rvc 晴天
/rvc 晴天 3
/刷新rvc模型
/本地翻唱 塔菲 2
/查看翻唱任务
/我的翻唱统计
```

**本地音频**

```
[发一条语音] → /本地翻唱
# 或者
[发一条带音频的消息，正文写「翻唱」]（需要开 local_audio_auto_trigger）
```

## 命名建议

音色文件名建议带语言后缀，一眼能分清：中文加 `CN`、日语加 `JP`，例如
`安可CN_可爱.pth`、`安可JP.pth`；同一角色有多个语气版本时再加后缀区分。
索引文件用同样的名字（`.index`），插件按模型名匹配，改名前先确认索引一起改。

## 常见问题

**模型列表里怎么冒出一堆 `xxx_e30`、`xxx_e60`？**
那些是训练时的中间检查点（每 10 轮存一次的 `-sw 1` 产物），会被当成可用音色列出来。
训练时用 `-sw 0` 只留最终模型，或者训练完把它们从 `assets/weights` 移到 `logs/` 下面。



**Q：`/rvc` 说「当前没有可用的 RVC 模型」？**
先 `/刷新rvc模型`。它读后端的 `/show_model`，后端没起或地址填错都会空。

**Q：一直提示「所有音源都没找到能确认是《X》的版本」？**
先看日志里每个源那行结论。常见原因：歌名带了奇怪的版本后缀、只有伴奏版、或者需要 cookie 才拿得到。换个更完整的歌名，或调整 `source_order`。

**Q：只有伴奏版？**
插件会主动排除伴奏/纯享/消音类候选；如果搜到的候选全是伴奏，会直接说「只找到伴奏版」，不会拿它去翻唱（那样出来是静音）。

**Q：翻唱很慢 / 超时？**
`full` 模式是整首分离 + 整首变声，耗时可能是副歌模式的 5~10 倍。改回 `chorus`，或把 `inference_timeout` 调大。

**Q：升调以后人声和伴奏不在一个调上？**
保持 `shift_accompaniment=true`。插件会把同样的半音数同时给人声和伴奏，两者时长一致才对得齐。

**Q：换了模型但没生效？**
优先级：用户明确指定的模型名/序号 > 语言对应音色（`language_model_zh/ja/en`）> `rvc_default_model` > 列表第 1 个。下拉里选的模型如果不在当前列表（没刷新、模型被删），会回落到第 1 个并在日志里记一条 warning。

**Q：语言音色怎么配，判错了怎么办？**
在设置里把「按歌曲语言自动选音色」打开，再把中文/日语/英文三档各选一个模型；不选的档位走默认音色。判断只看歌词的字形占比（没歌词才看歌名和歌手），模型名字不参与，判不出来就回落、不猜。日志里搜 `stage=language_detect` 能看到这首歌判成什么、依据是什么、最后用了哪个模型；如果某首歌判错，单独点名音色再唱一遍即可覆盖那一次。

**Q：`language_model_*` 的下拉里没有新模型？**
下拉选项由 `/刷新rvc模型` 写回 `_conf_schema.json`，写完需要重载一次插件，设置页才会显示新列表。

**Q：`/列出msst模型` 报错？**
说明后端没提供 `/show_msst_models`。这功能是可选的，不影响翻唱。另外 chorus-mw 0.8.0 的分离模型由后端启动配置（`CHORUS_SEP_MODEL`）固定，`/切换msst模型` 不会改变实际分离模型；设置页因此不再提供 MSST 参数。

**Q：进度一条都看不到？**
默认就是静音的（`notify_progress=false`）。要看得把它打开，或者看日志。0.10.1 起后端进度条那条直发通道已删除。

**Q：想完全不让用户用命令？**
打开 `llm_force_mode`，只能通过对话触发；也可以直接关 `enable_rvc`。

**Q：点歌消息会被翻唱抢走吗？**
不会。所有翻唱入口先过 `intent_gate.py`：命中「点歌/放首歌/听」这类意图的消息不会被翻唱接走，`local_audio_auto_trigger` 也会跳过点歌类消息。

## 更新日志

只记最近几条，完整历史见 [CHANGELOG.md](CHANGELOG.md)。

- **v0.10.1**：清理设置页——删除 17 个历史遗留/重复/空转项（`default_model_index`、`filter_radius`、混响/延迟/人声后处理、`uvr5_*`、`msst_*`、进度条两个开关），删掉的项在中间层本来就不生效。
- **v0.10.0**：设置页补齐 `rms_mix_rate` / `protect` / `inst_volume_db` / `vocal_volume_db` 并按名字传给 `/convert`；配合 chorus-mw 0.8.0，`index_rate` 等推理参数与两条轨道的音量真正生效（不再写死）。
- **v0.9.0**：收窄为纯 RVC 插件，移除另外两套语音转换引擎与旧 QQ 音乐相关命令和配置，设置页只留生效项。
- **v0.8.0**：选歌判定改成确定规则（整串当歌名、歌手仅明确指定时生效、别名拼接匹配、伴奏排除）。
- **v0.7.0**：多音源路由（网易云 → 酷狗 → 酷我 → QQ 音乐，可配顺序）。
- **v0.6.0**：`render_mode` / `chorus_seconds`（副歌 / 整首 / 自定义）。

## 许可与出处

- 本插件以 **AGPL-3.0** 发布，`LICENSE` 保持上游原样。
- 取歌与下载逻辑借用了点歌插件 astrbot_plugin_music：https://github.com/Zhalslar/astrbot_plugin_music（作者 Zhalslar）的实现思路。
- 本分支最初基于 sdfsfsk/matsuko_cover：https://github.com/sdfsfsk/matsuko_cover v2.13.0（AGPL-3.0）改写，来源与修改声明见 [NOTICE.md](NOTICE.md)。
- 上游代码版权归原作者所有；本分支新增与修改的部分同样以 AGPL-3.0 发布。
