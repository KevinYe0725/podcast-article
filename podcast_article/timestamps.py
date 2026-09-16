"""时间戳的识别与换算（含「时间区间」这种形态）。

文章里的时间戳有两种，都是模型合理写出来的：

    [00:56:03]                单个时间点
    [00:06:36-00:06:49]       引用一段话时的时间区间（引文跨了 13 秒）

以前只认第一种，于是整篇都是区间的文章（实测《The Diary Of A CEO》那篇全是这种）
一个可点的语音链接都没有。区间按**起点**跳转：引文就是从那一秒开始的。

另外还认两段式的 `[09:24]`：模型偶尔漏写小时，按 分:秒 解释
（播客里 `[01:45]` 当 1 小时 45 分的情况不会出现，而当 1 分 45 秒是合理的）。
"""
from __future__ import annotations

import re

# 单个时间点：分:秒（09:24）或 时:分:秒（00:06:36）
_CORE = r"\d{1,2}:\d{2}(?::\d{2})?"
# 区间分隔符：连字符、各种破折号/减号、波浪号，以及「至 / 到」
_SEP = r"\s*[-–—−~～至到]\s*"

# 正文里的时间戳（含括号）。全角方括号 / 中文方头括号也认：
#   分组 1 = 起点，分组 2 = 区间终点（单个时间戳时是 None）
TS_RE = re.compile(rf"[\[［【]\s*({_CORE})(?:{_SEP}({_CORE}))?\s*[\]］】]")

# 行首时间戳：文字稿每行都是这个形态（`[00:10:07] 正文` / `[00:10:07-00:11:20] 正文`）
LINE_TS_RE = re.compile(rf"^\[({_CORE})(?:{_SEP}({_CORE}))?\]")

# 整行只有时间戳（排版收尾时要把这种孤儿行并回引文，见 postprocess.fix_orphan_timestamps）
ONLY_TS_RE = re.compile(rf"^\s*\[({_CORE})(?:{_SEP}({_CORE}))?\]\s*$")

# 只匹配不带括号的时间点（搜索、歌词式引用等处用）
BARE_TS_RE = re.compile(rf"({_CORE})")


def to_seconds(text: str) -> int:
    """`00:06:36` → 396；`09:24` → 564（两段式按 分:秒 算）。"""
    parts = [int(p) for p in str(text).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)          # 09:24 → 00:09:24
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def linkify(html: str) -> str:
    """把正文里的时间戳包成可点的 <span>（阅读页用，前端靠 data-sec 跳到音频那一段）。"""
    def repl(m: re.Match) -> str:
        start = m.group(1)
        title = ("跳到音频此处" if not m.group(2)
                 else f"跳到音频此处（这一段的起点 {start}）")
        return (f'<span class="ts" data-sec="{to_seconds(start)}" role="button" tabindex="0" '
                f'title="{title}">{m.group(0)}</span>')

    return TS_RE.sub(repl, html)


def mark(html: str) -> str:
    """只包一层 <span class="ts">（导出的 HTML 单文件用：那里没有播放器，只有样式）。"""
    return TS_RE.sub(lambda m: f'<span class="ts">{m.group(0)}</span>', html)


def start_of(text: str) -> int | None:
    """从任意一段带时间戳的文本里取出起点秒数；没有时间戳返回 None。"""
    m = TS_RE.search(str(text or ""))
    return to_seconds(m.group(1)) if m else None
