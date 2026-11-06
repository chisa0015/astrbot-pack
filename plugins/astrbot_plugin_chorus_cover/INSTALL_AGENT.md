# 给 agent 的安装与验收指南

这份文档是写给「帮用户装插件的 agent」看的：按顺序做，每一步都有可验证的结果。
给人类看的简版在 [INSTALL.md](INSTALL.md)。

## 0. 先确认边界

- 本插件只做「取歌 → 分离 → RVC 变声 → 混音 → 发回会话」，**不带任何模型权重**。
- 需要本机有 RVC 整合包（`runtime\python.exe`、`ffmpeg.exe`、`assets\weights`）和中间层 `chorus-mw`。
- 不要改 AstrBot 核心，不要动别的插件源码；只改本插件目录与 `data/config/` 下自己的配置。

## 1. 确认宿主

1. AstrBot ≥ 4.16，`data/` 目录在用户目录下（Windows 常见 `C:\Users\<user>\data`）。
2. 插件目录存在：`data/plugins/astrbot_plugin_chorus_cover`，`metadata.yaml` 里版本号能对上。
3. 记下 AstrBot 版本与插件版本，最后汇报要用。

## 2. 装中间层

1. 目标版本 **chorus-mw ≥ 0.9.4**（0.9.4 才支持 `chorus_start`；0.9.3 起支持按音域自动移调，默认关）。
2. 用仓库里的 `scripts/install.bat` 或手动：

```powershell
git clone https://github.com/void-HaoNan/chorus-mw
cd chorus-mw
$env:CHORUS_RVC_ROOT = 'D:\你的RVC整合包根目录'
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

3. 验证：`curl http://127.0.0.1:3333/model_info` 返回 JSON，且 `count` 大于 0。

## 3. 配插件

在 `data/config/astrbot_plugin_chorus_cover_config.json` 里确认：

| 键 | 期望 |
|---|---|
| `rvc_base_url` | `http://127.0.0.1:3333/`（结尾带斜杠） |
| `enable_rvc` | `true` |
| `language_model_zh/ja/en` | 按用户有的模型填，没有就留空 |
| `index_rate` | 默认 `0.8`，别随手改 |
| `persona_song_pick_enable` | 用户想要「按人设选歌」时打开 |

改完重启 AstrBot，日志里应出现 `Plugin astrbot_plugin_chorus_cover (0.17.x)`。

## 4. 验收（逐条给用户看结果）

1. 群里发 `/环境自检`：后端、模型数、运行时、ffmpeg、三档音色、取歌服务逐项有结论。
2. 发 `/刷新rvc模型`：列表里能看到用户的模型。
3. 点一首中文歌走完整流程：收到音频即通过；若失败，日志里搜 `CoverFlow`。
4. 连发两首：第二首应被「排队」接住，而不是被拒绝。
5. 让用户发「唱首歌」不带歌名：若开了 `persona_song_pick_enable`，应挑一首非烂大街的。

## 5. 已知边界（别当成 bug 报）

- 副歌里如果有说唱/念白段，RVC 转出来会糊；用 `chorus_start_seconds` 指定干净起点。
- 这个 RVC 整合包不支持 `harvest`（只认 pm/rmvpe/fcpe）。
- 自动移调默认关闭（实测不移调更贴音色）；需要时设 `CHORUS_AUTO_PITCH=1`。
- 语音转写受智谱 30 秒限制，引用长语音会失败。

## 6. 汇报模板

```
插件：astrbot_plugin_chorus_cover 0.17.0
中间层：chorus-mw 0.9.4（3333 在线）
模型：<n> 个；三档音色：zh=… ja=… en=…
实测：取歌 ✓ / 翻唱 ✓ / 排队 ✓ / 自检 ✓
未验：<列出没跑到的>
```

## 7. 自己训练模型时

- `train.py` 的 `-sw 1` 会在 `assets/weights` 里留一堆 `xxx_e30`、`xxx_e60` 检查点，
  它们会出现在音色列表里。训练时用 `-sw 0`，或训练完把这些文件移到 `logs/` 下。
- 最终模型起个能被看懂的名字（例如「角色名+语言」），中间产物不要留在 `weights` 里。
