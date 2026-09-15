"""导出：把 output/<slug>/ 里的一集变成可以带走的文件。

四个公开函数（webapp 的下载接口按这些名字调用，改名会直接打断前端）：

    article_markdown(root, dir_name) -> (文件名, 内容)   单篇 Markdown（带 YAML front matter）
    article_html(root, dir_name, *, title=...) -> str    一份能单独打开的完整 HTML
    bundle(root, dest, *, dirs=None, include_transcript=True) -> dict
                                                        多篇打包成一个 zip（含目录页 README.md）
    export_episode(root, dir_name, fmt) -> (文件名, bytes, MIME)
                                                        统一入口，fmt ∈ {"md", "html", "txt"}

设计约定：

- root 由调用方传入（webapp 用 OUTPUT_ROOT，可用 PA_OUTPUT_DIR 覆盖），本模块不自己读环境变量。
- 只用已声明的依赖（markdown），不引 pyyaml / PDF / EPUB：front matter 是手写生成的，
  用双引号标量 + 反斜杠转义，天然合法（YAML 1.2 是 JSON 的超集）。
- 所有函数都容错：meta.json 缺失、损坏、字段类型奇怪都当成空值处理；
  只有「这一集/这份文件不存在」才抛 ValueError。
- 导出的 HTML 不引用任何外部资源（不引用 /static/），因为用户会把它单独发给别人。
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import datetime
from html import escape as _escape
from pathlib import Path
from urllib.parse import quote, urlparse

import markdown

from .util import human_time

# 文件名（含后缀）的字符数上限：中文按 1 个字符算；Windows 全路径上限之外还要留出空间
MAX_FILENAME = 80

# 正文里的 [hh:mm:ss]（与 webapp 的 _TS_RE 一致）
_TS_RE = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\]")

# 文件名里绝对不允许的字符：路径分隔符、Windows 保留字符、控制字符与换行
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')

# Markdown 渲染扩展：与 webapp._md_to_html 保持一致，同一个 .md 两边看起来一样
_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]

# 下载时用的 MIME
MIME = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}

# front matter 的字段顺序（导出给 Obsidian / Hugo / 静态站都用得上）
FRONT_MATTER_KEYS = ("title", "podcast", "author", "date", "duration", "url", "source")


# ---------------------------------------------------------------- 读数据（全部容错）


def _load_meta(base: Path) -> dict:
    """读 meta.json：文件不存在、JSON 坏了、根不是对象，一律返回空 dict。"""
    try:
        data = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _episode_dir(root: Path | str, dir_name: str) -> Path:
    """定位一集目录；顺便挡掉 dir_name 里的路径穿越（webapp 直接把请求参数传进来）。"""
    name = (dir_name or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise ValueError(f"文章不存在：{dir_name}")
    base = Path(root) / name
    if not base.is_dir():
        raise ValueError(f"文章不存在：{dir_name}")
    return base


def _read_article(base: Path, dir_name: str) -> str:
    """读 article.md；没有这份文件就是「文章不存在」。"""
    path = base / "article.md"
    if not path.is_file():
        raise ValueError(f"文章不存在（缺少 article.md）：{dir_name}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"文章读取失败：{dir_name}（{exc}）") from exc


def _title_of(meta: dict, dir_name: str) -> str:
    """标题：meta 里的 title，没了就退回目录名（绝不给空字符串）。"""
    return str(meta.get("title") or "").strip() or dir_name


# ---------------------------------------------------------------- YAML 转义


def _yaml_str(value: object) -> str:
    """把任意值渲染成 YAML 的**双引号标量**。

    为什么一律加引号而不是「能裸写就裸写」：标题里出现 `: `、`#`、`-` 开头、
    首尾空格都会改变 YAML 结构，判断起来很容易漏；双引号标量把这些全兜住。

    转义规则（与 JSON 字符串一致，所以产物对 JSON/YAML 两种解析器都合法）：
    `\\` → `\\\\`，`"` → `\\"`，换行/回车/制表符 → `\\n` / `\\r` / `\\t`，
    其余控制字符 → `\\uXXXX`。换行必须转义成两个字符，否则一条字段会被撑成两行。
    """
    text = "" if value is None else str(value)
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _date_text(raw: object) -> str:
    """pub_date → YYYY-MM-DD；解析不出来就原样返回（不抛异常）。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


