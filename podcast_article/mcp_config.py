"""MCP 服务器配置的读写。

配置存于 PA_DATA_ROOT/mcp_servers.json（权限 0600，仅管理员接口可访问）。
env 的值支持 ${VAR} 引用 .env 里的变量，避免把 token 复制两份。

界面上以「预设」为主：常见服务器点一下就配好，自定义才需要填命令。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = ROOT / "mcp_servers.json"
# Preserve patch points used by older callers while the deployed config now lives
# under PA_DATA_ROOT (or PA_MCP_CONFIG when an operator needs an explicit path).
CONFIG_PATH = _DEFAULT_CONFIG_PATH
SERVERS_PATH = _DEFAULT_CONFIG_PATH


def _config_path() -> Path:
    if CONFIG_PATH != _DEFAULT_CONFIG_PATH:
        return Path(CONFIG_PATH)
    if SERVERS_PATH != _DEFAULT_CONFIG_PATH:
        return Path(SERVERS_PATH)
    from .workspace import data_root

    return Path(os.environ.get("PA_MCP_CONFIG") or (data_root() / "mcp_servers.json"))


def _migrate_legacy_config(path: Path) -> None:
    """Move the old project-root config into private data storage on first admin read."""
    if (
        os.environ.get("PA_MCP_CONFIG")
        or CONFIG_PATH != _DEFAULT_CONFIG_PATH
        or SERVERS_PATH != _DEFAULT_CONFIG_PATH
        or path == _DEFAULT_CONFIG_PATH
    ):
        return
    try:
        source_stat = _DEFAULT_CONFIG_PATH.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("Unable to securely inspect legacy MCP config") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise RuntimeError("Legacy MCP config must be a regular file")
    try:
        fd = os.open(
            _DEFAULT_CONFIG_PATH,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        _restrict_legacy_config_permissions()
        raise RuntimeError("Unable to securely read legacy MCP config") from exc
    try:
        opened_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_dev != source_stat.st_dev
            or opened_stat.st_ino != source_stat.st_ino
        ):
            raise RuntimeError("Legacy MCP config must be a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            legacy = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _restrict_legacy_config_permissions()
        return
    except OSError as exc:
        _restrict_legacy_config_permissions()
        raise RuntimeError("Unable to securely read legacy MCP config") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    servers = legacy.get("servers") if isinstance(legacy, dict) else legacy
    if not isinstance(servers, list):
        _restrict_legacy_config_permissions()
        return
    try:
        save_servers([item for item in servers if isinstance(item, dict) and item.get("name")])
    except OSError:
        _restrict_legacy_config_permissions()
        raise
    try:
        _DEFAULT_CONFIG_PATH.unlink()
    except OSError:
        _restrict_legacy_config_permissions()


def _restrict_legacy_config_permissions() -> None:
    """Lock down a legacy file or stop if its permissions cannot be secured."""
    try:
        fd = os.open(
            _DEFAULT_CONFIG_PATH,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("Unable to secure legacy MCP config permissions") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Legacy MCP config must be a regular file")
        os.fchmod(fd, 0o600)
    except OSError:
        raise RuntimeError("Unable to secure legacy MCP config permissions") from None
    finally:
        os.close(fd)

# 一键添加的预设。needs 表示还缺哪个环境变量才能用。
PRESETS: list[dict] = [
    {
        "id": "notion",
        "label": "Notion 官方 MCP",
        "hint": "让 AI 直接读写你的 Notion 页面",
        "name": "notion",
        "command_line": "notion-mcp-server",
        "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
        "needs": {
            "key": "NOTION_TOKEN",
            "placeholder": "粘贴 Notion Integration Token（ntn_…）",
            "help": "在 notion.so/profile/integrations 创建 Internal Integration 后复制密钥",
        },
    },
    {
        "id": "podcast-article",
        "label": "本项目 MCP",
        "hint": "把本项目的分析能力开放给其他 AI 客户端",
        "name": "podcast-article",
        "command_line": f"uv --directory {ROOT} run podcast-article-mcp",
        "env": {},
        "needs": None,
    },
]

_SECRET_HINT = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL", re.I)


def parse_command_line(line: str) -> tuple[str, list[str]]:
    """把「npx -y @scope/pkg」拆成命令与参数。"""
    parts = shlex.split(line or "")
    if not parts:
        raise ValueError("启动命令不能为空")
    return parts[0], parts[1:]


def _expand(value: str, extra_env: dict[str, str] | None = None) -> str:
    """把 ${VAR} 展开成环境变量（.env 已由 config 加载进 os.environ）。"""
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: (extra_env or {}).get(m.group(1)) or os.environ.get(m.group(1), ""),
        value or "",
    )


def read_env_keys() -> dict[str, str]:
    """读 .env 里已配置的键（用于判断预设是否还需要密钥）。"""
    from . import settings

    return settings.read_env()


def load_servers() -> list[dict]:
    path = _config_path()
    if not path.exists():
        _migrate_legacy_config(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    servers = data.get("servers") if isinstance(data, dict) else data
    return [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]


def save_servers(servers: list[dict]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps({"servers": servers}, ensure_ascii=False, indent=2).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError:
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def get_server(name: str) -> dict | None:
    return next((s for s in load_servers() if s["name"] == name), None)


def upsert_server(entry: dict) -> dict:
    """新增或更新一台服务器。支持传 command_line（单行）或 command + args。"""
    name = (entry.get("name") or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    if entry.get("command_line") is not None:
        command, args = parse_command_line(entry.get("command_line", ""))
    else:
        command = (entry.get("command") or "").strip()
        if not command:
            raise ValueError("启动命令不能为空")
        args = [a for a in (entry.get("args") or []) if str(a).strip()]
    clean = {
        "name": name,
        "command": command,
        "args": args,
        "env": {k: v for k, v in (entry.get("env") or {}).items() if str(k).strip()},
        "note": entry.get("note", ""),
    }
    servers = [s for s in load_servers() if s["name"] != name]
    servers.append(clean)
    save_servers(servers)
    return clean


def preset_catalog(extra_env: dict[str, str] | None = None) -> list[dict]:
    """给界面的预设列表：带上「还缺什么」的状态。"""
    env = read_env_keys()
    env.update(extra_env or {})
    configured = {s["name"] for s in load_servers()}
    out = []
    for p in PRESETS:
        need = p.get("needs")
        ready = True
        if need and not env.get(need["key"]):
            # 服务器自己的 env 里若已直接写了值，也算就绪
            saved = get_server(p["name"]) or {}
            saved_value = str((saved.get("env") or {}).get(need["key"], "")).strip()
            if saved_value.startswith("$" + "{") and saved_value.endswith("}"):
                ready = bool(env.get(saved_value[2:-1]))
            else:
                ready = bool(saved_value)
        out.append({
            "id": p["id"],
            "label": p["label"],
            "hint": p["hint"],
            "ready": ready,
            "added": p["name"] in configured,
            "need_key": (need or {}).get("key", ""),
            "need_placeholder": (need or {}).get("placeholder", ""),
            "need_help": (need or {}).get("help", ""),
        })
    return out


def build_from_preset(preset_id: str, secret: str | None = None) -> dict:
    """按预设生成配置并保存；个人密钥必须由调用方加密保存。"""
    preset = next((p for p in PRESETS if p["id"] == preset_id), None)
    if not preset:
        raise ValueError(f"未知的预设：{preset_id}")
    if secret:
        raise ValueError("preset secrets must be stored through encrypted per-account integrations")
    env = dict(preset.get("env") or {})
    entry = {
        "name": preset["name"],
        "command_line": preset["command_line"],
        "env": env,
        "note": preset["label"],
    }
    return upsert_server(entry)


def delete_server(name: str) -> bool:
    servers = load_servers()
    kept = [s for s in servers if s["name"] != name]
    if len(kept) == len(servers):
        return False
    save_servers(kept)
    return True


def resolved_env(entry: dict, *, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """展开 ${VAR}，丢掉空值（空值会让某些服务器启动失败）。"""
    return {
        k: _expand(str(v), extra_env)
        for k, v in (entry.get("env") or {}).items()
        if _expand(str(v), extra_env)
    }


def mask_entry(entry: dict) -> dict:
    """给前端看的样子：密钥只留前后几位。"""
    def mask(v: str) -> str:
        v = str(v)
        if not v:
            return ""
        if v.startswith("${"):
            return v  # 变量引用，本来就不是明文
        return f"{v[:6]}…{v[-4:]}" if len(v) > 12 else "•••"

    return {
        "name": entry.get("name", ""),
        "command": entry.get("command", ""),
        "args": entry.get("args", []),
        "env": {k: mask(v) for k, v in (entry.get("env") or {}).items()},
        "note": entry.get("note", ""),
        "has_secret": any(_SECRET_HINT.search(k) for k in (entry.get("env") or {})),
    }
