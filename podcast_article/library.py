"""文章分类的存储层。

数据落在项目根目录 library.json（已 gitignore，属于个人数据）：

    {
      "categories": [{"id": "c1", "name": "AI 技术", "color": "#10a37f"}],
      "assignments": {"20241029-无人知晓-E37-鱼不存在": "c1"}
    }

分类是全局概念（可以增删改名），文章归属按输出目录名记录。
删除分类不会删文章，只是把文章退回「未分类」。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .config import PROJECT_ROOT

# 可用 PA_LIBRARY_FILE 覆盖（测试跑在临时文件上，不碰真实分类数据）
STORE_PATH = Path(os.environ.get("PA_LIBRARY_FILE") or (PROJECT_ROOT / "library.json"))

# 新建分类时轮换的配色（与界面主题一致）
PALETTE = [
    "#10a37f", "#4d6bfe", "#d97706", "#c026d3",
    "#0ea5e9", "#ef4146", "#7c3aed", "#65a30d",
]


def _load() -> dict:
    if not STORE_PATH.exists():
        return {"categories": [], "assignments": {}}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"categories": [], "assignments": {}}
    data.setdefault("categories", [])
    data.setdefault("assignments", {})
    return data


def _save(data: dict) -> None:
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def snapshot() -> dict:
    """分类列表 + 文章归属（供接口直接返回）。"""
    data = _load()
    counts: dict[str, int] = {}
    for cid in data["assignments"].values():
        counts[cid] = counts.get(cid, 0) + 1
    cats = [{**c, "count": counts.get(c["id"], 0)} for c in data["categories"]]
    return {"categories": cats, "assignments": data["assignments"]}


def create(name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("分类名不能为空")
    if len(name) > 20:
        raise ValueError("分类名不要超过 20 个字")
    data = _load()
    if any(c["name"] == name for c in data["categories"]):
        raise ValueError(f"已存在同名分类：{name}")
    color = PALETTE[len(data["categories"]) % len(PALETTE)]
    cat = {"id": "c" + uuid.uuid4().hex[:8], "name": name, "color": color}
    data["categories"].append(cat)
    _save(data)
    return cat


def rename(cid: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("分类名不能为空")
    data = _load()
    for c in data["categories"]:
        if c["id"] == cid:
            if any(o["name"] == name and o["id"] != cid for o in data["categories"]):
                raise ValueError(f"已存在同名分类：{name}")
            c["name"] = name
            _save(data)
            return c
    raise ValueError("分类不存在")


def delete(cid: str) -> None:
    data = _load()
    before = len(data["categories"])
    data["categories"] = [c for c in data["categories"] if c["id"] != cid]
    if len(data["categories"]) == before:
        raise ValueError("分类不存在")
    # 文章退回未分类，不删文章
    data["assignments"] = {k: v for k, v in data["assignments"].items() if v != cid}
    _save(data)


def assign(dir_name: str, cid: str | None) -> None:
    """把一篇文章放进分类；cid 为空表示移出分类。"""
    dir_name = (dir_name or "").strip()
    if not dir_name:
        raise ValueError("缺少文章目录")
    data = _load()
    if cid:
        if not any(c["id"] == cid for c in data["categories"]):
            raise ValueError("分类不存在")
        data["assignments"][dir_name] = cid
    else:
        data["assignments"].pop(dir_name, None)
    _save(data)


def category_of(dir_name: str) -> str | None:
    return _load()["assignments"].get(dir_name)


def forget(dir_name: str) -> None:
    """文章目录消失时清掉它的归属记录。"""
    data = _load()
    if data["assignments"].pop(dir_name, None) is not None:
        _save(data)
