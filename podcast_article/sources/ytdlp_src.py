"""YouTube / Bilibili 来源：yt-dlp 提取元信息 + 音频 + 字幕（网络抖动自动重试）。"""
from __future__ import annotations

import os
import re
import time
import hashlib
import http.cookiejar
import json
import urllib.request
import uuid
from typing import Callable, TypeVar

import yt_dlp
from yt_dlp.utils.networking import std_headers

from .base import Episode, SubtitleTrack

T = TypeVar("T")

# 字幕语言偏好：中文简体优先，其次其他中文、英文
_LANG_PREF = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK", "en", "en-US", "en-GB"]
_MAX_LANGS = 12
_BILIBILI_FINGER_URL = "https://api.bilibili.com/x/frontend/finger/spi"
_BILIBILI_HEADERS = {
    **std_headers,
    "Referer": "https://www.bilibili.com/",
    "Sec-CH-UA": '"Google Chrome";v="146", "Chromium";v="146", "Not)A;Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

# ---------------------------------------------------------------- 重试配置
# 环境变量名与 pipeline.py 一致（PA_DOWNLOAD_RETRIES / PA_DOWNLOAD_BACKOFF），
# 只是这里的默认退避更宽一点（3s、8s）：yt-dlp 一次失败的代价更高（还有分片与合并）。
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = "3,8"
SOCKET_TIMEOUT = 60.0  # 单次请求「多久没数据算挂」，yt-dlp 只接受单个 socket_timeout

# 确定性失败：视频不存在 / 私有 / 地区限制 / 需要会员 / 链接本身不支持 —— 重试没有任何意义
_FATAL_HINTS = (
    "video unavailable", "this video is unavailable", "video is not available",
    "private video", "this video is private", "members-only", "members only",
    "not available in your country", "not available in this country", "geo restricted",
    "has been removed", "has been deleted", "account has been terminated",
    "unsupported url", "is not a valid url", "no video formats found",
)

# 网络抖动：跨国链路最常见的几种（TLS 断连、握手超时、对端掐连接、分片下载中途 EOF）
_RETRYABLE_HINTS = (
    "timed out", "timeout", "temporary failure", "connection reset", "connection refused",
    "connection aborted", "connection error", "connection closed", "remote end closed",
    "server closed", "unable to connect", "cannot connect", "failed to establish a new connection",
    "max retries exceeded", "network is unreachable", "no route to host", "getaddrinfo",
    "name or service not known", "urlopen error", "unable to download webpage", "eof occurred",
    "incomplete read", "connection unexpectedly closed", "chunked", "content too short",
    "ssl", "handshake", "too many requests",
)


def _env_int(name: str, default: int) -> int:
    """读整数环境变量：缺省或非法时用默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _retries() -> int:
    """总尝试次数（PA_DOWNLOAD_RETRIES，默认 3）。"""
    return _env_int("PA_DOWNLOAD_RETRIES", DEFAULT_RETRIES)


def _waits() -> list[float]:
    """每次重试前的等待秒数（PA_DOWNLOAD_BACKOFF，默认 3,8；不够长时重复最后一个值）。"""
    raw = os.environ.get("PA_DOWNLOAD_BACKOFF", "").strip() or DEFAULT_BACKOFF
    waits: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            waits.append(max(0.0, float(part)))
        except ValueError:
            continue
    return waits or [float(p) for p in DEFAULT_BACKOFF.split(",") if p.strip()]


def _backoff_seconds(retry_no: int, waits: list[float]) -> float:
    """第 retry_no 次重试（从 1 开始）该等多久。"""
    if not waits:
        return 0.0
    return waits[min(retry_no - 1, len(waits) - 1)]


def _retryable(exc: BaseException | str) -> bool:
    """判断 yt-dlp 的这次失败值不值得重试（只做白名单判断，默认不重试）。

    判断依据（先看错误消息，再看异常类型）：
    1. 消息里带 HTTP 状态码时按状态码定：5xx / 429 是服务端瞬时故障 → 重试；
       其他 4xx（404 链接失效、403 无权限）→ 不重试，重试只是白等。
    2. 命中确定性提示（Video unavailable / Private video / 地区限制 / 需要会员）→ 不重试。
    3. 命中网络抖动关键词（timed out、Temporary failure、Connection reset、
       SSL handshake、IncompleteRead…）→ 重试，跨国链路的 TLS 断连多属这类。
    4. 其余一律不重试：参数错误、解析错误重试也不会变好，宽松重试只会把失败拖成几分钟。
    """
    low = str(exc).lower()

    m = re.search(r"http error\s+(\d{3})", low)
    if m:
        status = int(m.group(1))
        return status >= 500 or status == 429

    if any(h in low for h in _FATAL_HINTS):
        return False
    if any(h in low for h in _RETRYABLE_HINTS):
        return True
    # 消息里没线索时按类型兜底：超时/连接类异常本身就是网络问题
    return isinstance(exc, (TimeoutError, ConnectionError))


def _with_retries(what: str, fn: Callable[[], T], *, log=print, progress=None) -> T:
    """执行 fn()，只在网络类错误上重试（默认 3 次尝试，退避 3s / 8s）。"""
    attempts = _retries()
    waits = _waits()
    last: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not _retryable(exc):
                log(f"[audio] {what}失败（确定性错误，不重试）：{exc}")
                raise
            last = exc
            if attempt >= attempts:
                break
            wait = _backoff_seconds(attempt, waits)
            log(f"[audio] {what}第 {attempt} 次重试（共 {attempts} 次尝试）：{exc}；{wait:g} 秒后重试")
            if progress:
                # 字段名与 pipeline 保持一致：total_retries 表示总尝试次数
                progress("download", {
                    "retry": attempt,
                    "total_retries": attempts,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            time.sleep(wait)

    raise RuntimeError(f"{what}失败（已尝试 {attempts} 次）：{last}") from last


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


def _set_bilibili_cookie(cookiejar, name: str, value: str) -> None:
    cookiejar.set_cookie(http.cookiejar.Cookie(
        0, name, value, None, False, ".bilibili.com", True, True,
        "/", True, True, None, True, None, None, {},
    ))


def _prepare_bilibili_ytdlp(ydl, url: str) -> None:
    if _source(url) != "bilibili":
        return
    existing = {cookie.name for cookie in ydl.cookiejar}
    if "buvid_fp" not in existing:
        _set_bilibili_cookie(ydl.cookiejar, "buvid_fp", hashlib.md5(uuid.uuid4().bytes).hexdigest())


def _refresh_bilibili_fingerprint(ydl, *, timeout: float, log) -> bool:
    headers = dict(_BILIBILI_HEADERS)
    cookie_header = ydl.cookiejar.get_cookie_header("https://api.bilibili.com/")
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = urllib.request.Request(_BILIBILI_FINGER_URL, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        log(f"[bilibili] 指纹刷新失败（{type(exc).__name__}）")
        return False

    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("b_3") or not data.get("b_4"):
        log("[bilibili] 指纹接口未返回 buvid3 / buvid4")
        return False
    _set_bilibili_cookie(ydl.cookiejar, "buvid3", data["b_3"])
    _set_bilibili_cookie(ydl.cookiejar, "buvid4", data["b_4"])
    if "buvid_fp" not in {cookie.name for cookie in ydl.cookiejar}:
        _set_bilibili_cookie(ydl.cookiejar, "buvid_fp", hashlib.md5(uuid.uuid4().bytes).hexdigest())
    return True


def _extract_info(ydl, url: str, *, download: bool, timeout: float, log):
    _prepare_bilibili_ytdlp(ydl, url)
    try:
        return ydl.extract_info(url, download=download)
    except Exception as exc:
        message = str(exc).lower()
        if _source(url) != "bilibili" or "http error 412" not in message or "unable to download webpage" not in message:
            raise
        log("[bilibili] 网页请求被拒绝（HTTP 412），刷新匿名指纹后重试一次")
        if not _refresh_bilibili_fingerprint(ydl, timeout=timeout, log=log):
            raise
        return ydl.extract_info(url, download=download)


def probe(url: str, timeout: float = SOCKET_TIMEOUT, log=print) -> Episode:
    """只提取元信息与字幕地址，不下载（网络抖动自动重试，确定性错误直接抛）。"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "extract_flat": False,
        "retries": _retries(),
        "extractor_retries": _retries(),
    }
    if _source(url) == "bilibili":
        opts["http_headers"] = _BILIBILI_HEADERS

    def extract() -> dict | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return _extract_info(ydl, url, download=False, timeout=timeout, log=log)

    info = _with_retries("解析元信息", extract, log=log)
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


