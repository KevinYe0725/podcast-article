"""大纲驱动的分节写作：让篇幅变成「算出来的」而不是「求出来的」。

为什么要这样写：
- 实测单次生成时，模型对字数的自我约束完全无效（5 轮实测，产出稳定为目标的 1.5-2 倍）
- 但模型对**单节**的篇幅约束明显更听话，而且单节调用可以设 token 上限机械封顶
- 于是：总篇幅 = 开篇预算 + Σ 各节预算 + 结尾预算，逐节封顶 ⇒ 总量可预测

成本控制：素材块（文字稿或分段提炼）作为每条消息的**固定前缀**放在最前面，
DeepSeek 的上下文缓存会命中该前缀，逐节调用不会重复计费全量输入。
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict

from . import config, settings
from .util import html_to_text, ts_clock

# 每档的结构与预算（字符数）。总篇幅 = openings + Σ sections + closing
MODE_PLANS: dict[str, dict] = {
    # 预算按「模型实际产出 ≈ 预算 × 1.25」反推：预算 ×1.25 即为预期成稿字数
    "concise": {
        "sections": 3, "opening": 280, "section": 450, "takeaways": 3, "notes": 0,
        "extras": False, "label": "精华版", "expect": 2100,
    },
    "standard": {
        "sections": 4, "opening": 300, "section": 560, "takeaways": 4, "notes": 3,
        "extras": False, "label": "标准版", "expect": 3700,
    },
    "deep": {
        "sections": 5, "opening": 300, "section": 650, "takeaways": 5, "notes": 3,
        "extras": True, "label": "深度版", "expect": 6000,
    },
}
DEFAULT_MODE = "standard"
TOKENS_PER_CHAR = 0.58  # 中文实测约 0.56，留一点余量


# 这些字符结尾就算「写完了」；否则视为被截断，需要补写收尾
_TERMINALS = "。！？…」』）)】*|`" + chr(34) + "”"


def plan_for(mode: str | None) -> dict:
    return MODE_PLANS.get(mode or DEFAULT_MODE, MODE_PLANS[DEFAULT_MODE])


def stated(chars: int) -> int:
    """给模型看的字数指标。

    实测：模型对「写 X 字」的响应是稳定写到 2 倍左右（109 分钟访谈素材下）。
    所以报价取目标的一半，让自然产出落到目标附近；上限再放宽到报价的 2.4 倍，
    保证它能在不被截断的情况下自然收尾。
    """
    return max(60, int(chars * 0.55))


def budget_total(mode: str | None) -> int:
    p = plan_for(mode)
    closing = p["takeaways"] * 45 + p["notes"] * 130 + (700 if p["extras"] else 0)
    return p["opening"] + p["sections"] * p["section"] + closing


def _cap(chars: int, slack: float = 1.45) -> int:
    """把字符预算换成 token 上限（机械封顶）。"""
    return max(200, int(chars * TOKENS_PER_CHAR * slack))


# ------------------------------------------------------------------ 提示词

_RULES = """写作纪律（违反任意一条即为不合格）：
- 说人话：句子长短交替，允许口语和具体比喻，不要播音腔
- **每个段落不超过 200 字**；抽象论断后面必须马上跟一个具体例子
- **每 200 字至少一个能从素材里查证的具体细节**：人名、数字、时间、地点、物件、原话片段
- 引用就地出现在行文里，格式 `> 原话 [时:分:秒]`，时间戳必须与引文同行且真实存在于素材中
- 禁用这些套话：值得注意的是、总的来说、综上所述、不难看出、毫无疑问、本期最大的信息增量、硬币的另一面、不仅…更是…、发人深省、引人深思、令人震撼、深刻的、让我们、我们看到、众所周知、可以说、从某种意义上
- 不要用"首先/其次/最后"组织段落；不要写"本节/本章/这一节讲了什么"这类自我描述
- 中文标点全角；数字与中文之间加半角空格（如"1906 年 4 月 18 日"）；禁止无必要的中英夹杂（人名、作品名、术语原名除外）
- 忠于素材，绝不编造；区分事实与观点，写清是谁说的"""

_OUTLINE_SYSTEM = f"""你是内容策划。你会收到一期播客或视频的素材，请为它设计一篇文章的大纲——目标是一篇**读者愿意一口气读完**的文章，不是摘要。

先判断内容类型，再安排重心：人物访谈看"做过的选择与代价"；叙事故事看故事线与反转（保留悬念）；方法论看可操作做法与适用边界；观点辩论看双方最强论证与分歧根因。