def _duration_text(raw: object) -> str:
    """时长：能转成秒数就写数字，否则原样文本（给 "1:01:01" 这类可读值留条路）。"""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, (int, float)):
        return str(int(raw))
    return str(raw).strip()


def _duration_yaml(raw: object) -> str:
    """时长在 front matter 里：纯秒数不加引号（便于工具消费），其余走 _yaml_str。"""
    text = _duration_text(raw)
    return text if text.isdigit() else _yaml_str(text)


def _source_of(meta: dict) -> str:
    """来源：meta.source 优先，没有就退回链接的域名。"""
    src = str(meta.get("source") or "").strip()
    if src:
        return src
    return urlparse(str(meta.get("url") or "").strip()).netloc


def _front_matter(meta: dict, dir_name: str) -> str:
    """拼出 `---` 包裹的 YAML front matter（含结尾空行）。"""
    values = {
        "title": _yaml_str(_title_of(meta, dir_name)),
        "podcast": _yaml_str(meta.get("podcast")),
        "author": _yaml_str(meta.get("author")),
        "date": _yaml_str(_date_text(meta.get("pub_date"))),
        "duration": _duration_yaml(meta.get("duration")),
        "url": _yaml_str(meta.get("url")),
        "source": _yaml_str(_source_of(meta)),
    }
    lines = ["---"]
    lines += [f"{key}: {values[key]}" for key in FRONT_MATTER_KEYS]
    lines.append("---")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------- 文件名安全化


def _clean(text: object) -> str:
    """去掉非法字符与首尾空格/点（首尾的点在 Windows 上会害死文件创建）。"""
    cleaned = _ILLEGAL_RE.sub("", "" if text is None else str(text))
    cleaned = re.sub(r"\s+", " ", cleaned)   # 换行已被清掉，这里收拢连续空白
    return cleaned.strip(" .")


def _compose_stem(meta: dict, dir_name: str) -> str:
    """`<podcast>-<title>` → 安全主干；清理后为空就回退成目录名。"""
    podcast = _clean(meta.get("podcast"))
    title = _clean(meta.get("title"))
    if podcast and title:
        stem = f"{podcast}-{title}"
    else:
        stem = podcast or title
    stem = _clean(stem) or _clean(dir_name)
    return stem or "episode"


def _stem_with_suffix(stem: str, suffix: str) -> str:
    """截断主干，保证「主干+后缀」不超过 MAX_FILENAME 个字符（中文按 1 个算）。"""
    return stem[: max(1, MAX_FILENAME - len(suffix))].strip(" .") or "episode"


def _file_stem(base: Path, dir_name: str) -> str:
    return _compose_stem(_load_meta(base), dir_name)


# ---------------------------------------------------------------- Markdown → HTML


def _wrap_timestamps(html: str) -> str:
    """把正文里的 `[hh:mm:ss]` 包成 `<span class="ts">`；代码块里原样保留。

    逐段处理：先按标签切开，只在标签之外做替换，避免动了 href/title 之类的属性。
    """
    out: list[str] = []
    depth = 0                      # <pre>/<code> 的嵌套深度，>0 时不动
    for chunk in re.split(r"(<[^>]*>)", html):
        if chunk.startswith("<") and chunk.endswith(">"):
            tag = chunk.lower()
            if tag.startswith("<pre") or tag.startswith("<code"):
                depth += 1
            elif tag.startswith("</pre") or tag.startswith("</code"):
                depth = max(0, depth - 1)
            out.append(chunk)
        elif depth:
            out.append(chunk)
        else:
            out.append(_TS_RE.sub(lambda m: f'<span class="ts">{m.group(0)}</span>', chunk))
    return "".join(out)


