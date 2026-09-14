"""RSS / Apple Podcasts 来源。

- 直接给 feed URL：默认取最新一集（--pick N 选第 N 新）
- 给单集页面链接：在 feed 里按链接匹配
- 给 podcasts.apple.com 链接：先通过 iTunes lookup 换取 feed URL
"""
from __future__ import annotations

import re
import time as _time

import feedparser
import requests

from .base import Episode

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_ITUNES_RE = re.compile(r"[?&]id=(\d+)")


def is_apple_podcasts_url(url: str) -> bool:
    return "podcasts.apple.com" in url


def _apple_to_feed_url(url: str, timeout: float = 20.0) -> str:
    m = _ITUNES_RE.search(url)
    if not m:
        raise ValueError("Apple Podcasts 链接里没有 id 参数")
    resp = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": m.group(1)},
        headers={"User-Agent": _UA},
        timeout=timeout,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    feed = results[0].get("feedUrl") if results else None
    if not feed:
        raise ValueError("iTunes lookup 没有返回 feedUrl")
    return feed


def _entry_audio(entry) -> str | None:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href")
        if href:
            return href
    for mc in entry.get("media_content", []) or []:
        if mc.get("url"):
            return mc["url"]
    return None


def _entry_shownotes(entry) -> str | None:
    content = entry.get("content") or []
    if content and content[0].get("value"):
        return content[0]["value"]
    return entry.get("summary") or None


def _entry_duration(entry) -> float | None:
    raw = entry.get("itunes_duration")
    if not raw:
        return None
    raw = str(raw).strip()
    parts = [p for p in raw.split(":") if p != ""]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec or None


def fetch_episode(
    url: str,
    pick: int = 1,
    timeout: float = 30.0,
) -> Episode:
    """url 可以是 feed 链接或 feed 里的单集链接。pick 从 1 开始（1=最新）。"""
    feed_url = _apple_to_feed_url(url) if is_apple_podcasts_url(url) else url
    parsed = feedparser.parse(feed_url, request_headers={"User-Agent": _UA})
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败：{parsed.bozo_exception}")
    if not parsed.entries:
        raise ValueError("这个 feed 里没有任何单集")

    show = parsed.feed.get("title", "")
    author = parsed.feed.get("author") or parsed.feed.get("itunes_author") or ""

    # 尝试按单集链接精确匹配
    target = None
    if "xiaoyuzhoufm.com" not in feed_url:  # 单集页链接匹配（排除 feed 自身）
        for entry in parsed.entries:
            links = [entry.get("link", "")] + [l.get("href", "") for l in entry.get("links", [])]
            if url.rstrip("/") in (l.rstrip("/") for l in links if l):
                target = entry
                break

    if target is None:
        idx = max(0, pick - 1)
        if idx >= len(parsed.entries):
            raise ValueError(f"--pick {pick} 超出范围：feed 只有 {len(parsed.entries)} 集")
        target = parsed.entries[idx]

    audio = _entry_audio(target)
    if not audio:
        raise ValueError("该单集没有音频附件（enclosure）")

    published = target.get("published_parsed")
    cover = None
    feed_image = parsed.feed.get("image") or {}
    if isinstance(feed_image, dict):
        cover = feed_image.get("href")

    return Episode(
        source="rss",
        url=target.get("link") or url,
        title=(target.get("title") or "").strip(),
        podcast=show,
        author=author or target.get("author", ""),
        pub_date=_time.strftime("%Y-%m-%dT%H:%M:%SZ", published) if published else None,
        duration=_entry_duration(target),
        shownotes_html=_entry_shownotes(target),
        audio_url=audio,
        cover=cover,
    )
