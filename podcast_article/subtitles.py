"""字幕下载与解析：VTT/SRT -> 带时间戳的片段列表。"""
from __future__ import annotations

import re

import requests

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})")
_TAG_RE = re.compile(r"<[^>]+>")
_SPEAKER_RE = re.compile(r"^\s*([\w\u4e00-\u9fff]{1,20}):\s*")


def _ts_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _clean_line(line: str) -> str:
    line = _TAG_RE.sub("", line)          # 去掉 <c>、<00:00:01.000> 等内联标签
    line = line.replace("&nbsp;", " ").replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<")
    return line.strip()


def _dedup_lines(lines: list[str]) -> list[str]:
    """YouTube 滚动字幕会把上一帧文本留在 cue 里，去掉连续重复。"""
    out: list[str] = []
    for line in lines:
        if line and (not out or line != out[-1]):
            out.append(line)
    return out


def _parse_vtt(text: str) -> list[dict]:
    segments: list[dict] = []
    cur_start = cur_end = None
    cur_lines: list[str] = []
    prev_lines: list[str] = []  # 上一条 cue 的文本行，用于去掉滚动字幕的重复

    def flush() -> None:
        nonlocal cur_start, cur_end, cur_lines, prev_lines
        if cur_start is None:
            return
        lines = _dedup_lines(cur_lines)
        # 滚动字幕：新 cue 的开头常会重复上一 cue 的结尾
        while lines and prev_lines and lines[0] in prev_lines:
            lines.pop(0)
        body = " ".join(lines).strip()
        if body:
            segments.append({"start": cur_start, "end": cur_end, "text": body})
        prev_lines = lines
        cur_start, cur_end, cur_lines = None, None, []

    for raw in text.splitlines():
        m = _TS_RE.search(raw)
        if m:
            flush()
            cur_start = _ts_to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
            cur_end = _ts_to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
            continue
        if raw.strip() == "":
            continue
        if raw.strip().isdigit() or raw.strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if cur_start is not None:
            line = _clean_line(raw)
            if line:
                cur_lines.append(line)
    flush()
    return segments


def _parse_srt(text: str) -> list[dict]:
    # SRT 与 VTT 的 cue 结构兼容，走同一解析器（先补一个 WEBVTT 头）
    return _parse_vtt("WEBVTT\n\n" + text)


def download_track(track_url: str, ext: str, timeout: float = 60.0) -> list[dict]:
    resp = requests.get(track_url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    text = resp.text
    return _parse_vtt(text) if ext == "vtt" else _parse_srt(text)
