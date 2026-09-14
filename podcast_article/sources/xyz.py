"""小宇宙单集抓取：免登录解析单集页面 __NEXT_DATA__ 里的 JSON。"""
from __future__ import annotations

import json
import re

import requests

from .base import Episode

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def is_xyz_episode(url: str) -> bool:
    return "xiaoyuzhoufm.com/episode/" in url


def _parse_next_data(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("页面里没有找到 __NEXT_DATA__，小宇宙可能改版了")
    return json.loads(m.group(1))


def fetch_episode(url: str, timeout: float = 30.0) -> Episode:
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    data = _parse_next_data(resp.text)
    ep = data.get("props", {}).get("pageProps", {}).get("episode")
    if not ep:
        raise ValueError("__NEXT_DATA__ 里没有 episode 字段，小宇宙可能改版了")

    enclosure = ep.get("enclosure") or {}
    podcast = ep.get("podcast") or {}
    authors = podcast.get("authors") or []
    author_names = "、".join(a.get("nickname", "") for a in authors if a.get("nickname"))

    return Episode(
        source="xiaoyuzhou",
        url=url,
        title=(ep.get("title") or "").strip(),
        podcast=(podcast.get("title") or "").strip(),
        author=author_names or (podcast.get("author") or ""),
        pub_date=ep.get("pubDate"),
        duration=ep.get("duration"),
        shownotes_html=ep.get("shownotes") or None,
        audio_url=enclosure.get("url"),
        cover=(podcast.get("image") or {}).get("middlePic") or (podcast.get("image") or {}).get("smallPic"),
    )
