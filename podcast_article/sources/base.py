"""来源层数据模型：一集播客/视频的元信息。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class SubtitleTrack:
    lang: str
    url: str
    ext: str          # vtt / srt
    auto: bool        # 是否机器自动字幕


@dataclass
class Episode:
    source: str                     # xiaoyuzhou / rss / youtube / bilibili / file
    url: str                        # 单集页面链接
    title: str = ""
    podcast: str = ""               # 节目名
    author: str = ""                # 主播
    pub_date: str | None = None     # ISO 字符串
    duration: float | None = None   # 秒
    shownotes_html: str | None = None
    audio_url: str | None = None    # 音频直链（已知时直接下载）
    cover: str | None = None
    subtitle_tracks: list[SubtitleTrack] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Episode":
        tracks = [SubtitleTrack(**t) for t in d.pop("subtitle_tracks", [])]
        ep = cls(**d)
        ep.subtitle_tracks = tracks
        return ep
