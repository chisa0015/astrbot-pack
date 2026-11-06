# AstrBot 整合包一键安装
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -AstrBotDir "D:\AstrBot"
#
# 参数：
#   -AstrBotDir    AstrBot 安装根目录（含 main.py 和 data\ 的那一层）
#   -NoBackup      跳过备份（默认会备份）

param(
    [Parameter(Mandatory = $false)]
    [string]$AstrBotDir = "",
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "AstrBot 整合包安装器" -ForegroundColor Magenta
Write-Host "=====================" -ForegroundColor Magenta
Write-Host ""

# 1) 确定 AstrBot 目录
if ([string]::IsNullOrWhiteSpace($AstrBotDir)) {
    Write-Step "请指定 AstrBot 安装目录"
    Write-Host "    （就是包含 main.py 和 data 文件夹的那一层）"
    $AstrBotDir = Read-Host "路径"
}

if (-not (Test-Path $AstrBotDir)) {
    Write-Host "目录不存在：$AstrBotDir" -ForegroundColor Red
    exit 1
}

$AstrBotDir = (Resolve-Path $AstrBotDir).Path
$dataDir = Join-Path $AstrBotDir "data"
$pluginsDir = Join-Path $dataDir "plugins"
$personaDir = Join-Path $dataDir "persona"

Write-Ok "AstrBot: $AstrBotDir"

# 2) 检查像不像 AstrBot
$looksLike = $false
if (Test-Path (Join-Path $AstrBotDir "main.py")) { $looksLike = $true }
if (Test-Path $dataDir) { $looksLike = $true }
if (-not $looksLike) {
    Write-Warn2 "这个目录里没有 main.py，也没有 data 文件夹"
    $ans = Read-Host "确定继续？(y/N)"
    if ($ans -ne "y") { exit 0 }
}

# 3) 备份
if (-not $NoBackup) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDir = Join-Path $AstrBotDir "_backup_astrbotpack_$stamp"
    Write-Step "备份到 $backupDir"
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    if (Test-Path $pluginsDir) {
        Copy-Item $pluginsDir (Join-Path $backupDir "plugins") -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "plugins 已备份"
    }
    if (Test-Path $personaDir) {
        Copy-Item $personaDir (Join-Path $backupDir "persona") -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "persona 已备份"
    }
}

# 4) 复制插件
$srcPlugins = Join-Path $here "plugins"
if (Test-Path $srcPlugins) {
    if (-not (Test-Path $pluginsDir)) { New-Item -ItemType Directory -Path $pluginsDir -Force | Out-Null }
    Write-Step "安装插件"
    Get-ChildItem $srcPlugins -Directory | ForEach-Object {
        $dst = Join-Path $pluginsDir $_.Name
        if (Test-Path $dst) {
            Write-Warn2 "$($_.Name) 已存在，跳过（如需覆盖请先手动删除）"
        } else {
            Copy-Item $_.FullName $dst -Recurse -Force
            Write-Ok $_.Name
        }
    }
} else {
    Write-Warn2 "包里没有 plugins 目录"
}

# 5) 复制人格
$srcPersona = Join-Path $here "personas"
if (Test-Path $srcPersona) {
    if (-not (Test-Path $personaDir)) { New-Item -ItemType Directory -Path $personaDir -Force | Out-Null }
    Write-Step "安装人格"
    Get-ChildItem $srcPersona -Directory | ForEach-Object {
        $dst = Join-Path $personaDir $_.Name
        if (Test-Path $dst) {
            Write-Warn2 "$($_.Name) 已存在，跳过"
        } else {
            Copy-Item $_.FullName $dst -Recurse -Force
            Write-Ok $_.Name
        }
    }
} else {
    Write-Warn2 "包里没有 personas 目录"
}

# 6) 提示后续
Write-Host ""
Write-Host "安装完成" -ForegroundColor Green
Write-Host "-----------------------" -ForegroundColor Green
Write-Host "1. 重启 AstrBot（关掉进程再启动）"
Write-Host "2. 打开面板 → 插件 → 确认 3 个插件已加载"
Write-Host "3. 面板 → 配置 → 人格 → 启用「色情鲸鱼」"
Write-Host "4. 如果插件报缺依赖，按各插件 README 安装（bitchfinder 需要 playwright）"
Write-Host ""
if (-not $NoBackup) {
    Write-Host "备份目录：$backupDir" -ForegroundColor DarkGray
}