只输出 JSON，不要任何解释、不要代码块围栏，格式严格如下：
{{
  "title": "标题：具体、有信息量、有钩子，不要照抄原标题",
  "deck": "一句话引语：只说核心张力或问题，绝不要写结局，40 字以内",
  "type": "访谈|叙事|干货|辩论",
  "opening": "开篇怎么切入：写清用哪个具体场景、细节或数字开场（不要写正文）",
  "sections": [
    {{"heading": "小标题：必须含具体名词、数字或动作，不要'光与暗'这种空泛标题",
      "points": ["本节要讲清的 2-3 个要点，每条不超过 25 字"]}}
  ]
}}

要求：
- sections 恰好 {{n}} 节，按文章顺序排列
- **输出务必紧凑**：每节最多 3 条 points，每条不超过 25 字；不要写多余的解释文字或注释
- 若素材里有反转、代价或矛盾，把它安排成靠后的一节（作为全篇高潮），但标题不要剧透
- 各节之间要有推进关系，不要并列堆砌
- 素材不足时不硬凑节数，宁可少一节"""

_OPENING_SYSTEM = f"""你在写一篇文章的开篇。只输出开篇正文（2-3 段，共约 {{chars}} 字），不要写标题、不要写小标题、不要写任何栏目名。

要求：
- 用一个具体场景、细节、数字或反直觉事实切入，让读者想读下一段
- **选一个后面章节不会专门展开的细节**：不要用第 1 节的场景开场，也不要引用
  与后面章节相同的那句原话（同一处细节被讲两遍，读者会觉得文章在绕圈）
- **绝不罗列结论、绝不概括全文、绝不提前交出结局**；只暗示后面有转折
- 只写散文段落，不要表格、不要清单
- 段落不超过 200 字

{_RULES}"""

_SECTION_SYSTEM = f"""你在写一篇文章的其中一节。只输出这一节（`## 小标题` + 正文），不要写其他节，不要写结尾栏目。

本节目标 {{chars}} 字，**上限是它的 1.3 倍**。写满要点即可；要点写不完时宁可少写一个要点，也绝不超长，绝不注水。

要求：
- 首句给出本节的核心判断，随后用场景、细节、数字、原话展开
- 小标题用给定的大纲标题，可微调措辞但必须保持具体
- 本节引 1-2 处原话（标注时间戳，整篇合计 4-6 条）；确保本节至少引 1 处，**不要与其他章节重复同一条引用**
- **只写散文段落**：不要表格、不要 bullet 清单、不要「素材要点/时间戳」这类工作笔记
- 不要在正文里直接对读者说话（不要写"对你关注的 XX"），把判断写成面向所有读者的结论

{_RULES}"""

_CLOSING_SYSTEM = f"""你在写一篇文章的结尾栏目。只输出这些栏目本身，不要重复正文内容。

栏目：
1. `## 读完你会带走什么`：{{takeaways}} 条，每条一句话。必须是**可检验的判断、可试的做法、或可复用的框架**；禁止写"了解了 XX""认识了 XX"这类空话。
{{notes_part}}
{{extras_part}}
{_RULES}"""


def _notes_block(n: int) -> str:
    if not n:
        return ""
    return (
        f"2. `## 编辑点评`：{n} 条。每条先指出薄弱、可疑或以偏概全之处，"
        "再说读者该怎么看待。不要用「值得深挖」「值得思考」敷衍。"
    )


def _extras_block(enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        "3. `## 提及的书影音 / 人物 / 概念`：Markdown 表格（名称 | 是什么 | "
        "一句话说明为什么值得了解），只收对理解本文有必要的，不超过 8 行。\n"
        "4. `## 金句摘录`：3-5 条原话并标注 [时:分:秒]，**必须是正文中没有出现过的**。"
    )


def _parse_json(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍代码块围栏与前后废话）。"""
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        return json.loads(cleaned[start : end + 1])
    raise ValueError(f"大纲不是合法 JSON（输出 {len(text)} 字，可能被截断）")


# ------------------------------------------------------------------ 主流程

def build_material(
    title: str, podcast: str, author: str, shownotes_html: str | None, body: str
) -> str:
    """构造每条消息共用的固定前缀（便于上下文缓存命中）。"""
    profile = settings.profile_text()
    notes = html_to_text(shownotes_html)
    parts = [f"节目标题：{title}", f"播客/频道：{podcast}", f"主播/作者：{author or '未知'}"]
    if profile:
        parts.append(profile)
    if notes:
        parts.append(f"主播的节目笔记（可作为内容地图参考）：\n{notes[:4000]}")
    parts.append(f"以下是素材：\n\n{body}")
    return "\n\n".join(parts)


def _chat_stream(client, model: str, system: str, user: str, log, on_chars, max_tokens: int) -> str:
    """与 summarize._chat 同构，但独立出来避免循环依赖。"""
    from .summarize import _chat

    return _chat(client, model, system, user, log=log, max_tokens=max_tokens,
                 on_chars=on_chars, temperature=0.9)


