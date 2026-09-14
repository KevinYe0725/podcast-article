"""来源解析入口：给任意链接/路径，返回对应的 Episode 抓取器。"""
from __future__ import annotations

from pathlib import Path

from .base import Episode
from . import rss, xyz, ytdlp_src


def resolve(url: str, pick: int = 1) -> Episode:
    """识别链接类型并抓取单集元信息。"""
    url = url.strip()

    # 本地文件直接当音频
    p = Path(url).expanduser()
    if p.exists() and p.is_file():
        return Episode(
            source="file", url=str(p.resolve()), title=p.stem, podcast=p.stem
        )

    if xyz.is_xyz_episode(url):
        return xyz.fetch_episode(url)

    if rss.is_apple_podcasts_url(url):
        return rss.fetch_episode(url, pick=pick)

    if ytdlp_src.is_ytdlp_url(url):
        return ytdlp_src.probe(url)

    # 其他 http(s) 链接：尝试当 RSS 处理
    if url.startswith(("http://", "https://")):
        return rss.fetch_episode(url, pick=pick)

    raise ValueError(
        f"无法识别的链接：{url}\n支持：小宇宙单集 / YouTube / Bilibili / Apple Podcasts / RSS / 本地音视频文件"
    )
