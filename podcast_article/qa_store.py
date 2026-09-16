"""阅读助手问答记录的存储。

每集一个 `output/<dir>/qa.json`：

    {
      "items": [
        {"id": "a1b2c3d4", "at": 1712345678.0,
         "selection": "被选中的原文", "question": "用户追问（可能为空）",
         "answer": "AI 的解读（markdown）",
         "passages": [{"ts": "00:10:07", "text": "引用的原文", "start": 607}],
         "web": {"ok": true, "provider": "bing", "results": [{"title","url","snippet"}]},
         "error": ""}
      ]
    }

为什么存在这一集旁边而不是集中一个文件：**问答是这一集的附属内容**，
跟着文章一起被删除/备份/迁移才符合直觉（用户在文章库里删掉这一集，它的问答不该留着变孤儿）。

容量上限 `MAX_ITEMS`：这是阅读笔记不是语料库，只留最近的若干条，
免得一个 json 无限膨胀（界面会明说「只保留最近 N 条」）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

MAX_ITEMS = 50
FILE_NAME = "qa.json"

# 存进 json 的字符串字段都截断，避免一次误选整篇把文件撑大
MAX_SELECTION = 2000
MAX_QUESTION = 600
MAX_ANSWER = 20000


def _path(workdir: Path) -> Path:
    return Path(workdir) / FILE_NAME


def load(workdir: Path) -> list[dict]:
    """读这一集的问答记录（按时间**倒序**，最新在前）。坏文件/结构不对 → []。"""
    p = _path(workdir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    clean = [i for i in items if isinstance(i, dict) and i.get("answer") is not None]
    clean.sort(key=lambda i: i.get("at") or 0, reverse=True)
    return clean


def _save(workdir: Path, items: list[dict]) -> None:
    p = _path(workdir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)          # 原子写：半截文件会让整集问答都读不出来


def append(workdir: Path, *, selection: str, question: str, answer: str,
           passages: list[dict] | None = None, web: dict | None = None,
           error: str = "") -> dict:
    """追加一条记录并落盘，返回存下来的那条（含生成的 id）。"""
    record = {
        "id": "a" + uuid.uuid4().hex[:8],
        "at": round(time.time(), 3),
        "selection": (selection or "")[:MAX_SELECTION],
        "question": (question or "")[:MAX_QUESTION],
        "answer": (answer or "")[:MAX_ANSWER],
        "passages": [
            {"ts": p.get("ts"), "text": (p.get("text") or "")[:800], "start": p.get("start")}
            for p in (passages or []) if isinstance(p, dict)
        ],
        "web": _slim_web(web),
        "error": (error or "")[:500],
    }
    items = load(workdir)                       # 已按时间倒序
    items.insert(0, record)
    _save(workdir, items[:MAX_ITEMS])
    return record


def _slim_web(web: dict | None) -> dict | None:
    """网络结果只留展示需要的字段，别把整个响应体写进 json。"""
    if not isinstance(web, dict):
        return None
    return {
        "ok": bool(web.get("ok")),
        "provider": web.get("provider") or "",
        "error": (web.get("error") or "")[:300],
        "results": [
            {"title": (r.get("title") or "")[:200],
             "url": (r.get("url") or "")[:500],
             "snippet": (r.get("snippet") or "")[:400]}
            for r in (web.get("results") or [])[:6] if isinstance(r, dict)
        ],
    }


def remove(workdir: Path, item_id: str) -> bool:
    items = load(workdir)
    left = [i for i in items if i.get("id") != item_id]
    if len(left) == len(items):
        return False
    _save(workdir, left)
    return True


def clear(workdir: Path) -> int:
    items = load(workdir)
    if items:
        _save(workdir, [])
    return len(items)


def count(workdir: Path) -> int:
    return len(load(workdir))
