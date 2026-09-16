"""播客订阅（feed）的存储与轮询层。

数据落在项目根目录 feeds.json（可用 PA_FEEDS_FILE 覆盖，测试跑在临时文件上）：

    {
      "feeds": [
        {"id": "f12ab34c", "url": "<feed 链接>", "title": "<订阅时抓到的节目名>",
         "auto": true,              # 有新单集时是否自动排队生成文章
         "backfill": 0,             # 首次订阅时先把最新的 N 集也排队
         "added_at": 1712345678.0,
         "last_checked": 1712345678.0 | None,
         "last_error": "" | "错误信息",
         "seen": ["<guid 或链接>", ...]}   # 已处理的单集，防止重复入队
      ]
    }

设计要点：
- 网络访问只在 `_fetch()` 里发生，其余函数都是纯数据操作，方便测试替换。
- `check()` 只在「抓取成功」时写 seen / last_checked；失败只记 last_error 并跳过，
  这样定时轮询时某个 feed 挂掉不会影响其他 feed，也不会漏掉它的新单集。
- 「新单集」= 这次抓到的 entry id 不在 seen 里；返回的 pick 是**本次抓取那一刻**
  该单集在 feed 里的 1-based 位置（1 = 最新），可以直接喂给
  `sources.rss.fetch_episode(url, pick=N)`。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

from .config import PROJECT_ROOT

# 可用 PA_FEEDS_FILE 覆盖（测试跑在临时文件上，不碰真实订阅数据）
FEEDS_PATH = Path(os.environ.get("PA_FEEDS_FILE") or (PROJECT_ROOT / "feeds.json"))

# 与 sources/rss.py 保持一致的 UA，避免部分播客站拒绝默认 feedparser UA
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# update() 只允许改这几个字段，其他键忽略
_UPDATABLE = ("auto", "backfill", "title")


# ------------------------------------------------------------------ 存取

def _load() -> dict:
    if not FEEDS_PATH.exists():
        return {"feeds": []}
    try:
        data = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"feeds": []}
    if not isinstance(data, dict):
        return {"feeds": []}
    data.setdefault("feeds", [])
    if not isinstance(data["feeds"], list):
        data["feeds"] = []
    for feed in data["feeds"]:
        feed.setdefault("seen", [])
        feed.setdefault("last_error", "")
        feed.setdefault("last_checked", None)
    return data


def _save(data: dict) -> None:
    """原子写：先写同目录临时文件再 rename，避免留下半截 json。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    FEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FEEDS_PATH.with_name(f"{FEEDS_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, FEEDS_PATH)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _norm_url(url: str) -> str:
    """比较订阅链接是否重复时用的归一形式：去空白、去尾部斜杠。"""
    return (url or "").strip().rstrip("/")


def _find(data: dict, fid: str) -> dict | None:
    for feed in data["feeds"]:
        if feed.get("id") == fid:
            return feed
    return None


def _require(data: dict, fid: str) -> dict:
    feed = _find(data, fid)
    if feed is None:
        raise ValueError(f"订阅不存在：{fid}")
    return feed


# ------------------------------------------------------------------ feed 解析

def _fetch(url: str, timeout: float = 25.0):
    """唯一的网络出口（测试里用 monkeypatch 替换掉它就不会联网）。

    注意：**不能**给 feedparser.parse 传 request_timeout —— 它没有这个参数（传了直接
    TypeError，订阅会整体不可用；实测踩过）。feedparser 走 urllib、无法设超时，
    所以这里先用 requests 带超时把内容取回来，再交给 feedparser 解析。
    """
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=(10.0, timeout))
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def _parsed_or_raise(url: str, parsed) -> None:
    """和 sources/rss.py 保持一致：bozo 且完全没有 entry 才算失败。"""
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败：{parsed.bozo_exception}")
    if not parsed.entries:
        raise ValueError("这个 feed 里没有任何单集")


