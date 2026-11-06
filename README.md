# AstrBot 整合包 — 插件 · 配置 · 人格

一个可直接导入 AstrBot 的整合包，包含 3 个插件、1 套人格、1 份配置模板。

## 目录结构

```
astrbot-pack/
├── README.md                       ← 本文件
├── install.ps1                     ← 一键安装脚本
├── personas/
│   └── 色情鲸鱼/
│       ├── astrbot_persona.json    ← AstrBot 人格配置（可直接导入）
│       ├── persona.md              ← 人设正文（Markdown）
│       ├── 色情鲸鱼.png             ← 人格头像
│       └── 色情鲸鱼.v2.json         ← v2 格式人格卡
├── plugins/
│   ├── astrbot_plugin_bitchfinder/    ← 以图搜人（Google Lens 反查 coser 账号）
│   ├── astrbot_plugin_chorus_cover/   ← 合唱翻唱生成（TTS/音色转换）
│   └── astrbot_plugin_zz_trace_20261031/ ← 消息追溯 / 调试
└── config/
    └── config.example.json         ← 配置模板（参考字段，不要直接覆盖）
```

## 安装

### 方式一：一键脚本（推荐）

```powershell
# 右键 install.ps1 → 使用 PowerShell 运行
# 或者
powershell -ExecutionPolicy Bypass -File .\install.ps1 -AstrBotDir "D:\AstrBot"
```

脚本会：
1. 检查目标目录是否存在
2. 备份已有的 `data/plugins` 和 `data/persona`
3. 复制插件到 `data/plugins/`
4. 复制人格到 `data/persona/`
5. 提示需要重启 AstrBot

### 方式二：手动

**插件**：把 `plugins/` 下每个目录整个复制到 `<AstrBot>\data\plugins\`

**人格**：
- 进入 AstrBot 面板 → 配置 → 人格
- 或者把 `personas/色情鲸鱼/` 整个目录放到 `<AstrBot>\data\persona\色情鲸鱼\`
- `astrbot_persona.json` 里是完整的人设配置，面板可以直接导入

**配置**：`config/config.example.json` 只是参考，**不要直接覆盖**你已有的 `config.json`，按字段对照着改。

## 插件说明

### astrbot_plugin_bitchfinder

以图搜人。发一张 cosplay 图 → 走 Google Lens → 提取原作者的 X / Instagram / 抖音账号。

依赖：`playwright`（Chromium）、代理（默认 `127.0.0.1:7890`）

首次使用需要跑一次：
```bash
playwright install chromium
```

### astrbot_plugin_chorus_cover

合唱 / 翻唱生成，走 TTS + 音色转换。

需要额外配置：TTS 服务地址、RVC 模型路径（见插件目录里的 `INSTALL.md`）。

### astrbot_plugin_zz_trace_20261031

消息追溯，用于调试（记录消息来源 / 处理链路）。

## 人格说明

「色情鲸鱼」是一个偏亲密向的人设，`persona.md` 是正文，`astrbot_persona.json` 是配置。

导入后记得在 AstrBot 面板里**启用**这个人格，并绑定到对应会话。

## 注意事项

- 插件目录名必须保持 `astrbot_plugin_*` 前缀，否则 AstrBot 不识别
- 所有插件都要求 AstrBot **重启后**才生效
- 插件里可能写死了路径 / 端口，装完请检查各插件的 `_conf_schema.json` 和 `README.md`
- 代理端口默认 `7890`，如果你的不同请在插件配置里改

## 版本

- 打包日期：2026-11-06
- 来源：本地 AstrBot 实例的插件 + 人格导出