def _md_to_html(text: str) -> str:
    """与 webapp._md_to_html 同一套扩展，再加时间戳弱化处理。"""
    rendered = markdown.markdown(text, extensions=_MD_EXTENSIONS)
    return _wrap_timestamps(rendered)


def _header_html(meta: dict) -> str:
    """正文前的一行元信息：播客 · 作者 · 日期 · 时长 · 原文链接。"""
    duration = meta.get("duration")
    bits = [
        str(meta.get("podcast") or "").strip(),
        str(meta.get("author") or "").strip(),
        _date_text(meta.get("pub_date")),
        human_time(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else "",
    ]
    parts = [_escape(b) for b in bits if b and b != "?"]
    url = str(meta.get("url") or "").strip()
    if url:
        parts.append(f'<a href="{_escape(url, quote=True)}">原文链接</a>')
    if not parts:
        return ""
    return '<p class="meta">' + " · ".join(parts) + "</p>"


# 内联样式：中文正文（720px 宽、1.75 行高、系统字体栈）+ 引用/表格/代码块
_CSS = """    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0 auto;
      padding: 48px 20px 96px;
      max-width: 720px;
      color: #1f2328;
      background: #ffffff;
      font-size: 17px;
      line-height: 1.75;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans SC",
        "Noto Sans CJK SC", "WenQuanYi Micro Hei", sans-serif;
      -webkit-text-size-adjust: 100%;
    }
    h1 { font-size: 1.75em; line-height: 1.35; margin: 0 0 .7em; }
    h2 { font-size: 1.35em; margin: 1.9em 0 .6em; padding-bottom: .25em; border-bottom: 1px solid #eceff2; }
    h3 { font-size: 1.15em; margin: 1.5em 0 .5em; }
    p { margin: 0 0 1.1em; }
    a { color: #0b62d6; }
    ul, ol { padding-left: 1.4em; }
    li { margin: .3em 0; }
    blockquote {
      margin: 1.2em 0; padding: .7em 1.1em;
      border-left: 3px solid #10a37f; border-radius: 0 6px 6px 0;
      background: #f6f8f7; color: #3c4043;
    }
    blockquote p:last-child { margin-bottom: 0; }
    code {
      padding: .15em .35em; border-radius: 4px; background: #f2f4f6;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: .92em;
    }
    pre { padding: 14px 16px; border-radius: 8px; overflow-x: auto; background: #f6f8fa; line-height: 1.6; }
    pre code { padding: 0; background: none; font-size: .9em; }
    table { border-collapse: collapse; width: 100%; margin: 1.2em 0; font-size: .95em; }
    th, td { border: 1px solid #e3e6ea; padding: 8px 12px; text-align: left; vertical-align: top; }
    th { background: #f6f8fa; font-weight: 600; }
    hr { border: none; border-top: 1px solid #e6e8eb; margin: 2em 0; }
    img { max-width: 100%; height: auto; }
    /* 时间戳弱化：可读但不抢正文注意力 */
    .ts { color: #8a9099; font-size: .88em; font-variant-numeric: tabular-nums; white-space: nowrap; }
    .meta { margin: 0 0 2em; padding-bottom: 1em; border-bottom: 1px solid #eceff2;
            color: #6b7280; font-size: .9em; line-height: 1.6; }
    .meta a { color: #6b7280; }
    @media (prefers-color-scheme: dark) {
      body { color: #e6e6e6; background: #1b1d21; }
      h2 { border-bottom-color: #2c2f34; }
      a { color: #7cb0ff; }
      blockquote { background: #22252a; color: #c6c9ce; border-left-color: #10a37f; }
      code, pre, th { background: #24272c; }
      th, td { border-color: #33373d; }
      .ts { color: #8b9199; }
      .meta { border-bottom-color: #2c2f34; }
    }
    @media print { body { max-width: none; padding: 0; } }"""

# 完整文档骨架（用 {{name}} 占位，单次替换，正文里的 {{...}} 不会被二次解释）
_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title>
<style>
{{css}}
</style>
</head>
<body>
<main>
{{header}}
{{body}}
</main>
</body>
</html>
"""


def _render(template: str, **values: str) -> str:
    """单次替换 `{{name}}`（re.sub 不会重扫插入进来的内容）。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), ""), template)