def _entry_id(entry) -> str:
    """单集去重用的 id，优先级固定，保证同一个 feed 反复抓取结果稳定：

        1. entry 的 `id` —— RSS 里的 <guid>/Atom 的 <id>，多数播客站就是永久链接或 GUID，
           是最可靠、不会随编辑标题而变的标识；
        2. `guid` —— feedparser 少数情况下把 guid 单列，兜底取一次；
        3. `link` —— 没有 guid 时用单集页面链接（同 feed 内唯一）；
        4. `title + published` —— 老站点两样都缺时才退回标题 + 发布时间；标题重复的
           可能性由此降到最低；
        5. 最后只剩标题时，加一个固定前缀，避免和上面几种形态串味。
    """
    for key in ("id", "guid"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    link = str(entry.get("link") or "").strip()
    if link:
        return link
    title = str(entry.get("title") or "").strip()
    published = str(entry.get("published") or entry.get("updated") or "").strip()
    if title and published:
        return f"{title}|{published}"
    if title:
        return f"title:{title}"
    return ""


def _entry_audio(entry) -> str:
    """音频地址：enclosure 优先（fetch_episode 也要求它），退化到 media_content。"""
    for enc in entry.get("enclosures") or []:
        if enc.get("href"):
            return enc["href"]
    for mc in entry.get("media_content") or []:
        if mc.get("url"):
            return mc["url"]
    return ""


def _parse_date(iso: str | None):
    """把 ISO 时间串解析成 datetime，失败返回 None。"""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entry_pub_date(entry) -> str | None:
    """抓取时刻顺手记下发布时间（与 sources/rss.py 的格式一致）。"""
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if not published:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", published)
    except (TypeError, ValueError):
        return None


def _episodes(parsed) -> list[dict]:
    """feed 里的单集列表，按 feed 顺序（1 = 最新），带 1-based 的 index。"""
    out: list[dict] = []
    for i, entry in enumerate(parsed.entries):
        out.append(
            {
                "title": (entry.get("title") or "").strip(),
                "pub_date": _entry_pub_date(entry),
                "index": i + 1,
                "entry_id": _entry_id(entry),
                "audio_url": _entry_audio(entry),
                "episode_url": (entry.get("link") or "").strip(),
            }
        )
    return out


def _show_title(parsed) -> str:
    return (parsed.feed.get("title") or "").strip()


# ------------------------------------------------------------------ 读接口

def load() -> dict:
    """读原始存储（含 seen 明细）。"""
    return _load()


def snapshot() -> dict:
    """给接口直接返回：{"feeds": [...], "total_new": int}。

    每项额外带 "new_count"（本轮发现但可能还没处理的条数，这里不比对新单集，固定给 0），
    并去掉可能很长的 seen 明细，只留 "seen_count"。
    """
    data = _load()
    items: list[dict] = []
    for feed in data["feeds"]:
        item = {k: v for k, v in feed.items() if k != "seen"}
        item["seen_count"] = len(feed.get("seen") or [])
        item["new_count"] = 0
        items.append(item)
    return {"feeds": items, "total_new": 0}


def get(fid: str) -> dict | None:
    """按 id 取一条订阅（含 seen 明细），没有则 None。"""
    return _find(_load(), fid)


# ------------------------------------------------------------------ 写接口

def add(
    url: str,
    *,
    title: str = "",
    auto: bool = True,
    backfill: int = 0,
    timeout: float = 25.0,
) -> dict:
    """订阅一个 feed（会真的抓一次）。

    - 抓不到 / 不是 feed → ValueError
    - 成功 → 当前所有单集 id 写入 seen
    - backfill > 0 → 把最新 backfill 集从 seen 里去掉，之后 check() 会把它们当成新单集
      返回（按旧→新），实现补跑历史
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("feed 链接不能为空")
    try:
        backfill = int(backfill)
    except (TypeError, ValueError):
        raise ValueError("backfill 必须是整数") from None
    if backfill < 0:
        raise ValueError("backfill 不能为负数")

    data = _load()
    key = _norm_url(url)
    for feed in data["feeds"]:
        if _norm_url(feed.get("url", "")) == key:
            raise ValueError(f"已经订阅过这个节目：{feed.get('title') or url}")

    try:
        parsed = _fetch(url, timeout=timeout)
    except ValueError:
        raise
    except Exception as exc:  # 网络层任何异常都归成一句人话
        raise ValueError(f"抓取 feed 失败：{exc}") from exc
    _parsed_or_raise(url, parsed)

    episodes = _episodes(parsed)
    seen = [ep["entry_id"] for ep in episodes if ep["entry_id"]]
    if backfill:
        # 从 seen 里去掉最新的 backfill 集（feed 顺序即新→旧），让 check() 补跑它们
        drop = {ep["entry_id"] for ep in episodes[:backfill] if ep["entry_id"]}
        seen = [eid for eid in seen if eid not in drop]

    feed = {
        "id": "f" + uuid.uuid4().hex[:8],
        "url": url,
        "title": (title or "").strip() or _show_title(parsed) or url,
        "auto": bool(auto),
        "backfill": backfill,
        "added_at": time.time(),
        "last_checked": None,
        "last_error": "",
        "seen": seen,
    }
    data["feeds"].append(feed)
    _save(data)
    return feed


def remove(fid: str) -> None:
    data = _load()
    before = len(data["feeds"])
    data["feeds"] = [f for f in data["feeds"] if f.get("id") != fid]
    if len(data["feeds"]) == before:
        raise ValueError(f"订阅不存在：{fid}")
    _save(data)


def update(fid: str, **fields) -> dict:
    """只允许改 auto / backfill / title，其他键忽略。"""
    data = _load()
    feed = _require(data, fid)
    for key in _UPDATABLE:
        if key not in fields:
            continue
        value = fields[key]
        if key == "backfill":
            try:
                value = max(0, int(value))
            except (TypeError, ValueError):
                raise ValueError("backfill 必须是整数") from None
        elif key == "auto":
            value = bool(value)
        else:
            value = str(value or "").strip()
        feed[key] = value
    _save(data)
    return feed


# ------------------------------------------------------------------ 订阅前预览

def discover(url: str, *, limit: int = 5, timeout: float = 25.0) -> dict:
    """订阅前预览：{"title", "episodes": [{"title","pub_date","index"}...], "count"}。

    index 是 1-based 的 pick 值（1 = 最新），可直接传给 rss.fetch_episode 的 pick。
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("feed 链接不能为空")
    try:
        parsed = _fetch(url, timeout=timeout)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"抓取 feed 失败：{exc}") from exc
    _parsed_or_raise(url, parsed)

    episodes = _episodes(parsed)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 5
    return {
        "title": _show_title(parsed) or url,
        "episodes": [
            {"title": ep["title"], "pub_date": ep["pub_date"], "index": ep["index"]}
            for ep in episodes[:limit]
        ],
        "count": len(episodes),
    }


