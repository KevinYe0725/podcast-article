"""MCP 服务器配置的读写。

配置存于项目根目录 mcp_servers.json（可能含密钥，已 gitignore）。
env 的值支持 ${VAR} 引用 .env 里的变量，避免把 token 复制两份。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp_servers.json"

# UI 里一键添加的示例（Notion 官方 MCP）
EXAMPLES = [
    {
        "name": "notion",
        "command": "notion-mcp-server",
        "args": [],
        "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
        "note": "Notion 官方 MCP Server（npm i -g @notionhq/notion-mcp-server）",
    },
    {
        "name": "podcast-article",
        "command": "uv",
        "args": ["--directory", str(CONFIG_PATH.parent), "run", "podcast-article-mcp"],
        "env": {},
        "note": "本项目自带的 MCP Server",
    },
]

_SECRET_HINT = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL", re.I)


def _expand(value: str) -> str:
    """把 ${VAR} 展开成环境变量（.env 已由 config 加载进 os.environ）。"""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value or "")


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
    name = (entry.get("name") or "").strip()
    if not name:
        raise ValueError("服务器名称不能为空")
    if not (entry.get("command") or "").strip():
        raise ValueError("启动命令不能为空")
    clean = {
        "name": name,
        "command": entry["command"].strip(),
        "args": [a for a in (entry.get("args") or []) if str(a).strip()],
        "env": {k: v for k, v in (entry.get("env") or {}).items() if str(k).strip()},
        "note": entry.get("note", ""),
    }
    servers = [s for s in load_servers() if s["name"] != name]
    servers.append(clean)
    save_servers(servers)
    return clean


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
