"""「问你的库」的作答规则：**一处定义**，Web / CLI / MCP 三个入口共用。

为什么单独一个模块（而不是在三处各写一遍提示词）：

1. 三处各写一遍，就会出现「同一个能力、三种措辞、各自漂移」。
2. 更贵的一次教训是签名错位 —— `/api/kb/ask`、CLI `ask`、MCP `ask_library` 当初各自
   手拼 `_chat` 调用，三处全传错形状（messages 数组当第一个参数），一跑就
   `TypeError`，而测试因为打了宽松的桩一直没发现。合并到一处，这种事只能错一次。

作答规则里最容易被忽略的一条是**不要逐条汇报检索结果**：早期版本要求「每条依据标编号」，
模型于是把 6 条片段挨个点评一遍（"只提到…但没有展开"），看着合规，实际上什么也没回答。
现在要求它先给结论，只引用真正支撑结论的那几条；资料不够就一句话说清，不要硬撑。
"""
from __future__ import annotations

SYSTEM = (
    "你是这位读者私人播客书库的研究助手。回答他关于自己收藏内容的问题。\n"
    "规则：\n"
    "1. 只依据【资料片段】里的事实回答，不要引入资料之外的知识；资料里没有就直接说"
    "「资料里没有」，**不要猜、不要用常识补**。\n"
    "2. 先给结论（一两句），再给支撑它的依据。**不要逐条复述每条片段**，也不要评论"
    "片段本身（例如「只提到…但没有展开」）；只引用真正支撑结论的那几条，用 [n] 标出。\n"
    "3. 如果资料只能回答一半：把能回答的那半答完，然后用一句话说明剩下那半资料里没有"
    "（例如「另外，资料里没有说他具体怎么操作」），就此打住。\n"
    "4. 如果资料与问题基本无关（例如问的东西不在这些内容里）：直接说这一句，"
    "并指出资料里最接近的是什么，**不要罗列一堆不相关的片段**。\n"
    "5. 不要复述资料原文的长度，不要写栏目名，不要客套。用中文。"
)


def build(question: str, hits: list[dict], memories: list[str]) -> tuple[str, str]:
    """拼出 (system, user)。

    user 里的三个标题（【关于这位读者的已知信息】/【资料片段】/读者的问题）是**契约**：
    Web 的界面文案、CLI 输出与 MCP 返回值都靠它们，改动要同步测试。
    """
    lines = []
    for i, h in enumerate(hits or [], 1):
        where = h.get("title") or h.get("dir") or ""
        if h.get("podcast"):
            where = f"{where}（{h['podcast']}）" if where else str(h["podcast"])
        if h.get("start_sec") is not None:
            where += f" · {int(h['start_sec']) // 60:02d}:{int(h['start_sec']) % 60:02d}"
        if h.get("heading"):
            where += f" · {h['heading']}"
        if h.get("doc_kind") == "transcript":
            where += " · 文字稿"
        lines.append(f"[{i}] {where}\n{h.get('text') or ''}")
    mem_text = "\n".join(f"- {t}" for t in (memories or [])) or "（没有）"
    user = (
        f"读者的问题：{question}\n\n"
        f"【关于这位读者的已知信息】\n{mem_text}\n\n"
        f"【资料片段】\n" + "\n\n".join(lines)
    )
    return SYSTEM, user


def answer(question: str, hits: list[dict], memories: list[str],
           *, max_tokens: int = 1200, usage_recorder=None) -> str:
    """让模型就检索到的片段作答（**会调用模型，产生费用**）。"""
    from . import summarize
    system, user = build(question, hits, memories)
    return summarize.ask_once(system, user, max_tokens=max_tokens, usage_recorder=usage_recorder)