# ------------------------------------------------------------------ 轮询

def check(fid: str | None = None, *, timeout: float = 25.0) -> list[dict]:
    """抓取（一个或全部）feed，找出没见过的单集，标记 seen 后返回它们。

    返回项：{"feed_id","feed_title","feed_url","episode_url","audio_url","title",
    "pub_date","pick"}，其中 pick 是本次抓取那一刻该单集在 feed 里的 1-based 位置。

    - 抓取失败：写入 last_error 并在结果里跳过（不抛异常，定时轮询不该被一个坏 feed 拖垮），
      且 **不更新** last_checked、不把单集标记成 seen（下次成功了还会再报）。
    - 顺序：按 pub_date 旧→新（补跑历史是时间正序），同 feed 内按 pick 倒序（也是旧→新）。
    """
    data = _load()
    if fid is not None:
        targets = [_require(data, fid)]
    else:
        targets = list(data["feeds"])

    found: list[dict] = []
    for feed in targets:
        try:
            parsed = _fetch(feed.get("url", ""), timeout=timeout)
            _parsed_or_raise(feed.get("url", ""), parsed)
        except Exception as exc:  # 抓取/解析失败都不该影响其他 feed
            feed["last_error"] = f"{type(exc).__name__}: {exc}"
            continue

        seen = list(feed.get("seen") or [])
        seen_set = set(seen)
        episodes = _episodes(parsed)
        for ep in episodes:
            eid = ep["entry_id"]
            if not eid:
                continue  # 没有任何稳定标识，只能放弃，避免每次轮询都重复入队
            if eid in seen_set:
                continue
            seen_set.add(eid)
            seen.append(eid)
            found.append(
                {
                    "feed_id": feed.get("id", ""),
                    "feed_title": feed.get("title") or _show_title(parsed),
                    "feed_url": feed.get("url", ""),
                    "episode_url": ep["episode_url"],
                    "audio_url": ep["audio_url"],
                    "title": ep["title"],
                    "pub_date": ep["pub_date"],
                    "pick": ep["index"],
                }
            )
        feed["seen"] = seen
        feed["last_checked"] = time.time()
        feed["last_error"] = ""

    _save(data)

    # 没有 pub_date 的排在最后（不知道时间就没法谈先后，仍保持原顺序稳定）
    found.sort(
        key=lambda item: (
            _parse_date(item.get("pub_date")) or datetime.max.replace(tzinfo=timezone.utc),
            item.get("feed_id", ""),
            -int(item.get("pick") or 0),
        )
    )
    return found


def due(interval_minutes: int = 60, *, now: float | None = None) -> list[str]:
    """距上次检查已超过 interval_minutes 的 feed id（从未检查过也算 due）。"""
    data = _load()
    at = time.time() if now is None else now
    try:
        seconds = max(0.0, float(interval_minutes) * 60)
    except (TypeError, ValueError):
        seconds = 3600.0
    out: list[str] = []
    for feed in data["feeds"]:
        last = feed.get("last_checked")
        try:
            last = float(last) if last is not None else None
        except (TypeError, ValueError):
            last = None
        if last is None or at - last >= seconds:
            out.append(feed.get("id", ""))
    return [fid for fid in out if fid]
