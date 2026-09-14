"""通用工具：slug、时间格式化、shownotes 清洗。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone


def human_time(seconds: float | int | None) -> str:
    """秒 -> h:mm:ss 或 m:ss。"""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def ts_clock(seconds: float | int) -> str:
    """统一 [hh:mm:ss] 格式的时间戳（用于文字稿行首）。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_SLUG_KEEP = re.compile(r"[\w\u4e00-\u9fff\u3040-\u30ff-]+")


def slugify(text: str, max_len: int = 40) -> str:
    """生成安全的目录名：保留中英文数字，其余折叠成 '-'。"""
    text = unicodedata.normalize("NFKC", text or "")
    parts = _SLUG_KEEP.findall(text)
    slug = "-".join(parts).strip("-") or "untitled"
    return slug[:max_len].strip("-") or "untitled"


def episode_slug(podcast: str, title: str, pub_date: str | None, max_len: int = 72) -> str:
    date_part = ""
    if pub_date:
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            date_part = dt.astimezone(timezone.utc).strftime("%Y%m%d-")
        except ValueError:
            date_part = ""
    body = slugify(f"{podcast}-{title}")
    return f"{date_part}{body}"[:max_len].strip("-") or "episode"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def html_to_text(html: str | None) -> str:
    """shownotes HTML -> 可读纯文本（给 LLM 当上下文）。"""
    if not html:
        return ""
    text = html.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&lt;", "<")
    )
    lines = []
    for line in text.splitlines():
        line = _WS_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    # 合并连续空行
    out: list[str] = []
    for line in lines:
        if out and line == out[-1]:
            continue
        out.append(line)
    return "\n".join(out)
