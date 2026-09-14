"""YouTube / Bilibili 来源：yt-dlp 提取元信息 + 音频 + 字幕。"""
from __future__ import annotations

import yt_dlp

from .base import Episode, SubtitleTrack

# 字幕语言偏好：中文简体优先，其次其他中文、英文
_LANG_PREF = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK", "en", "en-US", "en-GB"]
_MAX_LANGS = 12


def is_ytdlp_url(url: str) -> bool:
    return any(
        host in url
        for host in (
            "youtube.com", "youtu.be", "bilibili.com", "b23.tv",
            "m.youtube.com", "music.youtube.com",
        )
    )


def _source(url: str) -> str:
    return "bilibili" if ("bilibili.com" in url or "b23.tv" in url) else "youtube"


def probe(url: str, timeout: float = 60.0) -> Episode:
    """只提取元信息与字幕地址，不下载。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ValueError(f"yt-dlp 无法解析该链接：{url}")

    tracks: list[SubtitleTrack] = []

    def collect(d: dict, auto: bool) -> None:
        for lang, fmts in (d or {}).items():
            if not fmts:
                continue
            # 优先 vtt；同一语言只留一个
            best = next((f for f in fmts if f.get("ext") == "vtt"), fmts[0])
            tracks.append(
                SubtitleTrack(lang=lang, url=best["url"], ext=best.get("ext", "vtt"), auto=auto)
            )

    collect(info.get("subtitles"), auto=False)
    collect(info.get("automatic_captions"), auto=True)

    def lang_rank(t: SubtitleTrack) -> tuple:
        base = t.lang.split("-")[0].lower()
        if t.lang in _LANG_PREF:
            pri = _LANG_PREF.index(t.lang)
        elif base in ("zh", "en"):
            pri = 50
        else:
            pri = 100
        # 人工字幕优先于自动字幕
        return (pri, 1 if t.auto else 0)

    tracks = sorted(tracks, key=lang_rank)[:_MAX_LANGS]

    uploader = info.get("uploader") or info.get("channel") or ""
    duration = info.get("duration")
    ts = info.get("timestamp") or info.get("release_timestamp")
    pub_date = None
    if ts:
        from datetime import datetime, timezone

        pub_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Episode(
        source=_source(url),
        url=info.get("webpage_url") or url,
        title=(info.get("title") or "").strip(),
        podcast=uploader,
        author=uploader,
        pub_date=pub_date,
        duration=float(duration) if duration else None,
        shownotes_html=(info.get("description") or None),
        audio_url=None,  # 由 yt-dlp 下载
        cover=info.get("thumbnail"),
        subtitle_tracks=tracks,
    )


def download_audio(url: str, out_path_noext: str, timeout: float = 600.0, progress=None) -> str:
    """下载最佳纯音频，返回实际文件路径。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": timeout,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": f"{out_path_noext}.%(ext)s",
        "retries": 3,
    }
    if progress:
        def hook(d):
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                progress("download", {
                    "pct": round(done / total * 100, 1),
                    "downloaded": done,
                    "total": total,
                })
        opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    # extract_info 返回的 filename 已含扩展名
    path = ydl.prepare_filename(info)
    return path
