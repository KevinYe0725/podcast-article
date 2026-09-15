"""文章可读性体检：用可量化的指标评估「读者愿不愿意读」。

指标含义（都是越高越差，除密度类）：
- 套话密度：AI 编辑体套话出现次数 / 千字
- 元描述：出现「这一节讲了什么」「本章」这类自我描述，说明是纪要而非文章
- bullet 占比：bullet 行 / 总行数，越高越像会议纪要
- 重复率：文末引用与正文的重叠程度（金句节常见浪费）
- 具体性密度：每千字包含的数字/时间戳/专名数量（越高越好）
- 段落长度：最长段落字数（过长劝退）
- 开场剧透：开篇 200 字内是否已给出结局/全部结论
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CLICHE = [
    "值得注意的是", "总的来说", "综上所述", "不难看出", "毫无疑问", "毋庸置疑",
    "本期最大的信息增量", "硬币的另一面", "不仅是", "更是", "发人深省", "引人深思",
    "令人震撼", "深刻的", "这一节讲了什么", "本章", "本节", "让我们", "我们看到",
    "首先", "其次", "最后", "众所周知", "可以说", "从某种意义上",
]
CONCRETE = re.compile(r"\d{4}\s*年|\d+\s*%|\d+\.\d+|\[\d{2}:\d{2}:\d{2}\]|\d+\s*多?\s*[个条位名岁年天倍万亿级种次轮人元月日周]")


def analyze(text: str) -> dict:
    chars = len(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    bullets = [ln for ln in lines if ln.strip().startswith(("-", "*", "1.", "2.", "3."))]
    headings = [ln for ln in lines if ln.startswith("#")]

    # 套话
    cliche_hits = {w: text.count(w) for w in CLICHE if text.count(w)}
    cliche_total = sum(cliche_hits.values())

    # 元描述（自我指涉的纪要腔）
    meta = len(re.findall(r"(这一节|本章|本节|这部分)的?(讲了什么|讲的是|内容|要点)", text))

    # 具体性
    concrete = len(CONCRETE.findall(text))

    # 段落长度
    paras = [p.strip() for p in text.split("\n\n") if p.strip() and not p.strip().startswith("#")]
    longest = max((len(p) for p in paras), default=0)

    # 引用重复：文末引用节里的话是否已在正文出现过
    quoted = re.findall(r"[「“\"]([^」”\"]{12,})[」”\"]", text)
    dup = sum(1 for q in set(quoted) if text.count(q) > 1)

    # 开场剧透：开头 260 字里是否出现结局性表述
    head = text[:400]
    spoiler = bool(re.search(r"(根本不存在|最终发现|结局|却不知道|才发现|原来)", head))

    # 写作模式：以标题行开头的小节，判断是否「承诺具体信息」
    # 正文小标题（## 或 ###），排除固定栏目名
    FIXED = ("读完你会带走什么", "编辑点评", "金句摘录", "内容速览", "核心内容", "提及的书影音")
    sub_heads = [
        h.lstrip("# ").strip() for h in headings
        if h.startswith("##") and not any(h.strip().endswith(f) or f in h for f in FIXED)
    ]
    vague_heads = sum(1 for h in sub_heads if not re.search(r"\d|针|鱼|乔丹|米勒|毒|地震|标牌|母亲|地图|公司|斯坦福|阿加西|校长", h))

    # 格式缺陷
    orphan_ts = len(re.findall(r"^\s*\[\d{2}:\d{2}:\d{2}\]\s*$", text, re.M))
    halfwidth = len(re.findall(r"[\u4e00-\u9fff],|[\u4e00-\u9fff]\.(?![0-9])", text))
    no_space_num = len(re.findall(r"[\u4e00-\u9fff]\d", text))

    return {
        "字数": chars,
        "预计阅读": f"{max(1, round(chars / 500))} 分钟",
        "小标题数": len(sub_heads),
        "小标题含糊数": vague_heads,
        "套话次数": cliche_total,
        "套话密度(/千字)": round(cliche_total / max(chars, 1) * 1000, 1),
        "套话明细": cliche_hits,
        "元描述次数": meta,
        "bullet 行数": len(bullets),
        "bullet 占比": f"{round(len(bullets) / max(len(lines), 1) * 100)}%",
        "具体性密度(/千字)": round(concrete / max(chars, 1) * 1000, 1),
        "最长段落字数": longest,
        "引用重复条数": dup,
        "开场剧透": spoiler,
        "小标题列表": sub_heads,
        "中英夹杂": len(re.findall(r"[\u4e00-\u9fff]\s?[a-z]{3,}\s?[\u4e00-\u9fff]", text)),
        "孤立时间戳行": orphan_ts,
        "半角标点误用": halfwidth,
        "数字未加空格": no_space_num,
    }


if __name__ == "__main__":
    for path in sys.argv[1:]:
        p = Path(path)
        if not p.is_file():
            continue
        print(f"\n=== {p.parent.name} ===")
        for k, v in analyze(p.read_text(encoding="utf-8")).items():
            print(f"  {k}: {v}")