def download_audio(
    url: str,
    out_path_noext: str,
    timeout: float = SOCKET_TIMEOUT,
    progress=None,
    log=print,
) -> str:
    """下载最佳纯音频（yt-dlp 内层重试 + 外层退避重试），返回实际文件路径。"""
    attempts = _retries()
    opts = {
        "quiet": True,
        "no_warnings": True,
        # 读超时：600s 太久，链路挂掉后要等 10 分钟才报错；60s 内没有新数据就交给重试
        "socket_timeout": timeout,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": f"{out_path_noext}.%(ext)s",
        # 内层 HTTP 重试（yt-dlp 自己处理），外层再包一层更慢的退避重试
        "retries": attempts,
        "fragment_retries": attempts,  # HLS/DASH 分片单独失败也要重试
        "file_access_retries": attempts,
        "extractor_retries": attempts,
        "continuedl": True,  # 断点续传：重试接着已下载的部分下，不从头来
        "nopart": False,  # 保留 .part，才能跨进程续传
        "skip_unavailable_fragments": True,  # 个别分片彻底拿不到时，尽量保住其余音频
    }
    if _source(url) == "bilibili":
        opts["http_headers"] = _BILIBILI_HEADERS
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
        info = _with_retries(
            "下载音频", lambda: _extract_info(
                ydl, url, download=True, timeout=timeout, log=log,
            ),
            log=log, progress=progress,
        )
        if info is None:
            raise RuntimeError(f"yt-dlp 没有可下载的音频：{url}")
        # extract_info 返回的 filename 已含扩展名
        return ydl.prepare_filename(info)
