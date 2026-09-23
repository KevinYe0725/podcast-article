"""批量队列：一次提交多条链接，按顺序一条条跑完。

为什么需要它：转写是本地跑的重活（一期 100 分钟节目要十几分钟），
用户更想要的是「把攒下来的十条链接丢进去，回头来收文章」，
而不是守在页面旁边一条条点。所以队列是**持久化**的（queue.json），
浏览器关了、服务重启了都不会丢。

存储结构（queue.json，已 gitignore）：

    {
      "items": [
        {"id": "q1a2b3c4", "url": "...", "pick": 1, "title": "",
         "source": "manual" | "feed:<id>",
         "state": "pending" | "running" | "done" | "error" | "skipped",
         "opts": {...},                      # 提交时首页的选项快照
         "added_at": 1712345678.0, "started_at": null, "finished_at": null,
         "dir": null,                        # 跑完后记下输出目录名
         "error": ""}
      ]
    }

本模块只负责数据，**不负责执行**（执行循环在 webapp 里，见 _queue_worker），
这样所有状态转换都能用 pytest 直接测，不碰网络与线程。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from functools import lru_cache

from . import links
from .config import PROJECT_ROOT
from .platform_store import PlatformStore, PlatformJob
from .workspace import data_root

# "1. http…" / "- http…" / "* http…" / "• http…" 这类行首标记
_BULLET = re.compile(r"^(?:\d+[.)、]|[-*•])+\s*")

# 可用 PA_QUEUE_FILE 覆盖（测试跑在临时文件上）
QUEUE_PATH = Path(os.environ.get("PA_QUEUE_FILE") or (PROJECT_ROOT / "queue.json"))

STATES = ("pending", "running", "done", "error", "skipped")
STATE_LABELS = {
    "pending": "排队中", "running": "生成中",
    "done": "已完成", "error": "失败", "skipped": "已跳过",
}
MAX_ITEMS = 200          # 队列上限，防止误粘贴把 json 撑爆
MAX_URLS_PER_CALL = 50


@lru_cache(maxsize=4)
def _platform_store(path: str) -> PlatformStore:
    return PlatformStore(Path(path))


def _owner_store(owner_id: str) -> PlatformStore:
    return _platform_store(str(data_root() / "platform.sqlite"))


def _platform_item(job: PlatformJob) -> dict:
    payload = dict(job.payload)
    return {
        "id": job.id,
        "owner_id": job.owner_id,
        "url": payload.get("url") or "",
        "pick": payload.get("pick", 1),
        "title": payload.get("title") or "",
        "source": payload.get("source") or "manual",
        "state": job.state,
        "opts": payload.get("opts") or {},
        "added_at": round(job.created_at, 3),
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "dir": job.dir_name,
        "error": job.error,
    }


def _load() -> dict:
    if not QUEUE_PATH.exists():
        return {"items": []}
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def _save(data: dict) -> None:
    tmp = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(QUEUE_PATH)


def _find(data: dict, item_id: str) -> dict | None:
    return next((i for i in data["items"] if i["id"] == item_id), None)


def snapshot(*, owner_id: str | None = None) -> dict:
    """队列全貌 + 计数（按加入顺序返回）。"""
    if owner_id is not None:
        items = [_platform_item(job) for job in _owner_store(owner_id).list_jobs(owner_id, limit=500)]
    else:
        items = _load()["items"]
    counts = {s: 0 for s in STATES}
    for it in items:
        counts[it.get("state", "pending")] = counts.get(it.get("state", "pending"), 0) + 1
    return {
        "items": items,
        "counts": counts,
        "labels": STATE_LABELS,
        "active": counts.get("pending", 0) + counts.get("running", 0),
    }


def parse_urls(text: str | list[str]) -> list[str]:
    """把用户粘贴的一坨文本拆成链接列表。

    支持每行一条、一行里用空格/逗号分开，也支持链接夹在说明文字里
    （分享按钮复制出来就是「【标题】https://…」）；顺手去掉重复与常见噪音
    （行首的序号、项目符号）—— 从聊天记录里复制过来经常带着这些东西。
    """
    raw = text if isinstance(text, list) else str(text or "").splitlines()
    out: list[str] = []
    for line in raw:
        line = str(line)
        # 链接：不必整行都是链接，抠出来就行（【标题】https://… / 1. https://…）
        for url in links.extract_links(line):
            if url not in out:
                out.append(url)
        # 本地文件路径：只认「以 / 或 ~ 开头的整段 token」，避免把标题里的 AI/人类 当路径
        for token in re.split(r"[\s,，、]+", line):
            token = _BULLET.sub("", token.strip()).strip()
            if not token or not token.startswith(("/", "~")):
                continue
            if token not in out:
                out.append(token)
    return out[:MAX_URLS_PER_CALL]


def add(urls: str | list[str], *, opts: dict | None = None,
        source: str = "manual", pick: int = 1, title: str = "",
        owner_id: str | None = None) -> list[dict]:
    """入队。返回新加进去的条目（已在队列里的重复链接会被跳过）。"""
    items = parse_urls(urls)
    if not items:
        raise ValueError("没有识别到链接（需要 http/https 链接，或本地文件路径）")
    if owner_id is not None:
        store = _owner_store(owner_id)
        known = {(it["url"], it.get("pick", 1)) for it in
                 (_platform_item(job) for job in store.list_jobs(owner_id, limit=500))
                 if it.get("state") in ("pending", "running")}
        payloads: list[dict] = []
        for url in items:
            normalized_pick = int(pick) if str(pick).isdigit() else 1
            if (url, normalized_pick) in known:
                continue
            payloads.append({
                "url": url, "pick": normalized_pick, "title": title or "",
                "source": source, "opts": dict(opts or {}),
            })
            known.add((url, normalized_pick))
        jobs = store.enqueue_jobs(owner_id, payloads, created_at=time.time())
        return [_platform_item(job) for job in jobs]
    data = _load()
    known = {(i.get("url"), i.get("pick", 1)) for i in data["items"]
             if i.get("state") in ("pending", "running")}
    added: list[dict] = []
    for url in items:
        if (url, pick) in known:
            continue
        if len(data["items"]) >= MAX_ITEMS:
            raise ValueError(f"队列最多 {MAX_ITEMS} 条，先清一清再提交")
        entry = {
            "id": "q" + uuid.uuid4().hex[:8],
            "url": url,
            "pick": int(pick) if str(pick).isdigit() else 1,
            "title": title or "",
            "source": source,
            "state": "pending",
            "opts": dict(opts or {}),
            "added_at": round(time.time(), 3),
            "started_at": None,
            "finished_at": None,
            "dir": None,
            "error": "",
        }
        data["items"].append(entry)
        added.append(entry)
    if added:
        _save(data)
    return added


def get(item_id: str, *, owner_id: str | None = None) -> dict | None:
    if owner_id is not None:
        job = _owner_store(owner_id).get_job(owner_id, item_id)
        return _platform_item(job) if job else None
    return _find(_load(), item_id)


def next_pending(*, owner_id: str | None = None) -> dict | None:
    """拿最早入队且还没跑的一条（FIFO）。"""
    if owner_id is not None:
        jobs = _owner_store(owner_id).list_jobs(owner_id, limit=500)
        pending = [job for job in jobs if job.state == "pending"]
        return _platform_item(min(pending, key=lambda job: (job.position, job.created_at))) if pending else None
    return next((i for i in _load()["items"] if i.get("state") == "pending"), None)


def next_fair_job() -> dict | None:
    """Atomically claim the next account-fair platform job for the global worker."""
    job = _platform_store(str(data_root() / "platform.sqlite")).next_job()
    return _platform_item(job) if job else None


def has_running(*, owner_id: str | None = None) -> bool:
    if owner_id is not None:
        return any(job.state == "running" for job in _owner_store(owner_id).list_jobs(owner_id, limit=500))
    return any(i.get("state") == "running" for i in _load()["items"])


def claim(item_id: str, *, owner_id: str | None = None) -> dict | None:
    """把一条标成 running（开始跑之前调用）。已经被别人抢走则返回 None。"""
    if owner_id is not None:
        job = _owner_store(owner_id).claim_job(owner_id, item_id)
        return _platform_item(job) if job else None
    data = _load()
    item = _find(data, item_id)
    if not item or item.get("state") != "pending":
        return None
    item["state"] = "running"
    item["started_at"] = round(time.time(), 3)
    item["error"] = ""
    _save(data)
    return item


def finish(item_id: str, state: str, *, dir_name: str | None = None,
           error: str = "", owner_id: str | None = None) -> dict:
    if state not in STATES:
        raise ValueError(f"未知状态：{state}")
    if owner_id is not None:
        store = _owner_store(owner_id)
        if not store.finish_job(owner_id, item_id, state, dir_name=dir_name, error=error):
            raise ValueError("队列里没有这一条")
        job = store.get_job(owner_id, item_id)
        return _platform_item(job) if job else {}
    data = _load()
    item = _find(data, item_id)
    if not item:
        raise ValueError("队列里没有这一条")
    item["state"] = state
    item["finished_at"] = round(time.time(), 3)
    if dir_name:
        item["dir"] = dir_name
    item["error"] = error[:500]
    _save(data)
    return item


def update(item_id: str, *, owner_id: str | None = None, **fields) -> dict:
    """只允许改标题这类展示字段（状态一律走 claim/finish）。"""
    allowed = {"title", "pick", "opts", "source"}
    if owner_id is not None:
        job = _owner_store(owner_id).update_job(owner_id, item_id, fields)
        if not job:
            raise ValueError("队列里没有这一条")
        return _platform_item(job)
    data = _load()
    item = _find(data, item_id)
    if not item:
        raise ValueError("队列里没有这一条")
    for k, v in fields.items():
        if k in allowed:
            item[k] = v
    _save(data)
    return item


def remove(item_id: str, *, owner_id: str | None = None) -> bool:
    if owner_id is not None:
        return _owner_store(owner_id).remove_job(owner_id, item_id)
    data = _load()
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i["id"] != item_id]
    if len(data["items"]) == before:
        return False
    _save(data)
    return True


def clear(*, keep_failed: bool = False, owner_id: str | None = None) -> int:
    """清掉已结束的条目（done 与 error）。

    排队中/生成中的条目**永远保留**——正在跑的那条被删掉会让执行循环失去跟踪对象。
    keep_failed=True 时连失败的也留着（方便回头重试）。
    """
    if owner_id is not None:
        return _owner_store(owner_id).clear_jobs(owner_id, keep_failed=keep_failed)
    data = _load()
    keep = {"pending", "running"} | ({"error"} if keep_failed else set())
    before = len(data["items"])
    data["items"] = [i for i in data["items"] if i.get("state") in keep]
    removed = before - len(data["items"])
    if removed:
        _save(data)
    return removed


def move(item_id: str, delta: int, *, owner_id: str | None = None) -> int:
    """上移/下移一条待跑的任务（delta=-1 提前）。返回新位置；非 pending 不动。"""
    if owner_id is not None:
        return _owner_store(owner_id).move_job(owner_id, item_id, delta)
    data = _load()
    idx = next((n for n, i in enumerate(data["items"]) if i["id"] == item_id), None)
    if idx is None:
        raise ValueError("队列里没有这一条")
    if data["items"][idx].get("state") != "pending":
        return idx
    new = max(0, min(len(data["items"]) - 1, idx + int(delta)))
    if new != idx:
        data["items"].insert(new, data["items"].pop(idx))
        _save(data)
    return new


def reorder(ids: list[str], *, owner_id: str | None = None) -> None:
    """按给定 id 顺序重排（未出现的保持原有相对顺序跟在后面）。"""
    if owner_id is not None:
        _owner_store(owner_id).reorder_jobs(owner_id, ids)
        return
    data = _load()
    order = {i: n for n, i in enumerate(ids)}
    data["items"].sort(key=lambda it: order.get(it["id"], len(order)))
    _save(data)


def retry_failed(*, owner_id: str | None = None) -> int:
    """把失败的全部退回排队，用于「重跑失败的那几条」。"""
    if owner_id is not None:
        return _owner_store(owner_id).retry_failed_jobs(owner_id)
    data = _load()
    n = 0
    for it in data["items"]:
        if it.get("state") == "error":
            it["state"] = "pending"
            it["error"] = ""
            it["finished_at"] = None
            n += 1
    if n:
        _save(data)
    return n


# 进程被杀时留下的 running 条目，最多自动重排这么多次，避免「一跑就崩」的任务无限循环
MAX_AUTO_RECOVER = 2


def recover_running(*, owner_id: str | None = None) -> int:
    """把卡在 running 的条目归位（服务重启/进程被杀之后调用）。

    服务是单进程单任务的：进程起来时如果还有 running，那一定是上次没跑完就死了，
    而队列只挑 pending —— 不归位的话整条队列会被一条永远跑不完的任务堵死。
    连续失败 MAX_AUTO_RECOVER 次的条目直接标失败，把问题暴露给用户而不是反复空转。
    """
    if owner_id is not None:
        return _owner_store(owner_id).recover_running_jobs(owner_id)
    # The platform queue is the service-wide durable queue; recover stale claims at startup.
    recovered_platform = _platform_store(str(data_root() / "platform.sqlite")).recover_running_jobs()
    data = _load()
    changed = 0
    for it in data["items"]:
        if it.get("state") != "running":
            continue
        tries = int(it.get("recovered") or 0) + 1
        it["recovered"] = tries
        if tries > MAX_AUTO_RECOVER:
            it["state"] = "error"
            it["error"] = "服务重启前这条任务没跑完，连续中断多次，已停止自动重试"
        else:
            it["state"] = "pending"
            it["started_at"] = None
            it["error"] = ""
        changed += 1
    if changed:
        _save(data)
    return changed + recovered_platform
