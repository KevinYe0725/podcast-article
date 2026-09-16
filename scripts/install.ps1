# podcast-article 一键安装（Windows）
#
# 用法（在 PowerShell 里，或直接双击 install.bat）：
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Dir D:\pa -Check
#   $env:PA_DEEPSEEK_KEY="sk-..."; .\scripts\install.ps1
#
# 做的事情和 macOS/Linux 那份一样：装 uv → 装 Python 3.12 → 取代码 → 装依赖
# （Windows 上转写后端是 faster-whisper，走 CPU）→ 检查 ffmpeg → 写 .env → 冒烟测试。

param(
  [string]$Dir = "$HOME\podcast-article",
  [switch]$Check,
  [switch]$NoFfmpeg
)

$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:PA_REPO_URL) { $env:PA_REPO_URL } else { "https://github.com/KevinYe0725/podcast-article.git" }
$Key = if ($env:PA_DEEPSEEK_KEY) { $env:PA_DEEPSEEK_KEY.Trim() } else { "" }

function Step($m) { Write-Host "▸ $m" -ForegroundColor White }
function Ok($m)   { Write-Host "  ✓ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ⚠ $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  ✗ $m" -ForegroundColor Red; exit 1 }

Write-Host "podcast-article 安装" -ForegroundColor White
Write-Host "  系统：Windows $([System.Environment]::OSVersion.Version)"
Write-Host "  目录：$Dir$(if ($Check) { '   [只检查模式]' })"
Write-Host ""

# ---------------------------------------------------------------- 1. uv
Step "1/7 检查 uv（Python 运行时管理器）"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  Ok "已有 uv $((uv --version) -split ' ' | Select-Object -Index 1)"
} elseif ($Check) {
  Warn "没有 uv —— 安装时会用官方脚本装（astral.sh）"
} else {
  Step "  用官方脚本装 uv"
  try {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  } catch {
    Die "uv 安装失败（网络？）。也可以手动装：winget install --id astral-sh.uv -e，或 scoop install uv"
  }
  # 官方脚本装到 %USERPROFILE%\.local\bin，当前会话要补 PATH
  $uvBin = Join-Path $HOME ".local\bin"
  if (Test-Path $uvBin) { $env:Path = "$uvBin;$env:Path" }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die "uv 装完还是找不到，请重开一个 PowerShell 再跑一次" }
  Ok "uv 已就绪"
}

# ------------------------------------------------------------ 2. Python
Step "2/7 准备 Python 3.12"
if ($Check) {
  Warn "跳过（实际安装时由 uv 下载，不依赖系统里的 Python）"
} else {
  uv python install 3.12 | Out-Null
  Ok "Python 3.12 就绪（uv 自己管的，不动系统 Python）"
}

# -------------------------------------------------------------- 3. 代码
Step "3/7 取代码"
$Here = Split-Path -Parent $PSCommandPath
$LocalRepo = Join-Path (Split-Path -Parent $Here) "pyproject.toml"
if (Test-Path $LocalRepo) {
  $Dir = Split-Path -Parent $Here
  Ok "就在当前仓库里：$Dir"
} elseif (Test-Path (Join-Path $Dir ".git")) {
  if (-not $Check) { git -C $Dir pull --ff-only 2>$null | Out-Null }
  Ok "已有仓库：$Dir"
} elseif (Test-Path $Dir) {
  Die "$Dir 已存在且不是 git 仓库，请换个目录（-Dir）或先挪走"
} elseif ($Check) {
  Warn "还没有代码，安装时会 clone 到 $Dir"
} else {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "需要 git：winget install --id Git.Git -e" }
  git clone --depth 1 $RepoUrl $Dir | Out-Null
  Ok "已 clone 到 $Dir"
}

# ----------------------------------------------------------- 4. 依赖
Step "4/7 装依赖（uv sync，含 faster-whisper）"
if ($Check) {
  Warn "跳过（实际会装 flask / markdown / openai / yt-dlp / feedparser / faster-whisper …）"
} else {
  Push-Location $Dir
  uv sync --python 3.12 --extra faster
  if ($LASTEXITCODE -ne 0) { Pop-Location; Die "依赖安装失败（网络？可以试试设置镜像：`$env:UV_INDEX_URL='https://mirrors.aliyun.com/pypi/simple/'）" }
  Ok "依赖装好（转写后端 faster-whisper，走 CPU）"
  Pop-Location
}

