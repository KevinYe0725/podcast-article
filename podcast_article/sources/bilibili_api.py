"""Bilibili public API metadata and direct audio URL handling."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .base import Episode

_API = "https://api.bilibili.com"
SOCKET_TIMEOUT = 30.0
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
_WBI_MIXIN_KEY_ORDER = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


class _IncompleteDownload(ConnectionError):
    """Retryable transport failure while receiving a media range."""


def _get_json(path: str, params: dict | None = None, *, timeout: float = SOCKET_TIMEOUT) -> dict:
    response = requests.get(
        f"{_API}{path}", params=params, headers=_HEADERS, timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("B 站接口返回了非 JSON 对象")
    return payload


def _api_error(payload: dict) -> str:
    code = payload.get("code", "unknown")
    message = str(payload.get("message") or payload.get("msg") or "接口拒绝")
    return f"B 站接口拒绝（code={code}，{message}）"


def parse_bvid(url: str, *, timeout: float = SOCKET_TIMEOUT) -> str | None:
    """Extract a BV/av identifier, resolving b23.tv short links when needed."""
    current_url = str(url or "").strip()
    parsed = urlparse(current_url)
    host = (parsed.hostname or "").lower()

    if host == "b23.tv" or host.endswith(".b23.tv"):
        response = requests.get(
            current_url, headers=_HEADERS, timeout=timeout, allow_redirects=True,
        )
        response.raise_for_status()
        current_url = response.url
        parsed = urlparse(current_url)
        host = (parsed.hostname or "").lower()

    if host not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        return None

    match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)(?:/|$)", parsed.path, re.I)
    if not match:
        return None
    identifier = match.group(1)
    return "BV" + identifier[2:] if identifier[:2].lower() == "bv" else "av" + identifier[2:]


def view(identifier: str, *, timeout: float = SOCKET_TIMEOUT) -> dict:
    """Return public video metadata using the non-WBI view endpoint."""
    identifier = str(identifier).strip()
    if re.fullmatch(r"av\d+", identifier, re.I):
        params = {"aid": identifier[2:]}
    elif re.fullmatch(r"BV[0-9A-Za-z]{10}", identifier, re.I):
        params = {"bvid": "BV" + identifier[2:]}
    else:
        raise ValueError("B 站视频编号格式不正确")

    payload = _get_json("/x/web-interface/view", params, timeout=timeout)
    if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ValueError(_api_error(payload))
    return payload["data"]


def _wbi_key(nav_data: dict) -> str:
    wbi = (nav_data.get("data") or {}).get("wbi_img") or {}
    names = []
    for field in ("img_url", "sub_url"):
        path = urlparse(str(wbi.get(field) or "")).path
        filename = path.rsplit("/", 1)[-1]
        key = filename.rsplit(".", 1)[0]
        if not key:
            raise ValueError("B 站接口没有返回 WBI 签名密钥")
        names.append(key)
    lookup = "".join(names)
    if len(lookup) <= max(_WBI_MIXIN_KEY_ORDER):
        raise ValueError("B 站返回的 WBI 签名密钥长度不足")
    return "".join(lookup[index] for index in _WBI_MIXIN_KEY_ORDER)[:32]


def _sign_wbi(params: dict, mixin_key: str, *, now: int | None = None) -> dict:
    signed = dict(params)
    signed["wts"] = int(time.time() if now is None else now)
    cleaned = {
        key: "".join(character for character in str(value) if character not in "!'()*")
        for key, value in sorted(signed.items())
    }
    query = urlencode(cleaned)
    cleaned["w_rid"] = hashlib.md5(f"{query}{mixin_key}".encode("utf-8")).hexdigest()
    return cleaned


def playurl(bvid: str, cid: int, *, timeout: float = SOCKET_TIMEOUT) -> list[dict]:
    """Return audio tracks; retry the endpoint with WBI signing if needed."""
    params = {"bvid": bvid, "cid": int(cid), "fnval": 16, "fnver": 0, "fourk": 1}
    first = _get_json("/x/player/playurl", params, timeout=timeout)
    payload = first

    if first.get("code") != 0:
        nav = _get_json("/x/web-interface/nav", timeout=timeout)
        signed = _sign_wbi(params, _wbi_key(nav))
        payload = _get_json("/x/player/wbi/playurl", signed, timeout=timeout)
        if payload.get("code") != 0:
            raise ValueError(_api_error(payload))

    data = payload.get("data") or {}
    tracks = ((data.get("dash") or {}).get("audio") or [])
    tracks = [track for track in tracks if isinstance(track, dict)]
    if not tracks:
        raise ValueError("B 站播放接口没有返回可用音频轨道")
    return tracks


def pick_audio(tracks: list[dict]) -> dict:
    """Prefer AAC 30280, otherwise choose the highest-bandwidth audio track."""
    candidates = [track for track in tracks if isinstance(track, dict)]
    if not candidates:
        raise ValueError("B 站播放接口没有返回可用音频轨道")
    preferred = next((track for track in candidates if str(track.get("id")) == "30280"), None)
    if preferred:
        return preferred
    return max(candidates, key=lambda track: float(track.get("bandwidth") or 0))


def probe(url: str, *, timeout: float = SOCKET_TIMEOUT, log=print) -> Episode:
    """Build an Episode from Bilibili APIs without requesting its video webpage."""
    identifier = parse_bvid(url, timeout=timeout)
    if identifier is None:
        raise ValueError("无法从链接中识别 B 站视频编号")
    info = view(identifier, timeout=timeout)
    pages = info.get("pages") or []
    query = parse_qs(urlparse(url).query)
    page_number = int(query.get("p", ["1"])[0] or 1)
    if pages:
        if page_number < 1 or page_number > len(pages):
            raise ValueError(f"B 站视频不存在第 {page_number} 个分 P")
        page = pages[page_number - 1]
    else:
        page = {"cid": info.get("cid"), "duration": info.get("duration")}

    cid = page.get("cid") or info.get("cid")
    resolved_bvid = info.get("bvid") or (identifier if identifier.startswith("BV") else None)
    if not cid or not resolved_bvid:
        raise ValueError("B 站视频元信息缺少 cid 或 bvid")
    track = pick_audio(playurl(resolved_bvid, cid, timeout=timeout))
    audio_url = track.get("baseUrl") or track.get("base_url") or track.get("url")
    if not isinstance(audio_url, str) or not audio_url.startswith(("https://", "http://")):
        raise ValueError("B 站音频轨道缺少有效直链")

    title = str(info.get("title") or "").strip()
    part = str(page.get("part") or "").strip()
    if part and part != title:
        title = f"{title} - {part}" if title else part
    owner = info.get("owner") or {}
    timestamp = info.get("pubdate") or info.get("ctime")
    pub_date = None
    try:
        if timestamp:
            pub_date = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        pub_date = None

    try:
        duration = float(page.get("duration") or info.get("duration"))
    except (TypeError, ValueError, OverflowError):
        duration = None

    return Episode(
        source="bilibili",
        url=url,
        title=title,
        podcast=str(owner.get("name") or "").strip(),
        author=str(owner.get("name") or "").strip(),
        pub_date=pub_date,
        duration=duration,
        shownotes_html=info.get("desc") or None,
        audio_url=audio_url,
        cover=info.get("pic"),
        subtitle_tracks=[],
    )


def _content_range(value: str | None) -> tuple[int | None, int | None]:
    match = re.match(r"bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)", str(value or "").strip(), re.I)
    if not match:
        return None, None
    start = int(match.group(1)) if match.group(1) else None
    total = int(match.group(3)) if match.group(3) and match.group(3) != "*" else None
    return start, total


def _int_header(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _download_once(
    audio_url: str,
    destination: Path,
    partial: Path,
    *,
    timeout: float,
    progress=None,
    log=print,
) -> Path:
    offset = partial.stat().st_size if partial.exists() else 0
    headers = dict(_HEADERS)
    if offset:
        headers["Range"] = f"bytes={offset}-"

    with requests.get(audio_url, headers=headers, stream=True, timeout=(15.0, timeout)) as response:
        if response.status_code == 416:
            _, total = _content_range(response.headers.get("Content-Range"))
            if offset and total and offset == total:
                partial.replace(destination)
                return destination
            partial.unlink(missing_ok=True)
            raise _IncompleteDownload("B 站 CDN 拒绝断点续传，已清理损坏的临时文件")
        response.raise_for_status()

        content_length = _int_header(response.headers.get("Content-Length"))
        if offset and response.status_code == 206:
            start, range_total = _content_range(response.headers.get("Content-Range"))
            if start != offset:
                partial.unlink(missing_ok=True)
                raise _IncompleteDownload("B 站 CDN 返回的续传起点不匹配，已清理临时文件")
            mode, done = "ab", offset
        else:
            if offset:
                log(f"[audio] B 站 CDN 不支持续传（HTTP {response.status_code}），从头下载")
            mode, done, range_total = "wb", 0, None
        total = range_total or (done + content_length if content_length is not None else 0)
        received = 0
        with partial.open(mode) as stream:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                stream.write(chunk)
                received += len(chunk)
                done += len(chunk)
                if progress:
                    progress("download", {
                        "pct": round(done / total * 100, 1) if total else None,
                        "downloaded": done,
                        "total": total or None,
                    })

    if content_length is not None and received != content_length:
        raise _IncompleteDownload(
            f"B 站音频下载不完整：Content-Length={content_length}，实际收到 {received} 字节"
        )
    size = partial.stat().st_size
    if total and size != total:
        raise _IncompleteDownload(f"B 站音频下载不完整：期望 {total} 字节，实际收到 {size} 字节")
    partial.replace(destination)
    return destination


def download(audio_url: str, dest_noext: str, *, progress=None, timeout: float = SOCKET_TIMEOUT, log=print) -> str:
    """Download a Bilibili fMP4/AAC audio stream to an .m4a file with Range resume."""
    destination = Path(f"{dest_noext}.m4a")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    # Use the shared retry policy without importing ytdlp_src during module load.
    from .ytdlp_src import _with_retries

    result = _with_retries(
        "下载 B 站音频",
        lambda: _download_once(
            audio_url, destination, partial, timeout=timeout, progress=progress, log=log,
        ),
        log=log,
        progress=progress,
    )
    return str(result)