# ---------------------------------------------------------------- 公开接口


def article_markdown(root: Path, dir_name: str) -> tuple[str, str]:
    """返回 (文件名, 内容)：article.md 前面加上 YAML front matter 的单篇 Markdown。

    文件名形如 `<podcast>-<title>.md`，主干做了安全化与长度截断（含后缀 ≤ 80 字符）。
    dir_name 不存在或没有 article.md 时抛 ValueError。
    """
    base = _episode_dir(root, dir_name)
    body = _read_article(base, dir_name)
    meta = _load_meta(base)
    stem = _file_stem(base, dir_name)
    name = _stem_with_suffix(stem, ".md") + ".md"
    content = _front_matter(meta, dir_name) + body.lstrip("\n")
    return name, content


def article_html(root: Path, dir_name: str, *, title: str) -> str:
    """返回一份完整可独立打开的 HTML 文档（内联 CSS，不引用任何外部资源）。

    - 页面 title 用传入的 title；正文里没有一级标题时补一个 <h1>。
    - `[hh:mm:ss]` 渲染成 <span class="ts">（样式上弱化，不要求可点）。
    - 引用块、表格、代码块都有样式；中文按 720px 宽、1.75 行高排版。
    dir_name 不存在或没有 article.md 时抛 ValueError。
    """
    base = _episode_dir(root, dir_name)
    body = _read_article(base, dir_name)
    meta = _load_meta(base)
    inner = _md_to_html(body)
    if "<h1" not in inner.lower():
        inner = f"<h1>{_escape(title)}</h1>\n" + inner
    return _render(
        _HTML_TMPL,
        title=_escape(title),
        css=_CSS,
        header=_header_html(meta),
        body=inner,
    )


def _unique(name: str, used: set[str]) -> str:
    """同名条目加序号（不同播客的同名标题很常见）。"""
    if name not in used:
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    for i in range(2, 1000):
        candidate = f"{stem}-{i}.{suffix}" if suffix else f"{stem}-{i}"
        if candidate not in used:
            return candidate
    return f"{name}-{len(used)}"


def _writestr_utf8(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    """写一个条目，并显式打开 UTF-8 文件名标志。

    general purpose bit 11（0x800）就是在告诉解压端「文件名按 UTF-8 解码」；
    不设的话 Windows 自带解压会按本地代码页读，中文名直接乱码。
    """
    info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
    info.flag_bits |= 0x800
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16          # 普通文件，权限 644
    zf.writestr(info, data)


def _select_dirs(root: Path, dirs: list[str] | None) -> list[str]:
    """挑出要打包的目录名；坏掉的（不存在 / 没有 article.md）静默跳过，不拖垮整批导出。"""
    if dirs:
        picked: list[str] = []
        for raw in dirs:
            name = (raw or "").strip()
            if not name or name in picked:
                continue
            if _ILLEGAL_RE.search(name) or Path(name).name != name or name in {".", ".."}:
                continue
            if (root / name / "article.md").is_file():
                picked.append(name)
        return picked
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "article.md").is_file()
    )