def write_outlined(
    client,
    model: str,
    material: str,
    mode: str,
    log=print,
    progress=None,
) -> str:
    """分节写作主流程：大纲 → 开篇 → 逐节 → 结尾。返回完整 Markdown。"""
    plan = plan_for(mode)
    seen = 0

    def tick(n: int) -> None:
        nonlocal seen
        seen = n
        if progress:
            progress("llm", {"chars": n})

    def call(system: str, task: str, cap_chars: int, label: str) -> str:
        nonlocal seen
        base = seen
        text = _chat_stream(
            client, model, system, f"{material}\n\n----\n\n{task}",
            log, lambda n: tick(base + n), _cap(cap_chars),
        )
        seen = base + len(text)
        # 撞上限会在半句话处断掉：再要一小段把它收尾（比截断后交稿好）
        if text and text.rstrip()[-1] not in _TERMINALS:
            log(f"[llm] {label} 结尾不完整，补写收尾…")
            tail = _chat_stream(
                client, model,
                "你刚才写的内容在末尾被截断了。只输出接下来的内容，让它自然地收尾，"
                "总长不超过 120 字；不要重复已经写过的内容，不要另起新话题。",
                f"{material}\n\n----\n\n以下是已写内容（在末尾被截断）：\n\n{text[-1500:]}",
                log, None, 320,
            )
            if tail.strip():
                text = f"{text.rstrip()} {tail.strip()}"
                seen = base + len(text)
        if progress:
            progress("llm", {"chars": seen})
        log(f"[llm] {label} 完成（{len(text)} 字）")
        return text.strip()

    # 1) 大纲
    log(f"[llm] 分节写作 · {plan['label']}：先生成大纲（{plan['sections']} 节）…")
    outline = _parse_json(
        call(_OUTLINE_SYSTEM.replace("{n}", str(plan["sections"])),
             "请设计这篇文章的大纲，只输出紧凑 JSON。", 1500, "大纲")
    )
    title = (outline.get("title") or "").strip()
    deck = (outline.get("deck") or "").strip()
    sections = [s for s in (outline.get("sections") or []) if s.get("heading")][: plan["sections"]]
    if not title or not sections:
        raise ValueError("大纲缺少标题或分节")
    log(f"[llm] 大纲：{title}（{len(sections)} 节）")

    # 2) 开篇
    opening = call(
        _OPENING_SYSTEM.replace("{chars}", str(stated(plan["opening"]))),
        f"文章标题：{title}\n引语：{deck}\n开篇切入方式：{outline.get('opening', '')}\n"
        f"全文将包含这些节（避免在开篇把它们的结论讲完）："
        + "；".join(s["heading"] for s in sections),
        plan["opening"], "开篇",
    )

    # 3) 逐节
    heads = [s["heading"] for s in sections]
    bodies: list[str] = []
    for i, sec in enumerate(sections, 1):
        heading = sec["heading"].strip().lstrip("#").strip()
        points = sec.get("points") or []
        others = "；".join(h for h in heads if h != sec["heading"])
        task = (
            f"文章标题：{title}\n"
            f"本节是第 {i}/{len(sections)} 节，小标题：{heading}\n"
            f"本节要点：\n- " + "\n- ".join(str(p) for p in points) + "\n"
            + (f"其他章节（不要重复它们的内容）：{others}\n" if others else "")
        )
        if progress:
            progress("llm", {"section": i, "total": len(sections), "chars": seen})
        body = call(_SECTION_SYSTEM.replace("{chars}", str(stated(plan["section"]))), task,
                    plan["section"], f"第 {i}/{len(sections)} 节《{heading[:16]}》")
        if not body.lstrip().startswith("#"):
            body = f"## {heading}\n\n{body}"
        bodies.append(body)

    # 4) 结尾栏目
    closing = call(
        _CLOSING_SYSTEM.replace("{takeaways}", str(plan["takeaways"]))
        .replace("{notes_part}", _notes_block(plan["notes"]))
        .replace("{extras_part}", _extras_block(plan["extras"])),
        f"文章标题：{title}\n全文各节：{'；'.join(heads)}\n请写结尾栏目。",
        # 结尾不做折扣：点评类内容模型会反复补充，预算给足才不会被截断
        int(plan["takeaways"] * 60 + plan["notes"] * 170 + (900 if plan["extras"] else 0)),
        "结尾栏目",
    )

    article = "\n\n".join([f"# {title}", f"> {deck}", opening, *bodies, closing])
    log(f"[llm] 组装完成：{len(article)} 字（预算 {budget_total(mode)}，"
        f"预期成稿约 {plan.get('expect', '?')} 字，{len(sections)} 节）")
    return article
