<#
  chorus-cover 一键安装器（0.13.2）
  --------------------------------
  双击 install.bat 运行；也可以手动：
    powershell -ExecutionPolicy RemoteSigned -File scripts\installer.ps1

  它做这些事（每一步都会打印结果，不会静默改系统）：
    1. 找到 AstrBot 数据目录
    2. 找到 RVC 整合包（要 runtime\python.exe 和 assets\weights）
    3. 找到或下载 chorus-mw 中间层
    4. 给中间层装依赖（libs_web / libs）
    5. 补齐 rubberband（升降调用）
    6. 写插件配置里的后端地址
    7. 启动中间层并等待 3333
    8. 打印接下来要做什么

  不会：改系统 PATH、装驱动、静默执行网上的脚本。
#>
[CmdletBinding()]
param(
    [string]$RvcRoot = "",
    [string]$ChorusDir = "",
    [string]$AstrBotData = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Continue'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pluginDir = Split-Path -Parent $scriptRoot
$work = Join-Path $env:TEMP ("chorus-install-" + [guid]::NewGuid().ToString("N").Substring(0, 6))

function Say([string]$Text, [string]$Color = 'Gray') {
    Write-Host $Text -ForegroundColor $Color
}

function Ask([string]$Prompt, [string]$Default = "") {
    if ($Default) {
        $answer = Read-Host ("{0}（回车用 {1}）" -f $Prompt, $Default)
        if (-not $answer) { return $Default }
        return $answer.Trim('"').Trim()
    }
    return (Read-Host $Prompt).Trim('"').Trim()
}

function Test-Port([int]$Port) {
    return $null -ne (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Wait-Port([int]$Port, [int]$Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Find-RvcRoot {
    if ($RvcRoot -and (Test-Path (Join-Path $RvcRoot 'runtime\python.exe'))) { return (Resolve-Path $RvcRoot).Path }
    if ($env:CHORUS_RVC_ROOT -and (Test-Path (Join-Path $env:CHORUS_RVC_ROOT 'runtime\python.exe'))) {
        return (Resolve-Path $env:CHORUS_RVC_ROOT).Path
    }
    Say '正在找 RVC 整合包（含 runtime\python.exe 和 assets\weights）…' 'DarkGray'
    $roots = @()
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free -ne $null })) {
        $candidates = Get-ChildItem -LiteralPath ($drive.Root) -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match 'RVC|rvc' }
        foreach ($candidate in $candidates) {
            $roots += $candidate.FullName
            $roots += (Get-ChildItem -LiteralPath $candidate.FullName -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match 'RVC|rvc' } | ForEach-Object { $_.FullName })
        }
    }
    foreach ($root in ($roots | Select-Object -Unique)) {
        if ((Test-Path (Join-Path $root 'runtime\python.exe')) -and (Test-Path (Join-Path $root 'assets\weights'))) {
            return (Resolve-Path $root).Path
        }
    }
    return ""
}

function Find-Python {
    $candidates = @(@('py', '-3.12'), @('py', '-3.11'), @('py', '-3.10'), @('python'))
    foreach ($pair in $candidates) {
        $exe = $pair[0]
        $pre = @($pair | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $version = (& $exe @pre -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null | Select-Object -First 1)
            if ($version -match '^3\.(1[0-2])$') {
                $full = (& $exe @pre -c 'import sys;print(sys.executable)' 2>$null | Select-Object -First 1)
                if ($full) { return $full }
            }
        } catch { }
    }
    return ""
}

function Install-Deps([string]$Python, [string]$Target, [string]$Requirements, [string]$Label) {
    if (-not (Test-Path $Requirements)) {
        Say ("     找不到 {0}，跳过 {1}" -f $Requirements, $Label) 'Yellow'
        return $false
    }
    Say ("     安装 {0} 依赖到 {1} …（第一次会久一点）" -f $Label, $Target) 'DarkGray'
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    & $Python -m pip install --disable-pip-version-check --no-warn-script-location --upgrade --target $Target -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Say ("     {0} 依赖安装失败，退出码 {1}" -f $Label, $LASTEXITCODE) 'Red'
        return $false
    }
    return $true
}

