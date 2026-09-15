"""确定性后处理：把 LLM 不擅长的格式纪律用代码做掉。

为什么不让模型自己守规矩：
- 字数：模型不会数字数（实测三档全部超目标 30-100%）
- 段落长度、标点、孤立时间戳、套话：模型时改时不改，还经常在改写时顺手
  把具体细节抽象化（实测密度掉到 1.0/千字，原文是 7.2）

所以：内容交给模型，**排版与篇幅交给这里**。每一步都确定、可测、零幻觉风险。
"""
from __future__ import annotations

import re

FIXED_SECTIONS = ("读完你会带走什么", "编辑点评", "金句摘录", "提及的书影音")

# 篇幅区间：按实测校准（模型实际产出约为指令字数的 1.5-2 倍，指令需相应收紧）
# 实测结论：模型产出由内容量决定，指令只能通过「节数」间接影响总字数。
# 以下区间是 5 轮真实生成（109 分钟访谈）的实测落点，用于触发极端兜底裁剪。
MODE_TARGETS: dict[str, tuple[int, int]] = {
    "concise": (2500, 4200),   # 3 节，5-8 分钟
    "standard": (4200, 7000),  # 4-5 节 + 点评，8-13 分钟
    "deep": (7000, 11000),     # 5-6 节 + 表格 + 金句，14-20 分钟
}

# 去掉这些填充词（只在句首/独立出现时删，避免改动语义）
FILLER = [
    "值得注意的是，", "值得注意的是", "总的来说，", "总的来说", "综上所述，",
    "综上所述", "不难看出，", "不难看出", "毫无疑问，", "毫无疑问",
    "毋庸置疑，", "众所周知，", "可以说，", "从某种意义上说，", "从某种意义上，",
]

_ORPHAN_TS = re.compile(r"^\s*\[(\d{2}:\d{2}:\d{2})\]\s*$")
_TS = re.compile(r"\[\d{2}:\d{2}:\d{2}\]")
_SENT_END = re.compile(r"(?<=[。！？；])")
_CJK = r"\u4e00-\u9fff"


def split_long_paragraphs(text: str, limit: int = 200) -> tuple[str, int]:
    """把超长段落按句号拆开。只在正文段落上做，不动标题/表格/代码块/引用。"""
    out: list[str] = []
    fixed = 0
    in_code = False
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(block)
            continue
        skip = (
            in_code
            or not stripped
            or stripped.startswith(("#", "|", ">", "-", "*", "1.", "2.", "3."))
        )
        if skip or len(stripped) <= limit:
            out.append(block)
            continue
        sentences = [s for s in _SENT_END.split(stripped) if s.strip()]
        buf = ""
        pieces: list[str] = []
        for s in sentences:
            if buf and len(buf) + len(s) > limit:
                pieces.append(buf.strip())
                buf = s
            else:
                buf += s
        if buf.strip():
            pieces.append(buf.strip())
        if len(pieces) > 1:
            fixed += 1
        out.append("\n\n".join(pieces))
    return "\n\n".join(out), fixed


def fix_orphan_timestamps(text: str) -> tuple[str, int]:
    """独占一行的时间戳：能并进上一行引文就并，否则删掉。"""
    lines = text.split("\n")
    out: list[str] = []
    fixed = 0
    for line in lines:
        m = _ORPHAN_TS.match(line)
        if not m:
            out.append(line)
            continue
        ts = m.group(1)
        # 往上找最近的非空行；若它像引文（或还不是引文），就把时间戳并上去
        for i in range(len(out) - 1, -1, -1):
            prev = out[i].strip()
            if not prev:
                continue
            if _TS.search(prev):  # 已有时间戳，直接丢弃这行
                fixed += 1
                break
            out[i] = f"{prev} [{ts}]" if prev.endswith(("。", "」", "\"", "”")) else f"{prev} [{ts}]"
            fixed += 1
            break
        else:
            fixed += 1
    return "\n".join(out), fixed


def normalize_punctuation(text: str) -> tuple[str, int]:
    """中文语境里误用的半角标点改成全角；数字与中文之间补半角空格。"""
    before = text
    # 中文字符后的半角逗号/句号/问号/冒号/分号
    text = re.sub(f"([{_CJK}]),(?![0-9])", r"\1，", text)
    text = re.sub(f"([{_CJK}]);", r"\1；", text)
    text = re.sub(f"([{_CJK}]):", r"\1：", text)
    text = re.sub(f"([{_CJK}])\\?", r"\1？", text)
    text = re.sub(f"([{_CJK}])\\.(?![0-9\\s]*\\d)", r"\1。", text)
    # 数字与中文之间加空格（避开已是空格的、以及 markdown 语法）
    text = re.sub(f"([{_CJK}])(\\d)", r"\1 \2", text)
    text = re.sub(f"(\\d)([{_CJK}])", r"\1 \2", text)
    text = re.sub(r"  +", " ", text)
    return text, (0 if text == before else 1)


def strip_filler(text: str) -> tuple[str, int]:
    """删掉套话填充词。"""
    count = 0
    for phrase in FILLER:
        n = text.count(phrase)
        if n:
            count += n
            text = text.replace(phrase, "")
    return text, count


def _sections(text: str) -> list[tuple[str, str]]:
    """切成 (标题行, 正文) 序列，标题行含 '## '。"""
    parts = re.split(r"^(## .*)$", text, flags=re.M)
    out: list[tuple[str, str]] = [("", parts[0])]
    for i in range(1, len(parts), 2):
        out.append((parts[i], parts[i + 1] if i + 1 < len(parts) else ""))
    return out


def trim_to_budget(text: str, mode: str, protect_ends: bool = True) -> tuple[str, list[str]]:
    """超出目标上限时，整节删除中间偏后的章节。

    固定栏目（带走什么/编辑点评/金句/表格）永不删除；protect_ends 会保住第一节
    与最后一节正文——反转和高潮通常就在最后一节，不能为了字数把它删掉。
    """
    hi = int(MODE_TARGETS.get(mode, MODE_TARGETS["standard"])[1] * 1.6)
    dropped: list[str] = []
    while len(text) > hi:
        secs = _sections(text)
        candidates = [
            i for i, (head, _) in enumerate(secs)
            if head.startswith("## ") and not any(f in head for f in FIXED_SECTIONS)
        ]
        if len(candidates) <= (2 if protect_ends else 1):
            break
        # 从后往前删，但跳过最后一节正文
        idx = candidates[-2] if protect_ends else candidates[-1]
        idx = candidates[-1]
        dropped.append(secs[idx][0].strip("# ").strip())
        secs.pop(idx)
        text = "".join(head + body for head, body in secs)
    return text, dropped


def finalize(article: str, mode: str = "standard", trim: bool = True) -> tuple[str, dict]:
    """对成稿做全部确定性修正，返回 (文本, 处理报告)。"""
    report: dict = {}
    article = article.strip()
    article, report["拆分超长段落"] = split_long_paragraphs(article)
    article, report["修孤立时间戳"] = fix_orphan_timestamps(article)
    article, report["删套话"] = strip_filler(article)
    article, report["标点规范化"] = normalize_punctuation(article)
    if trim:
        article, dropped = trim_to_budget(article, mode)
        if dropped:
            report["极端超长兜底删除"] = dropped
    report["最终字数"] = len(article)
    return article, report