def _bundle_readme(entries: list[dict]) -> str:
    """zip 里的目录页：标题列表，Markdown 链接指向各自的 .md。

    链接目标做百分号编码（quote）：文件名里可能有空格和中文，编码后各家 Markdown
    阅读器都能正确跳转，不会在空格处断链。
    """
    lines = ["# 文章导出", "", f"共 {len(entries)} 篇。", ""]
    for entry in entries:
        bits = [b for b in (entry.get("podcast"), entry.get("date"),
                            entry.get("duration_text")) if b]
        suffix = f" —— {' · '.join(bits)}" if bits else ""
        lines.append(f"- [{entry['title']}]({quote(entry['md_name'], safe='')}){suffix}")
        if entry.get("txt_name"):
            lines.append(f"  - [文字稿]({quote(entry['txt_name'], safe='')})")
    return "\n".join(lines).rstrip("\n") + "\n"


def bundle(
    root: Path,
    dest: Path,
    *,
    dirs: list[str] | None = None,
    include_transcript: bool = True,
) -> dict:
    """把所有（或 dirs 指定的）文章打包成一个 zip 写到 dest。

    返回 {"path": str(dest), "episodes": int, "bytes": int}。
    zip 内是扁平结构：每篇一个 `<podcast>-<title>.md`（front matter 版），
    文字稿放在同名 `<podcast>-<title>-文字稿.txt`（仅当 include_transcript 且文件存在），
    外加一份目录页 README.md。
    """
    root = Path(root)
    dest = Path(dest)
    entries: list[dict] = []
    files: list[tuple[str, bytes]] = []
    used: set[str] = set()

    for dir_name in _select_dirs(root, dirs):
        base = root / dir_name
        md_name, md_text = article_markdown(root, dir_name)
        md_name = _unique(md_name, used)
        used.add(md_name)
        stem = md_name[: -len(".md")] if md_name.endswith(".md") else md_name
        meta = _load_meta(base)
        entry = {
            "title": _title_of(meta, dir_name),
            "podcast": str(meta.get("podcast") or "").strip(),
            "date": _date_text(meta.get("pub_date")),
            "duration_text": human_time(meta.get("duration")) if meta.get("duration") else "",
            "md_name": md_name,
            "txt_name": "",
        }
        files.append((md_name, md_text.encode("utf-8")))

        if include_transcript:
            transcript = base / "transcript.txt"
            if transcript.is_file():
                txt_name = _unique(_stem_with_suffix(stem, "-文字稿.txt") + "-文字稿.txt", used)
                used.add(txt_name)
                entry["txt_name"] = txt_name
                try:
                    files.append((txt_name, transcript.read_bytes()))
                except OSError:
                    entry["txt_name"] = ""      # 文件读不动就当没有，不影响其他篇
        entries.append(entry)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        _writestr_utf8(zf, "README.md", _bundle_readme(entries).encode("utf-8"))
        for name, data in files:
            _writestr_utf8(zf, name, data)

    try:
        size = dest.stat().st_size
    except OSError:
        size = 0
    return {"path": str(dest), "episodes": len(entries), "bytes": size}


def export_episode(root: Path, dir_name: str, fmt: str) -> tuple[str, bytes, str]:
    """统一入口：返回 (下载文件名, 文件字节, MIME)。fmt ∈ {"md", "html", "txt"}。

    txt 就是纯文字稿（transcript.txt），没有该文件时抛 ValueError。
    """
    fmt = (fmt or "").strip().lower()
    if fmt not in MIME:
        raise ValueError(f"不支持的导出格式：{fmt}（可选 md / html / txt）")

    base = _episode_dir(root, dir_name)
    stem = _file_stem(base, dir_name)

    if fmt == "txt":
        transcript = base / "transcript.txt"
        if not transcript.is_file():
            raise ValueError(f"文字稿不存在：{dir_name}")
        name = _stem_with_suffix(stem, "-文字稿.txt") + "-文字稿.txt"
        return name, transcript.read_bytes(), MIME["txt"]

    if fmt == "md":
        name, text = article_markdown(root, dir_name)
        return name, text.encode("utf-8"), MIME["md"]

    title = _title_of(_load_meta(base), dir_name)
    name = _stem_with_suffix(stem, ".html") + ".html"
    return name, article_html(root, dir_name, title=title).encode("utf-8"), MIME["html"]