function Ensure-Chorus([string]$Root) {
    $candidates = @()
    if ($ChorusDir) { $candidates += $ChorusDir }
    if ($env:CHORUS_DIR) { $candidates += $env:CHORUS_DIR }
    $candidates += (Join-Path (Split-Path -Parent $Root) 'chorus-mw')
    $candidates += (Join-Path $pluginDir 'chorus-mw')
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path (Join-Path $candidate 'app.py'))) {
            return (Resolve-Path $candidate).Path
        }
    }
    Say '没找到 chorus-mw 中间层。' 'Yellow'
    $target = Ask '请输入你要放中间层的目录（回车默认和 RVC 整合包并排）' (Join-Path (Split-Path -Parent $Root) 'chorus-mw')
    if (Test-Path (Join-Path $target 'app.py')) { return (Resolve-Path $target).Path }

    Say '准备从 GitHub 下载 chorus-mw …' 'DarkGray'
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $zip = Join-Path $work 'chorus-mw.zip'
    # 钉住验证过的版本；拿不到才退 main，并明确告警
    $tag = 'v0.9.1'
    $urls = @(
        ('https://codeload.github.com/void-HaoNan/chorus-mw/zip/refs/tags/{0}' -f $tag),
        'https://codeload.github.com/void-HaoNan/chorus-mw/zip/refs/heads/main'
    )
    $ok = $false
    $picked = ''
    foreach ($url in $urls) {
        try {
            Say ("     下载 {0}" -f $url) 'DarkGray'
            curl.exe -L --fail --max-time 240 -o $zip $url | Out-Null
            if ((Test-Path $zip) -and ((Get-Item $zip).Length -gt 10000)) { $ok = $true; $picked = $url; break }
        } catch { }
    }
    if (-not $ok) {
        Say '     下载失败：网络连不上 GitHub。你可以手动下载 chorus-mw 解压到上面那个目录再重跑本脚本。' 'Red'
        return ""
    }
    if ($picked -notmatch [regex]::Escape($tag)) {
        Say ('     注意：没拿到 {0}，这次装的是 main 分支，版本可能比文档新。' -f $tag) 'Yellow'
    }
    try {
        Expand-Archive -LiteralPath $zip -DestinationPath $work -Force
    } catch {
        Say ('     压缩包损坏或不完整：{0}' -f $_.Exception.Message) 'Red'
        return ""
    }
    $inner = Get-ChildItem $work -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'app.py') } | Select-Object -First 1
    if (-not $inner) {
        Say '     压缩包里没找到 app.py，解压失败。' 'Red'
        return ""
    }
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item $inner.FullName $target
    return (Resolve-Path $target).Path
}

function Ensure-Rubberband([string]$Chorus) {
    $bin = Join-Path $Chorus 'bin'
    $exe = Join-Path $bin 'rubberband.exe'
    if (Test-Path $exe) { return $true }
    Say '没找到 rubberband，尝试下载（升降调要用）…' 'DarkGray'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $zip = Join-Path $work 'rubberband.zip'
    $urls = @(
        'https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-windows.zip'
    )
    foreach ($url in $urls) {
        try {
            curl.exe -L --fail --max-time 120 -o $zip $url | Out-Null
            if ((Test-Path $zip) -and ((Get-Item $zip).Length -gt 10000)) { break }
        } catch { }
    }
    if (-not (Test-Path $zip)) { return $false }
    Expand-Archive -LiteralPath $zip -DestinationPath $work -Force
    $found = Get-ChildItem $work -Recurse -Filter 'rubberband.exe' | Select-Object -First 1
    $dll = Get-ChildItem $work -Recurse -Filter 'sndfile.dll' | Select-Object -First 1
    if (-not $found) { return $false }
    Copy-Item $found.FullName $exe -Force
    if ($dll) { Copy-Item $dll.FullName (Join-Path $bin 'sndfile.dll') -Force }
    return (Test-Path $exe)
}

function Set-PluginBackend([string]$DataDir, [string]$Url) {
    $configDir = Join-Path $DataDir 'config'
    $configPath = Join-Path $configDir 'astrbot_plugin_chorus_cover_config.json'
    if (-not (Test-Path $configPath)) {
        Say '     插件还没生成配置文件，先启动一次 AstrBot 再来跑本脚本，或手动在设置页填后端地址。' 'Yellow'
        return $false
    }
    $raw = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
    $json = $raw | ConvertFrom-Json
    $json.rvc_base_url = $Url
    ($json | ConvertTo-Json -Depth 20) | Set-Content -LiteralPath $configPath -Encoding UTF8
    return $true
}

Say '============================================' 'Cyan'
Say ' RVC 翻唱插件 · 一键安装器 0.13.2' 'Cyan'
Say '============================================' 'Cyan'
Say ''

# 1) AstrBot 数据目录
Say '[1/7] 找 AstrBot 数据目录' 'White'
$dataCandidates = @()
if ($AstrBotData) { $dataCandidates += $AstrBotData }
if ($env:ASTRBOT_DATA_DIR) { $dataCandidates += $env:ASTRBOT_DATA_DIR }
$dataCandidates += (Join-Path $env:USERPROFILE 'data')
foreach ($candidate in $dataCandidates) {
    if ($candidate -and (Test-Path (Join-Path $candidate 'config'))) {
        $AstrBotData = (Resolve-Path $candidate).Path
        break
    }
}
if (-not $AstrBotData -or -not (Test-Path (Join-Path $AstrBotData 'config'))) {
    Say '     没自动找到。AstrBot 的数据目录一般在 用户目录\data。' 'Yellow'
    $AstrBotData = Ask '请输入 AstrBot 数据目录' (Join-Path $env:USERPROFILE 'data')
}
Say ("     数据目录：{0}" -f $AstrBotData) 'Green'

