"""把文章发布到目标位置：内置 Notion REST，或任意 MCP 服务器上的某个工具。

MCP 目标通过「参数模板」适配不同服务器的工具签名，模板里可用占位符：
{{title}} {{podcast}} {{date}} {{duration}} {{url}} {{content}} {{blocks}}
其中 {{blocks}} 会被替换成 Notion 块 JSON 数组（前端已转好的原生块）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import mcp_client, mcp_config, notion

_URL_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|app\.notion\.com)/[^\s\)\]\"'，。]+")

# Notion 官方 MCP Server 创建页面的参数模板（属性名按你的数据库调整）
NOTION_MCP_TEMPLATE: dict[str, Any] = {
    "parent": {"database_id": "<替换成你的数据库 ID>"},
    "properties": {
        "标题": {"title": [{"text": {"content": "{{title}}"}}]},
        "播客": {"rich_text": [{"text": {"content": "{{podcast}}"}}]},
        "来源": {"url": "{{url}}"},
    },
    "children": "{{blocks}}",
}


def render_args(template: Any, ctx: dict[str, Any]) -> Any:
    """把模板里的 {{占位符}} 替换成实际值；{{blocks}} 等结构化占位符注入原始 JSON。"""
    if isinstance(template, dict):
        return {k: render_args(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [render_args(v, ctx) for v in template]
    if isinstance(template, str):
        exact = re.fullmatch(r"\{\{(\w+)\}\}", template.strip())
        if exact:
            key = exact.group(1)
            value = ctx.get(key)
            if key in ("blocks", "properties_json"):
                if isinstance(value, str):
                    return json.loads(value)
                return value
            return "" if value is None else str(value)
        return re.sub(
            r"\{\{(\w+)\}\}",
            lambda m: "" if ctx.get(m.group(1)) is None else str(ctx[m.group(1)]),
            template,
        )
    return template


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)") if m else None


def publish(ctx: dict[str, Any], target: str = "builtin", template: Any = None) -> dict:
    """ctx: title / podcast / date / duration / url / content / blocks。"""
    if target in ("", "builtin", "notion", None):
        url = notion.push_article(
            title=ctx["title"],
            markdown_text=ctx["content"],
            source_url=ctx.get("url"),
            podcast=ctx.get("podcast"),
            pub_date=ctx.get("date"),
            duration=ctx.get("duration"),
        )
        return {"via": "内置 Notion 集成（REST API）", "url": url, "text": f"已创建页面：{url}"}

    if not target.startswith("mcp:"):
        raise ValueError(f"未知的发布目标：{target}")

    parts = target.split(":", 2)
    if len(parts) < 3:
        raise ValueError("MCP 目标格式应为 mcp:<服务器名>:<工具名>")
    _, server_name, tool_name = parts

    entry = mcp_config.get_server(server_name)
    if not entry:
        raise ValueError(f"未找到 MCP 服务器：{server_name}")

    if template is None:
        template = NOTION_MCP_TEMPLATE
    args = render_args(template, ctx)
    result = mcp_client.call_tool(
        command=entry["command"],
        args=entry.get("args"),
        env=mcp_config.resolved_env(entry),
        tool=tool_name,
        arguments=args,
    )
    if not result["ok"]:
        raise RuntimeError(f"MCP 工具返回错误：{result['text'][:400]}")
    return {
        "via": f"MCP · {server_name} · {tool_name}",
        "url": _extract_url(result["text"]),
        "text": result["text"][:2000],
    }
