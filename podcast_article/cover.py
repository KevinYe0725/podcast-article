"""单集封面图：下载到本地，供文章卡片展示。

为什么**下载到本地**而不是直接引用外链（这是设计决定，不是偷懒）：

1. 这是个本地优先的工具 —— 断网、墙、CDN 抖动都不该让界面变成一堆碎图
2. 图床普遍有防盗链，今天能热链不代表明天能（B 站就要求 Referer），
   而**服务端带 UA 取一次**比浏览器热链稳得多
3. 卡片列表会一次渲染十几张图，让浏览器去打第三方 CDN 既慢又暴露阅读习惯

落盘位置：`output/<slug>/cover.<ext>`，扩展名按响应的 Content-Type 定
（YouTube 给的是 webp，硬写成 .jpg 再用 image/jpeg 发出去会显示不出来）。

只存不改：拿到的字节原样落盘，不做缩放/转码 —— 转码要引 Pillow，
而 Pillow 目前只是开发依赖（生成海报用），不该为封面变成运行依赖。
"""
from __future__ import annotations

import re
from pathlib import Path

import requests

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
CONNECT_TIMEOUT = 8.0
READ_TIMEOUT = 20.0

# 认这些扩展名（也是 /api/cover 允许发的类型）
EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
ALLOWED_EXTS = tuple(sorted(set(EXT_BY_TYPE.values())))
MAX_BYTES = 8 * 1024 * 1024        # 单张上限，防止误抓到大文件


def find(workdir: Path) -> Path | None:
    """找这一集已有的本地封面（按扩展名优先序返回第一个）。"""
    base = Path(workdir)
    for ext in ALLOWED_EXTS:
        p = base / f"cover{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def mimetype(path: Path) -> str:
    """按扩展名给 MIME（发文件用）。"""
    ext = Path(path).suffix.lower()
    for mime, known in EXT_BY_TYPE.items():
        if known == ext:
            return mime
    return "application/octet-stream"


def _ext_from(resp: requests.Response, url: str) -> str:
    """从 Content-Type 推断扩展名；没有就退回 URL 后缀；再不行按 jpg。"""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    suffix = Path(re.split(r"[?#]", url or "")[0]).suffix.lower()
    if suffix in ALLOWED_EXTS:
        return suffix
    return ".jpg"


def fetch(url: str, workdir: Path) -> Path | None:
    """把封面下载到 workdir/cover.<ext>。

    **不抛异常**：封面拿不到只是卡片少一张图，绝不能让整条流水线失败。
    失败返回 None，原因由调用方记日志。
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": _UA},
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        resp.raise_for_status()
        content = resp.content
    except Exception:
        return None
    if not content or len(content) > MAX_BYTES:
        return None
    ext = _ext_from(resp, url)
    dest = Path(workdir) / f"cover{ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        tmp.write_bytes(content)
        tmp.replace(dest)                 # 原子落盘：半截文件会让卡片显示碎图
    except OSError:
        tmp.unlink(missing_ok=True)
        return None
    # 换个扩展名重下过（例如上次存的是 .jpg，这次拿到 webp）：清掉旧的，避免 find() 拿到旧图
    for other in ALLOWED_EXTS:
        stale = Path(workdir) / f"cover{other}"
        if stale != dest:
            stale.unlink(missing_ok=True)
    return dest


def ensure(url: str | None, workdir: Path) -> Path | None:
    """已有就复用，没有才下载。返回本地路径或 None。"""
    existing = find(workdir)
    if existing:
        return existing
    if not url:
        return None
    return fetch(url, workdir)
