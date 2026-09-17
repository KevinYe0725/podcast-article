#!/usr/bin/env bash
# podcast-article 一键安装（macOS / Linux）
#
#   bash scripts/install.sh                     # 装到 ~/podcast-article，交互式问 API key
#   bash scripts/install.sh --dir /opt/pa       # 指定安装目录
#   bash scripts/install.sh --check             # 只检查环境，不装任何东西
#   PA_DEEPSEEK_KEY=sk-xxx bash scripts/install.sh   # 非交互（CI / 批量装）
#
# 这个脚本会做七件事：
#   1. 找到或装上 uv（Astral 的 Python 包与解释器管理器）
#   2. 用 uv 装一个 Python 3.12（不需要系统里已经有合适的 Python）
#   3. 取到代码：已经在仓库里就用当前目录，否则 clone
#   4. uv sync 装依赖。macOS Apple Silicon 上默认是 mlx-whisper（走 GPU，约 38x 实时）；
#      其它平台装 faster-whisper（CPU，慢很多）
#   5. 检查 ffmpeg（视频源需要它合并音轨；播客音频直链不需要）
#   6. 写 .env，问一次 DeepSeek API key（也可以留空，之后在 Web 界面里填）
#   7. 冒烟测试：导入模块 + 跑一次 CLI --help
set -euo pipefail

REPO_URL="${PA_REPO_URL:-https://github.com/KevinYe0725/podcast-article.git}"
INSTALL_DIR="${PA_INSTALL_DIR:-$HOME/podcast-article}"
CHECK_ONLY=0
WANT_FFMPEG=1
KEY="${PA_DEEPSEEK_KEY:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2;;
    --check) CHECK_ONLY=1; shift;;
    --no-ffmpeg) WANT_FFMPEG=0; shift;;
    --cn) PA_CN=1; shift;;          # 走国内镜像（PyPI 直连慢的时候用）
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2;;
  esac
done

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi
step() { printf '%s▸ %s%s\n' "$B" "$1" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s⚠%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

OS="$(uname -s)"
ARCH="$(uname -m)"
APPLE_SILICON=0
[ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ] && APPLE_SILICON=1

echo "${B}podcast-article 安装${N}"
echo "  系统：$OS $ARCH$([ "$APPLE_SILICON" = 1 ] && echo '（Apple Silicon，转写会走 GPU）' || echo '（转写走 CPU，慢很多）')"
echo "  目录：$INSTALL_DIR$([ "$CHECK_ONLY" = 1 ] && echo '   [只检查模式]')"
echo

# ---------------------------------------------------------------- 1. uv
step "1/7 检查 uv（Python 运行时管理器）"
if command -v uv >/dev/null 2>&1; then
  ok "已有 uv $(uv --version 2>/dev/null | awk '{print $2}')"
elif [ "$CHECK_ONLY" = 1 ]; then
  warn "没有 uv —— 安装时会自动装（macOS 走 brew，其它平台走官方脚本）"
else
  # 三条路依次试：Homebrew → 官方脚本 → pip 走镜像。
  # 国内网络下 astral.sh 经常很慢甚至连不上，所以必须有退路，不能让安装卡死在这一步。
  export PATH="$HOME/.local/bin:$PATH"
  if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    step "  用 Homebrew 装 uv"
    brew install uv || warn "brew 装 uv 失败，换官方脚本"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    step "  用官方脚本装 uv（https://astral.sh/uv）"
    curl -LsSf --max-time 120 https://astral.sh/uv/install.sh | sh || warn "官方脚本失败，换 pip 装（走阿里云镜像）"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    step "  用 pip 装 uv（阿里云 PyPI 镜像）"
    if ! python3 -m pip --version >/dev/null 2>&1; then
      SUDO_PIP=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO_PIP="sudo"
      if command -v apt-get >/dev/null 2>&1; then $SUDO_PIP apt-get install -y -qq python3-pip
      elif command -v dnf >/dev/null 2>&1; then $SUDO_PIP dnf install -y -q python3-pip
      elif command -v yum >/dev/null 2>&1; then $SUDO_PIP yum install -y -q python3-pip
      fi
    fi
    python3 -m pip install --user -q -i https://mirrors.aliyun.com/pypi/simple/ uv \
      || PYTHONPATH="" python3 -m pip install --user -q uv \
      || warn "pip 装 uv 也失败了"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv 装不上。手动装一个再跑本脚本：brew install uv ／ pip install uv ／ curl -LsSf https://astral.sh/uv/install.sh | sh"
  ok "uv 已就绪：$(uv --version)"
fi

# ------------------------------------------------------------ 2. Python
step "2/7 准备 Python 3.12"
if [ "$CHECK_ONLY" = 1 ]; then
  uv python find 3.12 >/dev/null 2>&1 && ok "已有 3.12" || warn "需要下载 Python 3.12（约 30MB）"
else
  uv python install 3.12 >/dev/null && ok "Python 3.12 就绪（uv 自己管的，不污染系统）"
fi

# -------------------------------------------------------------- 3. 代码
step "3/7 取代码"
if [ -f "$(dirname "$0")/../pyproject.toml" ]; then
  INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
  ok "就在当前仓库里：$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
  if [ "$CHECK_ONLY" = 0 ]; then git -C "$INSTALL_DIR" pull --ff-only >/dev/null 2>&1 || warn "git pull 失败（本地有改动？），继续用现有代码"; fi
  ok "已有仓库，顺手更新：$INSTALL_DIR"
elif [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR/.git" ]; then
  die "$INSTALL_DIR 已存在且不是 git 仓库，请换个目录（--dir）或先挪走"
elif [ "$CHECK_ONLY" = 1 ]; then
  warn "还没有代码，安装时会 clone 到 $INSTALL_DIR"
else
  command -v git >/dev/null 2>&1 || die "需要 git：macOS 装 Xcode 命令行工具，Linux 用包管理器装 git（apt/dnf install git）"
  # 注意：`git clone ... && ok ...` 这种写法在 set -e 下**不会**因为 clone 失败而退出
  # （&& 列表里非末尾的命令失败不触发 errexit）——国内网络下 GitHub 经常连不上，
  # 必须显式判断，否则会带着空目录继续往下跑（实测踩过）。
  fetched=0
  if git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1; then
    ok "已 clone 到 $INSTALL_DIR"
    fetched=1
  else
    warn "直接 clone GitHub 失败（国内网络常见），换压缩包方式再试一次"
    TARBALL="https://codeload.github.com/KevinYe0725/podcast-article/tar.gz/refs/heads/main"
    [ -n "${PA_GH_PROXY:-}" ] && TARBALL="${PA_GH_PROXY%/}/$TARBALL"
    mkdir -p "$INSTALL_DIR"
    if curl -LsSf --max-time 300 "$TARBALL" | tar xz -C "$INSTALL_DIR" --strip-components=1 2>/dev/null; then
      ok "已用压缩包取到代码（$([ -n "${PA_GH_PROXY:-}" ] && echo '走代理' || echo '直连 codeload')）"
      fetched=1
    fi
  fi
  if [ "$fetched" = 0 ]; then
    cat >&2 <<'MSG'

  ✗ 代码没取下来。三个办法，任选一个：

    1) 开代理再来一次（脚本支持 GitHub 代理前缀）：
         PA_GH_PROXY=https://ghproxy.net/ bash scripts/install.sh
         （代理地址换成你能用的；也可以只设 git 的代理：git config --global http.proxy http://127.0.0.1:7890）

    2) 手动下载：浏览器打开 https://github.com/KevinYe0725/podcast-article → Code → Download ZIP
       解压后进到那个目录，再跑一次：
         bash scripts/install.sh

    3) 用 Gitee 等国内镜像：把自己的 fork 地址给脚本
         PA_REPO_URL=https://gitee.com/你的名字/podcast-article.git bash scripts/install.sh

