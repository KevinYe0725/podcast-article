"""MCP 服务器配置的读写。

配置存于项目根目录 mcp_servers.json（可能含密钥，已 gitignore）。
env 的值支持 ${VAR} 引用 .env 里的变量，避免把 token 复制两份。

界面上以「预设」为主：常见服务器点一下就配好，自定义才需要填命令。
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp_servers.json"
ROOT = CONFIG_PATH.parent

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


def _expand(value: str) -> str:
    """把 ${VAR} 展开成环境变量（.env 已由 config 加载进 os.environ）。"""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value or "")


def read_env_keys() -> dict[str, str]:
    """读 .env 里已配置的键（用于判断预设是否还需要密钥）。"""
    from . import settings

    return settings.read_env()


def load_servers() -> list[dict]:
    if not CONFIG_PATH.exists():
        return []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    servers = data.get("servers") if isinstance(data, dict) else data
    return [s for s in (servers or []) if isinstance(s, dict) and s.get("name")]


def save_servers(servers: list[dict]) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"servers": servers}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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


def preset_catalog() -> list[dict]:
    """给界面的预设列表：带上「还缺什么」的状态。"""
    env = read_env_keys()
    configured = {s["name"] for s in load_servers()}
    out = []
    for p in PRESETS:
        need = p.get("needs")
        ready = True
        if need and not env.get(need["key"]):
            # 服务器自己的 env 里若已直接写了值，也算就绪
            saved = get_server(p["name"]) or {}
            ready = bool((saved.get("env") or {}).get(need["key"], "").strip())
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
    """按预设生成配置并保存。secret 用于补上缺失的密钥。"""
    preset = next((p for p in PRESETS if p["id"] == preset_id), None)
    if not preset:
        raise ValueError(f"未知的预设：{preset_id}")
    env = dict(preset.get("env") or {})
    need = preset.get("needs") or {}
    if secret and need.get("key"):
        env[need["key"]] = secret.strip()
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


def resolved_env(entry: dict) -> dict[str, str]:
    """展开 ${VAR}，丢掉空值（空值会让某些服务器启动失败）。"""
    return {k: _expand(str(v)) for k, v in (entry.get("env") or {}).items() if _expand(str(v))}


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
