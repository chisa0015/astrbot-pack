# 安全说明

这个插件在本地做翻唱：取歌、分离、变声、混音、发回群聊。下面把它会碰到的边界写清楚，
方便安全审计和杀毒软件误报时核对。

## 插件本体（不会执行的东西）

- 运行时代码只通过 HTTP 调本机中间层 `chorus-mw`（默认 `http://127.0.0.1:3333/`），
  不加载模型权重、不读 RVC 目录之外的路径。
- 不做凭据外传：配置里的 cookie、token 只用于本机取歌服务。
- 不加载远程代码，没有混淆、没有 base64 解码执行、没有隐藏窗口。
- 写盘只在 `data/plugin_data/astrbot_plugin_chorus_cover/` 下（偏好、缓存、归档音频）。

## 安装脚本（需要手动运行）

`scripts/install.bat` 和 `scripts/installer.ps1` 只有用户自己双击才会执行，插件不会调用它们。

它们做的事，每一步都在窗口里打印出来：

1. 找 AstrBot 的 `data` 目录、RVC 整合包根目录、可用的 Python；
2. 从 **GitHub 官方地址**下载 `chorus-mw` 的固定 tag 压缩包；
3. 从 **breakfastquay 官方地址**下载 `rubberband` 可执行文件（升降调用）；
4. 解压到本机目录，往独立目录装 Python 依赖；
5. 写插件配置里的后端地址，启动中间层并等 `3333` 就绪。

下载地址写死在脚本里，没有第三方加速镜像，不会静默执行网上下载的脚本，也不改系统 PATH。

## 为什么 VirusTotal 可能有几条误报

上传的包里含 `.ps1` / `.bat` 安装脚本和 `subprocess` 调用，这类组合常被杀毒引擎按
「脚本下载可执行文件」启发式标记。核对时可以看这几点：

- 脚本里每个下载 URL 都是官方域名（`codeload.github.com`、`breakfastquay.com`）；
- 没有 `-EncodedCommand`、没有 `Invoke-Expression`、没有把内容写进临时脚本再执行；
- 下载完成后只做解压、复制、`pip install`；
- 唯一用到 `subprocess` 的地方是本地跑 `ffprobe` 探测音频时长，以及管理员执行
  `/重启中间层` 时拉起本机的 `chorus-mw`。

如果仍然不放心，可以完全不跑安装脚本：按 `INSTALL.md` 手动下载 `chorus-mw` 和
`rubberband`，再改配置里的后端地址即可，功能一样。

## 报告问题

发现真实的安全问题请开 issue，说明复现步骤和影响范围。
