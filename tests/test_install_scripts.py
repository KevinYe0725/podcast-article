"""安装脚本的基本约束（这几个坑都是实测踩出来的，写成测试免得以后再踩）。

为什么值得单独立一个测试文件：Windows 那份脚本我**没法在这台 Mac 上真跑**（本机没装
PowerShell），能自动守住的就只有编码与语法这类硬约束 —— 一旦丢了 UTF-8 BOM，
中文版 Windows 的 PowerShell 5.1 会把脚本按 GBK 解码，输出全是乱码甚至语法错。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def test_install_sh_syntax_is_valid():
    r = subprocess.run(["bash", "-n", str(SCRIPTS / "install.sh")], capture_output=True, text=True)
    assert r.returncode == 0, f"install.sh 语法错误：{r.stderr}"


def test_install_sh_is_executable():
    mode = (SCRIPTS / "install.sh").stat().st_mode
    assert mode & 0o111, "install.sh 要有可执行位（git 只跟踪这一个权限位）"


def test_install_sh_handles_all_three_platforms():
    s = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    for tool in ("apt-get", "dnf", "brew", "pacman"):        # 包管理器分支
        assert tool in s, f"install.sh 里没有 {tool} 分支"
    for flag in ("--check", "--no-ffmpeg", "--cn"):
        assert flag in s, f"install.sh 缺少 {flag} 参数"
    assert "PA_GH_PROXY" in s, "GitHub 连不上时的代理退路不能删"
    assert "extra faster" in s, "非 Apple Silicon 平台要装 faster-whisper"


def test_install_sh_does_not_swallow_clone_failure():
    """`git clone ... && ok ...` 在 set -e 下不会因为 clone 失败而退出（实测踩过：
    服务器上 GitHub 连不上，脚本带着空目录继续跑到第 4 步才炸）。必须显式判断。"""
    s = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
    assert "fetched=0" in s and "if [ \"$fetched\" = 0 ]" in s, "clone 失败必须显式处理并退出"


def test_install_ps1_has_utf8_bom():
    """PowerShell 5.1（Windows 自带）读「无 BOM 的 UTF-8 脚本」会按 ANSI/GBK 解码 ——
    脚本里全是中文提示，丢了 BOM 就是一堆乱码。"""
    raw = (SCRIPTS / "install.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1 必须带 UTF-8 BOM（PowerShell 5.1 才认）"


def test_install_ps1_writes_env_without_bom():
    """反过来：写 .env 时**不能**带 BOM（Set-Content -Encoding UTF8 在 PS 5.1 会加 BOM，
    那会让 .env 第一行变成一个乱码键）。"""
    s = (SCRIPTS / "install.ps1").read_text(encoding="utf-8-sig")
    assert "UTF8Encoding($false)" in s, "写 .env 要用不带 BOM 的编码"
    assert "Set-Content -Path $EnvFile -Value $text -Encoding UTF8" not in s, \
        "不要用会写 BOM 的 Set-Content -Encoding UTF8 覆写 .env"


def test_install_bat_is_ascii_only():
    """cmd 在中文 Windows 上按 GBK 解析批处理文件，UTF-8 中文注释会变乱码。"""
    raw = (SCRIPTS / "install.bat").read_bytes()
    raw.decode("ascii")          # 不能抛 UnicodeDecodeError
    assert b"install.ps1" in raw and b"ExecutionPolicy Bypass" in raw


def test_readme_documents_the_installer():
    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README.en.md").read_text(encoding="utf-8")
    for text in (zh, en):
        for needle in ("scripts/install.sh", "install.ps1", "install.bat", "--cn", "PA_GH_PROXY"):
            assert needle in text, f"README 里缺少 {needle}（一键安装的用法要写全，含 Windows 与国内网络）"


def test_dev_script_restarts_cleanly():
    """scripts/dev.sh 守的是「代码是新的、跑着的进程是旧的」这个坑（实测踩过：
    本机跑了 4 小时前的进程，/api/config 404、文章里的时间区间点不动）。"""
    p = SCRIPTS / "dev.sh"
    assert p.stat().st_mode & 0o111, "dev.sh 要有可执行位"
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"dev.sh 语法错误：{r.stderr}"
    s = p.read_text(encoding="utf-8")
    assert "webapp.py" in s, "只该杀本项目的进程"
    assert "不是本项目的 webapp.py" in s, "占端口的是别人的进程时要拒绝，不能乱杀"
    assert "/api/config" in s, "用新端点的返回值判断就绪（能顺带确认进程是新版）"


def test_server_deployment_enables_cloud_asr_without_tracking_secrets():
    service = (ROOT / "deploy/podcast-article.service").read_text(encoding="utf-8")
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    caddy = (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    readme = (ROOT / "deploy/README.md").read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/podcast-article/server.env" in service
    assert "User=podcast" in service
    assert "--host 172.17.0.1 --port 8788" in service
    assert "PA_READONLY=0" in service
    assert "PA_ASR_BACKEND=cloud" in service
    assert "DASHSCOPE_API_KEY=" not in service
    assert "OSS_ACCESS_KEY_SECRET=" not in service
    assert "podcast.squareconf.cn" in caddy
    assert "basic_auth" in caddy
    assert "reverse_proxy {$PA_APP_UPSTREAM:127.0.0.1:8788}" in caddy
    assert "podcast.squareconf.cn" in readme
    assert "PA_ASR_BACKEND=cloud" in readme
    assert "server.env" in readme
    assert "PA_READONLY" in compose


def test_ui_runner_isolates_knowledge_database():
    script = (ROOT / "tests/ui/run.sh").read_text(encoding="utf-8")
    assert 'export PA_KB_FILE="$TMP/kb.sqlite"' in script
    assert 'PA_KB_FILE="$PA_KB_FILE"' in script