MSG
    exit 1
  fi
fi

# ----------------------------------------------------------- 4. 依赖
step "4/7 装依赖（uv sync）"
if [ "${PA_CN:-0}" = 1 ]; then
  export UV_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/"
  export UV_DEFAULT_INDEX="$UV_INDEX_URL"
  ok "已切到阿里云 PyPI 镜像"
fi
if [ "$CHECK_ONLY" = 1 ]; then
  warn "跳过（实际安装时会装：flask / markdown / openai / yt-dlp / feedparser …$([ "$APPLE_SILICON" = 1 ] && echo ' + mlx-whisper' || echo ' + faster-whisper'))"
else
  cd "$INSTALL_DIR"
  EXTRA=""; [ "$APPLE_SILICON" = 1 ] || EXTRA="--extra faster"
  if uv sync --python 3.12 $EXTRA >/dev/null; then
    ok "依赖装好（含 $([ "$APPLE_SILICON" = 1 ] && echo 'mlx-whisper，转写走 Apple GPU' || echo 'faster-whisper，CPU 转写')）"
  else
    warn "uv sync 失败，改用国内镜像重试一次"
    UV_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/" UV_DEFAULT_INDEX="https://mirrors.aliyun.com/pypi/simple/" \
      uv sync --python 3.12 $EXTRA >/dev/null \
      || die "依赖装不上（网络？手动重试：uv sync $EXTRA ／ 或加 --cn 走阿里云镜像）"
    ok "依赖装好（走了阿里云镜像）"
  fi
fi

# ---------------------------------------------------------- 5. ffmpeg
step "5/7 检查 ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
  ok "已有 ffmpeg（视频源合并音轨要用）"
elif [ "$CHECK_ONLY" = 1 ] || [ "$WANT_FFMPEG" = 0 ]; then
  warn "没有 ffmpeg —— 播客音频直链（小宇宙 / RSS）不受影响，YouTube / B站 视频会失败"
