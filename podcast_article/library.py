"""文章分类与阅读状态的存储层。

数据落在项目根目录 library.json（已 gitignore，属于个人数据）：

    {
      "categories": [{"id": "c1", "name": "AI 技术", "color": "#10a37f"}],
      "assignments": {"20241029-无人知晓-E37-鱼不存在": "c1"},
      "status": {"20241029-无人知晓-E37-鱼不存在": {"s": "read", "at": 1712345678.0}}
    }

分类是全局概念（可以增删改名），文章归属按输出目录名记录。
删除分类不会删文章，只是把文章退回「未分类」。

阅读状态有四种，未记录时默认「未读」：
    unread 未读 / reading 在读 / read 已读 / later 稍后读
"""
from __future__ import annotations

import json
import os
import time
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

# 阅读状态（顺序即界面上的展示顺序）
STATUSES = ("unread", "reading", "read", "later")
STATUS_LABELS = {"unread": "未读", "reading": "在读", "read": "已读", "later": "稍后读"}
DEFAULT_STATUS = "unread"


def _store_path(store_path: Path | None = None) -> Path:
    return Path(store_path) if store_path is not None else STORE_PATH


def _load(store_path: Path | None = None) -> dict:
    path = _store_path(store_path)
    if not path.exists():
        return {"categories": [], "assignments": {}, "status": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"categories": [], "assignments": {}, "status": {}}
    data.setdefault("categories", [])
    data.setdefault("assignments", {})
    data.setdefault("status", {})
    # 存储被手改坏时（值是列表/字符串）就地修正，否则后续写入会直接抛异常
    if not isinstance(data["categories"], list):
        data["categories"] = []
    if not isinstance(data["assignments"], dict):
        data["assignments"] = {}
    if not isinstance(data["status"], dict):
        data["status"] = {}
    return data


def _save(data: dict, store_path: Path | None = None) -> None:
    # 原子写：半截文件会让整份个人数据读不出来
    path = _store_path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _status_map(data: dict) -> dict[str, str]:
    """把存储结构压成 {目录名: 状态}，顺手丢掉无法识别的状态值。

    存储被手改坏时（status 写成列表、条目写成非字典）不能让整份数据读不出来，
    所以这里对结构本身也做类型检查，而不是只检查状态值。
    """
    raw = data.get("status")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        s = v.get("s") if isinstance(v, dict) else v
        if isinstance(s, str) and s in STATUSES:
            out[k] = s
    return out


def snapshot(*, store_path: Path | None = None) -> dict:
    """分类列表 + 文章归属 + 阅读状态（供接口直接返回）。"""
    data = _load(store_path)
    counts: dict[str, int] = {}
    for cid in data["assignments"].values():
        counts[cid] = counts.get(cid, 0) + 1
    cats = [{**c, "count": counts.get(c["id"], 0)} for c in data["categories"]]
    status = _status_map(data)
    status_counts = {s: 0 for s in STATUSES}
    for s in status.values():
        status_counts[s] = status_counts.get(s, 0) + 1
    return {
        "categories": cats,
        "assignments": data["assignments"],
        "status": status,
        "status_counts": status_counts,
        "status_labels": STATUS_LABELS,
    }


def create(name: str, *, store_path: Path | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("分类名不能为空")
    if len(name) > 20:
        raise ValueError("分类名不要超过 20 个字")
    data = _load(store_path)
    if any(c["name"] == name for c in data["categories"]):
        raise ValueError(f"已存在同名分类：{name}")
    color = PALETTE[len(data["categories"]) % len(PALETTE)]
    cat = {"id": "c" + uuid.uuid4().hex[:8], "name": name, "color": color}
    data["categories"].append(cat)
    _save(data, store_path)
    return cat


def rename(cid: str, name: str, *, store_path: Path | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("分类名不能为空")
    data = _load(store_path)
    for c in data["categories"]:
        if c["id"] == cid:
            if any(o["name"] == name and o["id"] != cid for o in data["categories"]):
                raise ValueError(f"已存在同名分类：{name}")
            c["name"] = name
            _save(data, store_path)
            return c
    raise ValueError("分类不存在")


def delete(cid: str, *, store_path: Path | None = None) -> None:
    data = _load(store_path)
    before = len(data["categories"])
    data["categories"] = [c for c in data["categories"] if c["id"] != cid]
    if len(data["categories"]) == before:
        raise ValueError("分类不存在")
    # 文章退回未分类，不删文章
    data["assignments"] = {k: v for k, v in data["assignments"].items() if v != cid}
    _save(data, store_path)


def assign(dir_name: str, cid: str | None, *, store_path: Path | None = None) -> None:
    """把一篇文章放进分类；cid 为空表示移出分类。"""
    dir_name = (dir_name or "").strip()
    if not dir_name:
        raise ValueError("缺少文章目录")
    data = _load(store_path)
    if cid:
        if not any(c["id"] == cid for c in data["categories"]):
            raise ValueError("分类不存在")
        data["assignments"][dir_name] = cid
    else:
        data["assignments"].pop(dir_name, None)
    _save(data, store_path)


def category_of(dir_name: str, *, store_path: Path | None = None) -> str | None:
    return _load(store_path)["assignments"].get(dir_name)


def forget(dir_name: str, *, store_path: Path | None = None) -> None:
    """文章目录消失时清掉它的归属记录与阅读状态。"""
    data = _load(store_path)
    dirty = data["assignments"].pop(dir_name, None) is not None
    dirty = data["status"].pop(dir_name, None) is not None or dirty
    if dirty:
        _save(data, store_path)


# ---------------------------------------------------------------- 阅读状态


def statuses(*, store_path: Path | None = None) -> dict[str, str]:
    """{目录名: 状态}，只含显式记录过的文章。"""
    return _status_map(_load(store_path))


def status_of(dir_name: str, *, store_path: Path | None = None) -> str:
    """未记录时返回 DEFAULT_STATUS（未读），所以调用方不用处理 None。"""
    return statuses(store_path=store_path).get(dir_name, DEFAULT_STATUS)


def set_status(dir_name: str, status: str | None, *, store_path: Path | None = None) -> str:
    """设置阅读状态。status 为空表示清除记录（回到默认的「未读」）。"""
    dir_name = (dir_name or "").strip()
    if not dir_name:
        raise ValueError("缺少文章目录")
    if status and status not in STATUSES:
        raise ValueError(f"未知的阅读状态：{status}（可选：{'、'.join(STATUSES)}）")
    data = _load(store_path)
    if status:
        data["status"][dir_name] = {"s": status, "at": round(time.time(), 3)}
    else:
        data["status"].pop(dir_name, None)
    _save(data, store_path)
    return status or DEFAULT_STATUS