# 2) RVC 整合包
Say '[2/7] 找 RVC 整合包' 'White'
$root = Find-RvcRoot
if (-not $root) {
    Say '     没找到。请把 RVC 整合包的根目录贴进来（里面有 runtime 和 assets 两个文件夹）。' 'Yellow'
    $root = Ask 'RVC 整合包目录'
    if (-not (Test-Path (Join-Path $root 'runtime\python.exe'))) {
        Say '     这个目录里没有 runtime\python.exe，请确认是整合包根目录后重跑。' 'Red'
        Read-Host '按回车退出'
        exit 1
    }
}
Say ("     整合包：{0}" -f $root) 'Green'

# 3) 中间层
Say '[3/7] 检查中间层 chorus-mw' 'White'
$chorus = Ensure-Chorus $root
if (-not $chorus) {
    Say '     中间层没准备好，先停下来。下载好 chorus-mw 解压后重跑即可。' 'Red'
    Read-Host '按回车退出'
    exit 1
}
Say ("     中间层：{0}" -f $chorus) 'Green'

if ($CheckOnly) {
    $pythonPreview = Find-Python
    Say ''
    Say '检查模式：只报告，不安装、不写配置、不启动。' 'Cyan'
    Say ("  AstrBot 数据目录：{0}" -f $AstrBotData)
    Say ("  RVC 整合包：{0}" -f $root)
    Say ("  中间层：{0}" -f $chorus)
    Say ("  Python：{0}" -f $(if ($pythonPreview) { $pythonPreview } else { '未找到 3.10~3.12' }))
    Say ("  rubberband：{0}" -f $(if (Test-Path (Join-Path $chorus 'bin\rubberband.exe')) { '已就绪' } else { '缺，需下载' }))
    Say ("  3333：{0}" -f $(if (Test-Port 3333) { '在跑' } else { '没跑' }))
    Read-Host '按回车关闭窗口'
    exit 0
}

# 4) Python 与依赖
Say '[4/7] 检查 Python 和依赖' 'White'
$python = Find-Python
if (-not $python) {
    Say '     没找到 Python 3.10~3.12。请先安装 Python（勾选 Add to PATH），再重跑本脚本。' 'Red'
    Start-Process 'https://www.python.org/downloads/windows/'
    Read-Host '按回车退出'
    exit 1
}
Say ("     Python：{0}" -f $python) 'Green'
$webOk = Install-Deps $python (Join-Path $chorus 'libs_web') (Join-Path $chorus 'requirements.txt') '服务端'
$workerReq = Join-Path $chorus 'requirements-worker.txt'
if (Test-Path $workerReq) {
    Install-Deps $python (Join-Path $chorus 'libs') $workerReq '变声 worker' | Out-Null
}

# 5) rubberband
Say '[5/7] 检查升降调组件' 'White'
if (Ensure-Rubberband $chorus) {
    Say '     rubberband 就绪' 'Green'
} else {
    Say '     没装上 rubberband：升降调会不可用，翻唱本身不受影响。' 'Yellow'
}

# 6) 写插件后端地址
Say '[6/7] 写插件配置' 'White'
$url = 'http://127.0.0.1:3333/'
if (Set-PluginBackend $AstrBotData $url) {
    Say ("     已把 rvc_base_url 设为 {0}" -f $url) 'Green'
} else {
    Say '     这一步跳过，稍后在插件设置里把 RVC 后端填成 http://127.0.0.1:3333/' 'Yellow'
}

# 7) 启动中间层
Say '[7/7] 启动中间层' 'White'
if (Test-Port 3333) {
    Say '     3333 已经在跑，跳过启动' 'Green'
} else {
    $env:CHORUS_RVC_ROOT = $root
    $env:CHORUS_PYTHON = $python
    $env:NO_PROXY = '127.0.0.1,localhost,::1'
    $env:no_proxy = $env:NO_PROXY
    $starter = Join-Path $chorus 'scripts\start.ps1'
    if (Test-Path $starter) {
        Start-Process powershell -ArgumentList '-ExecutionPolicy', 'RemoteSigned', '-File', $starter
    } else {
        Say '     中间层里没有 scripts\start.ps1，手动跑 python app.py 也行。' 'Yellow'
    }
    if (Wait-Port 3333 180) {
        Say '     3333 已就绪' 'Green'
    } else {
        Say '     180 秒还没起来，去看中间层目录下 _work\mw.log 的报错。' 'Yellow'
    }
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue

Say ''
Say '============================================' 'Cyan'
Say ' 安装流程结束' 'Cyan'
Say '============================================' 'Cyan'
Say '接下来：' 'White'
Say '  1. 重启一次 AstrBot（让插件读到新的后端地址）'
Say '  2. 在群里发 /环境自检 看还有没有红叉'
Say '  3. 把音色模型 .pth 放进 RVC 的 assets\weights，发 /刷新rvc模型'
Say ''
Read-Host '按回车关闭窗口'