else
  warn "没有 ffmpeg，尝试自动安装（视频源需要它合并音轨）"
  SUDO=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  set +e
  if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  elif command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q ffmpeg
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q ffmpeg
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm ffmpeg
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper -q install -y ffmpeg
  else
    warn "认不出包管理器，请手动装 ffmpeg"
  fi
  set -e
  command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg 装好了" || warn "ffmpeg 没装上 —— 不影响播客音频直链，只影响视频源"
fi

# ------------------------------------------------------------- 6. .env
step "6/7 配置 .env"
ENV_FILE="$INSTALL_DIR/.env"
if [ "$CHECK_ONLY" = 1 ]; then
  [ -f "$ENV_FILE" ] && ok "已有 .env" || warn "还没有 .env（安装时会创建并问你要 DeepSeek key）"
else
  cd "$INSTALL_DIR"
  if [ ! -f "$ENV_FILE" ]; then cp .env.example "$ENV_FILE" && chmod 600 "$ENV_FILE"; fi

  # 交互式问一次 key（非交互时用环境变量；都没有就留空，之后能在 Web 界面里填）
  if [ -z "$KEY" ] && [ -t 0 ]; then
    printf '  粘贴 DeepSeek API key（https://platform.deepseek.com 创建，直接回车可跳过）：'
    read -r KEY || KEY=""
  fi
  KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
  if [ -n "$KEY" ]; then
    # 注意：.env.example 里的占位 key 是**未注释**的，直接留着会让人以为配好了 —— 必须替换掉
    python3 - "$ENV_FILE" "$KEY" <<'PY' 2>/dev/null || sed -i.bak "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$KEY|" "$ENV_FILE"
import re, sys
p, key = sys.argv[1], sys.argv[2]
s = open(p, encoding="utf-8").read()
s = re.sub(r"^DEEPSEEK_API_KEY=.*$", "DEEPSEEK_API_KEY=" + key, s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
PY
    rm -f "$ENV_FILE.bak"
    ok "API key 已写入 .env（权限 600）"
  else
    python3 - "$ENV_FILE" <<'PY' 2>/dev/null || sed -i.bak "s|^DEEPSEEK_API_KEY=.*|# DEEPSEEK_API_KEY=|" "$ENV_FILE"
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r"^DEEPSEEK_API_KEY=.*$", "# DEEPSEEK_API_KEY=（还没填：Web 界面 → 设置 → API 密钥，或直接编辑 .env）", s, flags=re.M)
open(p, "w", encoding="utf-8").write(s)
PY
    rm -f "$ENV_FILE.bak"
    warn "没填 key —— 现在就能转写文字稿，写文章前在「设置 → API 密钥」里填一下即可"
  fi
fi

# --------------------------------------------------------- 7. 冒烟测试
step "7/7 冒烟测试"
if [ "$CHECK_ONLY" = 1 ]; then
  warn "跳过"
else
  cd "$INSTALL_DIR"
  uv run --python 3.12 python -c "import podcast_article, webapp" >/dev/null 2>&1 \
    && ok "模块导入正常" || die "导入失败，请看上面的报错（或在仓库里跑 uv run pytest -q 自查）"
  uv run --python 3.12 podcast-article --help >/dev/null 2>&1 && ok "命令行可用"
  ffmpeg -version >/dev/null 2>&1 && printf '' # 静默
fi

echo
if [ "$CHECK_ONLY" = 1 ]; then
  echo "${G}${B}环境检查完成${N}（没有改动任何东西）"
  echo
  if [ -f "$0" ]; then
    echo "  去掉 --check 再跑一次就是真装：bash $0"
  else
    # 一行命令（curl | bash）时没有本地文件，给回那条命令本身
    echo "  去掉 --check 再跑一次就是真装："
    echo "    curl -fsSL https://raw.githubusercontent.com/KevinYe0725/podcast-article/main/scripts/install.sh | bash"
  fi
  exit 0
fi
echo "${G}${B}装好了${N} —— 目录：$INSTALL_DIR"
echo
cat <<EOF
  接下来这么用（都在 $INSTALL_DIR 里跑）：

    ${B}# Web 界面（推荐）：粘贴链接 → 看实时进度 → 读文章${N}
    cd $INSTALL_DIR && uv run python webapp.py
    # 然后浏览器打开 http://127.0.0.1:8787

    ${B}# 命令行：一个链接直接出文章${N}
    uv run podcast-article 'https://www.xiaoyuzhoufm.com/episode/xxxx'

    ${B}# 手机上也想看？让局域网能访问（同一 Wi-Fi 下用 Mac 的 IP:8787）${N}
    uv run python webapp.py --host 0.0.0.0

  产物都在 $INSTALL_DIR/output/ 下（每集一个目录：文章、文字稿、音频、封面）。
  $([ "$APPLE_SILICON" = 1 ] && echo '首次转写会下载 whisper 模型（约 1.5GB），之后就快了。' || echo '提示：这台机器没有 Apple GPU，CPU 转写 100 分钟节目要很久 —— 建议用平台字幕，或换 Mac 跑转写。')
EOF
