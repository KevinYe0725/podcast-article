"""把生成的文章写入 Notion。

- markdown → Notion blocks（标题/引用/列表/表格/行内加粗斜体代码链接）
- 支持写入数据库（一行）或父页面（一个子页面），分批追加（每批 ≤100 blocks）
"""
from __future__ import annotations

import json
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
_BATCH = 90  # Notion 每次 append 的 children 上限是 100，留点余量


def _session() -> requests.Session:
    """带重试的 session：跨境网络对 api.notion.com 的 TLS 干扰（SSL EOF）很常见。"""
    s = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=2,
        status=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=None,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class NotionError(RuntimeError):
    pass


# ---------------------------------------------------------------- 行内 rich text

_INLINE_RE = re.compile(r"(\*\*.+?\*\*|\*[^*]+?\*|`[^`]+?`|\[[^\]]+?\]\([^)]+?\))")


def _rich(text: str) -> list[dict]:
    """把一行 markdown 文本拆成 Notion rich_text 片段（支持 **粗** *斜* `码` [文](链)）。"""
    out: list[dict] = []
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        seg = {"type": "text", "text": {"content": part}}
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            seg["text"]["content"] = part[2:-2]
            seg["annotations"] = {"bold": True}
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            seg["text"]["content"] = part[1:-1]
            seg["annotations"] = {"italic": True}
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            seg["text"]["content"] = part[1:-1]
            seg["annotations"] = {"code": True}
        elif part.startswith("[") and "](" in part:
            m = re.fullmatch(r"\[([^\]]+?)\]\(([^)]+?)\)", part)
            if m:
                seg["text"] = {"content": m.group(1), "link": {"url": m.group(2)}}
        out.append(seg)
    return out or [{"type": "text", "text": {"content": ""}}]


def _block(btype: str, text: str | None = None, **extra) -> dict:
    b: dict = {"object": "block", "type": btype}
    if text is not None:
        b[btype] = {"rich_text": _rich(text)}
    b[btype].update(extra)
    return b


def _table_row(cells: list[str]) -> dict:
    return {
        "object": "block",
        "type": "table_row",
        "table_row": {"cells": [_rich(c) for c in cells]},
    }


def _split_row(line: str) -> list[str]:
    line = line.strip().strip("|")
    return [c.strip() for c in line.split("|")]


# ---------------------------------------------------------------- markdown → blocks

_HEADING = {"#": "heading_1", "##": "heading_2", "###": "heading_3"}


def markdown_to_blocks(md: str) -> list[dict]:
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m and m.group(1) in _HEADING:
            blocks.append(_block(_HEADING[m.group(1)], m.group(2)))
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip()):
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i].strip()))
                i += 1
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            blocks.append(
                {
                    "object": "block",
                    "type": "table",
                    "table": {
                        "table_width": width,
                        "has_column_header": True,
                        "children": [_table_row(r) for r in rows],
                    },
                }
            )
            continue

        # 引用（连续 > 行合并成一个 quote）
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(_block("quote", "\n".join(quote_lines)))
            continue

        # 列表
        if re.match(r"^[-*]\s+", stripped):
            blocks.append(_block("bulleted_list_item", re.sub(r"^[-*]\s+", "", stripped)))
            i += 1
            continue
        m = re.match(r"^(\d+)[.、]\s+(.*)$", stripped)
        if m:
            blocks.append(_block("numbered_list_item", m.group(2)))
            i += 1
            continue

        # 分隔线
        if stripped in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        blocks.append(_block("paragraph", stripped))
        i += 1
    return blocks


# ---------------------------------------------------------------- API 调用

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.notion_token()}",
        "Notion-Version": VERSION,
        "Content-Type": "application/json",
    }


def _check(resp: requests.Response, what: str) -> dict:
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message", resp.text[:200])
        except ValueError:
            msg = resp.text[:200]
        raise NotionError(f"Notion {what} 失败（HTTP {resp.status_code}）：{msg}")
    return resp.json()


def _append_children(page_id: str, blocks: list[dict]) -> None:
    for start in range(0, len(blocks), _BATCH):
        _check(
            _session().patch(
                f"{API}/blocks/{page_id}/children",
                headers=_headers(),
                json={"children": blocks[start : start + _BATCH]},
                timeout=60,
            ),
            "追加内容",
        )


def _database_properties(database_id: str) -> dict[str, str]:
    """返回数据库属性名 -> 类型 的映射。"""
    data = _check(
        _session().get(f"{API}/databases/{database_id}", headers=_headers(), timeout=30),
        "读取数据库",
    )
    return {name: prop.get("type", "") for name, prop in (data.get("properties") or {}).items()}


def _iso_date(pub_date: str | None) -> str | None:
    """把各种 ISO 时间规范化为 Notion date.start 接受的格式（去掉毫秒）。"""
    if not pub_date:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", pub_date)
    if m:
        return f"{m.group(1)}T{m.group(2)}Z"
    return pub_date[:10]  # 纯日期


def push_article(
    title: str,
    markdown_text: str,
    source_url: str | None = None,
    podcast: str | None = None,
    pub_date: str | None = None,
    duration: float | None = None,
) -> str:
    """创建页面并写入文章，返回页面 URL。可选元信息会填入数据库同名属性。"""
    database_id = config.notion_database_id()
    parent_page_id = config.notion_parent_page_id()
    if not database_id and not parent_page_id:
        raise NotionError(
            "未配置目标位置：请在 .env 里设置 NOTION_DATABASE_ID（写入数据库）"
            "或 NOTION_PARENT_PAGE_ID（作为子页面）"
        )

    if database_id:
        props_meta = _database_properties(database_id)
        prop_name = next(
            (name for name, ptype in props_meta.items() if ptype == "title"), "Name"
        )
        parent = {"type": "database_id", "database_id": database_id}
        properties = {prop_name: {"title": [{"text": {"content": title}}]}}
        if podcast and props_meta.get("播客") == "rich_text":
            properties["播客"] = {"rich_text": [{"text": {"content": podcast}}]}
        if duration and props_meta.get("时长") == "rich_text":
            from .util import human_time

            properties["时长"] = {"rich_text": [{"text": {"content": human_time(duration)}}]}
        date_start = _iso_date(pub_date)
        if date_start and props_meta.get("日期") == "date":
            properties["日期"] = {"date": {"start": date_start}}
        if source_url and props_meta.get("来源") == "url":
            properties["来源"] = {"url": source_url}
    else:
        parent = {"type": "page_id", "page_id": parent_page_id}
        properties = {"title": {"title": [{"text": {"content": title}}]}}

    page = _check(
        _session().post(
            f"{API}/pages",
            headers=_headers(),
            json={"parent": parent, "properties": properties, "icon": {"type": "emoji", "emoji": "🎙"}},
            timeout=60,
        ),
        "创建页面",
    )
    page_id = page["id"]

    blocks = markdown_to_blocks(markdown_text)
    if source_url:
        blocks.append(_block("paragraph", f"[原文链接]({source_url})"))
    _append_children(page_id, blocks)
    return page["url"]