# ---------------------------------------------------------- 5. ffmpeg
Step "5/7 检查 ffmpeg"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
  Ok "已有 ffmpeg"
} elseif ($Check -or $NoFfmpeg) {
  Warn "没有 ffmpeg —— 播客音频直链不受影响，YouTube / B站 视频会失败"
} else {
  Warn "没有 ffmpeg，尝试自动安装"
  $installed = $false
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements --silent
    $installed = $?
  }
  if (-not $installed -and (Get-Command choco -ErrorAction SilentlyContinue)) { choco install ffmpeg -y; $installed = $? }
  if (-not $installed -and (Get-Command scoop -ErrorAction SilentlyContinue)) { scoop install ffmpeg; $installed = $? }
  # 装了但当前会话 PATH 还没刷新，按常见位置补一下
  foreach ($p in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links", "C:\ProgramData\chocolatey\bin")) {
    if (Test-Path $p) { $env:Path = "$p;$env:Path" }
  }
  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Ok "ffmpeg 装好了" }
  else { Warn "ffmpeg 没装上（或需要重开终端）—— 只影响视频源" }
}

# ------------------------------------------------------------- 6. .env
Step "6/7 配置 .env"
$EnvFile = Join-Path $Dir ".env"
if ($Check) {
  if (Test-Path $EnvFile) { Ok "已有 .env" } else { Warn "还没有 .env" }
} else {
  if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Dir ".env.example") $EnvFile }
  if (-not $Key) {
    Write-Host "  粘贴 DeepSeek API key（https://platform.deepseek.com 创建，直接回车跳过）：" -NoNewline
    $Key = (Read-Host).Trim()
  }
  $text = Get-Content $EnvFile -Raw -Encoding UTF8
  if ($Key) {
    # .env.example 里的占位 key 是未注释的，必须替换掉，否则会让人以为配好了
    $text = [regex]::Replace($text, "(?m)^DEEPSEEK_API_KEY=.*$", "DEEPSEEK_API_KEY=$Key")
    Ok "API key 已写入 .env"
  } else {
    $text = [regex]::Replace($text, "(?m)^DEEPSEEK_API_KEY=.*$", "# DEEPSEEK_API_KEY=（还没填：Web 界面 → 设置 → API 密钥）")
    Warn "没填 key —— 现在就能转写文字稿，写文章前填一下即可"
  }
  # 注意：Set-Content -Encoding UTF8 在 PowerShell 5.1 里会写 BOM，
  # BOM 会让 .env 的第一行变成一个乱码键 —— 所以用不带 BOM 的写法
  [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# --------------------------------------------------------- 7. 冒烟测试
Step "7/7 冒烟测试"
if ($Check) {
  Warn "跳过"
} else {
  Push-Location $Dir
  uv run --python 3.12 python -c "import podcast_article, webapp" | Out-Null
  if ($LASTEXITCODE -ne 0) { Pop-Location; Die "模块导入失败，请看上面的报错" }
  Ok "模块导入正常"
  uv run --python 3.12 podcast-article --help | Out-Null
  Ok "命令行可用"
  Pop-Location
}

Write-Host ""
Write-Host "装好了 —— 目录：$Dir" -ForegroundColor Green
Write-Host ""
if ($Check) {
  Write-Host "  这是只检查模式，什么都没改。去掉 -Check 再跑一次就是真装。"
  exit 0
}
@"

  接下来这么用（都在 $Dir 里跑）：

    # Web 界面（推荐）：粘贴链接 → 看实时进度 → 读文章
    cd $Dir
    uv run python webapp.py
    # 然后浏览器打开 http://127.0.0.1:8787

    # 命令行：一个链接直接出文章
    uv run podcast-article "https://www.xiaoyuzhoufm.com/episode/xxxx"

  产物都在 $Dir\output\ 下（每集一个目录：文章、文字稿、音频、封面）。

  提示：Windows 上转写走 CPU（faster-whisper），100 分钟节目可能要几十分钟到一两小时；
  平台有字幕时（YouTube / B站）会直接用字幕，零转写成本。
"@ | Write-Host
