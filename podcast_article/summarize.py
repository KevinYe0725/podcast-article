"""DeepSeek 精读提炼：文字稿 -> 深度文章。

策略：
- 文字稿不太长（<= max_chars）：单次直读全文，整体把握最好
- 太长：分段精读（保留时间戳）-> 汇总成文
"""
from __future__ import annotations

import re
from collections import Counter

from openai import OpenAI

from . import config, outline, postprocess, settings, usage
from .util import html_to_text, ts_clock


def _client() -> OpenAI:
    return OpenAI(api_key=config.deepseek_api_key(), base_url=config.DEEPSEEK_BASE_URL)


def _chat(
    client: OpenAI, model: str, system: str, user: str,
    log=print, max_tokens: int = 8192, on_chars=None, temperature: float = 1.0,
    thinking: bool = False, reasoning_effort: str = "low",
) -> str:
    """流式调用。按 DeepSeek 思考模式文档区分两种模式：

    - thinking=False：显式关闭思考（extra_body.thinking.type=disabled）。此时
      temperature 生效，输出 token 全部用于正文 —— 篇幅可控，适合逐节写作。
    - thinking=True ：开启思考，思维链走 delta.reasoning_content。此时 DeepSeek
      文档明确 temperature/top_p 不生效，所以不传；token 预算要额外留出思考开销。

    实测教训：思考模式默认是打开的，若只读 delta.content 且 token 上限偏小，
    思考会把预算吃光、正文返回空字符串（曾整篇产出 0 字）。
    """
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "stream": True,
        # 流式响应默认不带 usage，必须显式索要：最后一个 chunk 会带完整用量
        "stream_options": {"include_usage": True},
    }
    if thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        kwargs["temperature"] = temperature

    try:
        stream = client.chat.completions.create(**kwargs)
    except Exception as exc:
        # 兼容不支持 stream_options 的 OpenAI 兼容服务端：去掉它再试一次
        if "stream_options" not in str(exc):
            raise
        log("[llm] 服务端不支持 stream_options，本次不记录 token 用量")
        kwargs.pop("stream_options", None)
        stream = client.chat.completions.create(**kwargs)
    parts: list[str] = []
    printed = 0
    think_len = 0
    think_reported = 0
    finish = None
    for chunk in stream:
        raw_usage = getattr(chunk, "usage", None)
        if raw_usage is not None:
            usage.note(model, raw_usage)      # 记账：没有活动记录器时是空操作
        choice = chunk.choices[0] if chunk.choices else None
        if choice is None:
            continue
        if choice.finish_reason:
            finish = choice.finish_reason
        delta = choice.delta
        thought = getattr(delta, "reasoning_content", None)
        if thought:
            think_len += len(thought)
            if think_len >= think_reported + 800:
                think_reported = think_len
                log(f"[llm] 思考中 {think_len} 字…")
        if delta.content:
            parts.append(delta.content)
            n = len("".join(parts))
            if on_chars:
                on_chars(n)
            if len(parts) >= printed + 400:
                printed = len(parts)
                log(f"[llm] 已生成 {n} 字…")

    text = "".join(parts).strip()
    if not text:
        raise RuntimeError(
            f"模型未返回正文（finish_reason={finish}，思考 {think_len} 字，"
            f"上限 {max_tokens} tokens）——请调大上限或关闭思考模式"
        )
    if finish == "length":
        log(f"[llm] ⚠ 正文触达 max_tokens({max_tokens}) 被截断，共 {len(text)} 字"
            + (f"（另有思考 {think_len} 字）" if think_len else ""))
    elif think_len:
        log(f"[llm] 思考 {think_len} 字 → 正文 {len(text)} 字")
    return text


# ---------------------------------------------------------------- 单次直读

_EDITOR_SYSTEM = """你是一位顶尖的非虚构作者与编辑。你拿到一期播客或视频的完整文字稿（每行行首 [时:分:秒] 是时间戳），要把它写成**一篇读者愿意一口气读完、并愿意转发的文章**——不是摘要，不是读书笔记，不是会议纪要。

## 先判断内容类型，再决定重心
- 人物访谈 → 重心是"这个人做过的具体选择、代价与判断"，用决定与转折串起全文
- 叙事故事 → 重心是故事线与反转，保留悬念，绝不提前交底
- 方法论/干货 → 重心是可操作的做法与适用边界（什么情况下不适用）
- 观点辩论 → 重心是双方最强的论证，以及分歧的真正原因

## 硬性要求（违反任意一条即为不合格）
1. **不许在前面剧透全篇**：开头用一个具体场景、细节、数字或一句反直觉的话切入，让读者想读第二段。严禁开头罗列全部结论，**严禁写"内容速览"**。标题下那句一句话引语只给"核心张力或问题"，**不要把结局写进去**。
2. **小标题必须承诺具体信息**，含具体名词、数字或动作。
   反例：`混乱中的秩序`、`光与暗`、`另一个世界之中`。
   正例：`他把名字缝在鱼身上，因为混乱会再来一次`、`他毕生命名的 2500 种鱼，被证明根本不存在`。
3. **段落不超过 200 字**，长内容必须拆段；全篇最长段落不得超过 300 字。
4. **每 200 字至少有一个能从原稿查证的细节**：人名、数字、时间、地点、物件、原话片段。抽象论断后面必须马上跟一个具体例子。
5. **引用就地出现在行文里**（`>` 引用块或行内引号），标注 [时:分:秒]。全篇引用不超过 6 条，每条只出现一次——**不要另设"金句摘录"去重复正文**。
6. 忠于原意，绝不编造：区分事实与观点，写清是谁说的；时间戳必须真实存在于原稿。
7. 文章要有判断，不要只做转述。每个正文章节的最后一句可以是你的判断或一个悬念，但**不要复述本节内容**。

## 语言要求
- 简体中文（专有名词保留原文），面向没听过这期节目的读者，行文自成一体
- 说人话：句子长短交替，允许口语、具体比喻，不要播音腔
- **禁用这些套话**：值得注意的是、总的来说、综上所述、不难看出、毫无疑问、毋庸置疑、本期最大的信息增量、硬币的另一面、不仅…更是…、发人深省、引人深思、令人震撼、深刻的、让我们、我们看到、众所周知、可以说、从某种意义上
- 不要用"首先/其次/最后"来组织段落；不要写"本节/本章/这一节讲了什么"这类自我描述
- 数字优先于形容词；能写"三年"就不要写"很长时间"
- 中文排版：标点一律全角（，。：；？！、），数字与中文之间加半角空格（如"1906 年 4 月 18 日"），专有名词保留原文
- **禁止中英夹杂**：正文里不许出现没有必要的英文单词（如 motto、idea、mindset），改用中文词（宗旨、想法、心态）；只有人名、作品名、机构名、术语原名才保留英文
- 时间戳必须与引文写在同一行（`> 引文 [时:分:秒]`），绝不允许时间戳独占一行

{structure}"""


# 三种篇幅档位：让读者按场景选择（想快速判断值不值得听 / 认真读 / 留档细读）
STRUCTURES: dict[str, str] = {
    "concise": """## 本篇篇幅与结构（精华版：只写 3 节正文，5-8 分钟读完）
```
# {标题：具体、有信息量、有钩子}
> {一句话：最核心的冲突或结论，40 字以内}

{开篇 2-3 段，用一个具体场景切入，约 200 字，不要给结论}

## {章节标题：承诺具体信息}
{300-450 字}

## {章节标题}
{300-450 字}

## {章节标题}
{300-450 字}
（**就写这 3 节，不要多加章节**；每节 300-450 字）

## 读完你会带走什么
{3 条，每条一句话：可检验的判断、可试的做法、或可复用的框架。禁止写"了解了 XX"}
```
不写金句摘录、不写书影音表格、不写编辑点评。""",
    "standard": """## 本篇篇幅与结构（标准版：4-5 节正文 + 编辑点评，8-13 分钟读完）
```
# {标题：具体、有信息量、有钩子}
> {一句话：最核心的冲突或结论，40 字以内}

{开篇 2-3 段，用一个具体场景切入，约 200 字。暗示后面有转折，但不要交底}

## {章节标题：承诺具体信息}
{400-700 字}

## {章节标题}
{400-700 字}

## {章节标题}
{400-700 字}

## {章节标题（如有反转/代价/矛盾，必须单独成节并放在靠后位置，作为全篇高潮）}
{400-700 字}

## 读完你会带走什么
{3-5 条，每条一句话：可检验的判断、可试的做法、或可复用的框架。禁止写"了解了 XX"}

## 编辑点评
{3 条。每条：先指出薄弱、可疑或以偏概全之处，再说读者该怎么看待。不要用"值得深挖""值得思考"敷衍}
```""",
    "deep": """## 本篇篇幅与结构（深度版：5-6 节 + 书影音表格 + 金句，14-20 分钟读完）
```
# {标题：具体、有信息量、有钩子}
> {一句话：最核心的冲突或结论，40 字以内}

{开篇 2-3 段，用一个具体场景切入}

## {章节标题：承诺具体信息} × 5-6 个章节
{每节 500-900 字，段落仍不超过 200 字}

## 读完你会带走什么
{4-6 条}

## 编辑点评
{3-4 条，同标准版要求}

## 提及的书影音 / 人物 / 概念
{表格：名称 | 是什么 | 一句话说明为什么值得了解。只收对理解本文有必要的，不超过 8 行}

## 金句摘录
{3-5 条原话，标注 [时:分:秒]。**必须是正文中没有出现过的**，否则这一节整个去掉}
```""",
}
DEFAULT_MODE = "standard"
# 思考模式实测：长上下文下思考量可膨胀到 5000+ 字并吃光预算，正文为空。
# 因此默认全程关闭；开启时需同步调大 token 上限。
THINKING_ALLOWANCE = 8000

# 各档位的目标字数区间，用于复检时压缩/扩写
# 实测结论：模型产出由内容量决定，指令只能通过「节数」间接影响总字数。
# 以下区间是 5 轮真实生成（109 分钟访谈）的实测落点，也用于触发极端兜底裁剪。
MODE_TARGETS: dict[str, tuple[int, int]] = {
    "concise": (2500, 4200),   # 3 节，5-8 分钟
    "standard": (4200, 7000),  # 4-5 节 + 编辑点评，8-13 分钟
    "deep": (7000, 11000),     # 5-6 节 + 表格 + 金句，14-20 分钟
}

# 生成时的 token 上限（机械兜底，按中文约 0.56 token/字估算并留出余量）
# 上限只用来防止跑飞，不承担篇幅控制——指望它压字数会截断正文（实测把
# 「带走什么」「编辑点评」两节整个截掉）。篇幅由 postprocess.trim_to_budget 机械裁剪。
MODE_MAX_TOKENS: dict[str, int] = {
    "concise": 2600,   # ≈4600 字：够模型自然收尾
    "standard": 4600,  # ≈8200 字
    "deep": 7600,      # ≈13500 字
}


# 生成后的编辑复检：最小改动 + 机械闸门（细节被抽掉就退回）
_POLISH_SYSTEM = """你是一位苛刻的终审编辑。有人刚写完一篇播客文章，你要修掉它的可读性缺陷，**但这是一次最小改动，不是重写**。

## 绝对禁止（违反任意一条，这次修改就作废）
- 不许删掉任何一个数字、日期、时长、金额、比例、时间戳
- 不许把具体细节概括成抽象说法（"1906 年 4 月 18 日清晨"不能改成"一场大地震之后"）
- 不许改动事实、人名、作品名、引文内容
- 不许重写没有违反下面清单的句子——只修有问题的地方，其余原文照抄
- 不许新增观点、评价或总结

## 只修这些问题
1. **篇幅**：超过 {hi} 字时，用「删掉整节」或「合并重复段落」来压缩，而不是把每句话改短、把细节删掉。目标 {lo}-{hi} 字。
2. **开头**：若开头 3 段不是具体场景/细节/反直觉事实（而在概括全文或罗列结论），只重写开头这几段。
3. **小标题**：含糊的（如"光与暗""另一个世界""秩序的执念"）改成含具体名词、数字或动作的；清楚的不要动。
4. **段落过长**：超过 200 字的段落拆开（只加换行，内容不动）。
5. **套话**：删掉这些词及其变体——值得注意的是、总的来说、综上所述、不难看出、毫无疑问、本期最大的信息增量、硬币的另一面、不仅…更是…、发人深省、引人深思、令人震撼、深刻的、让我们、我们看到、众所周知、可以说、从某种意义上、首先/其次/最后（作为分段组织）。
6. **引用格式**：时间戳必须与引文同行，形如 `> 引文 [时:分:秒]`；独占一行的时间戳要么并回引文，要么连同该引文一起删。全文引用不超过 6 条。
7. **重复**：同一句话、同一论点出现两次的，删掉后面那次。
8. **中文排版**：标点全角；数字与中文之间加半角空格（"1906 年 4 月 18 日"）；专有名词保留原文。
9. **结尾**：点评里"值得深挖""值得思考"这类敷衍表述，改成具体判断。

保持 Markdown 结构与标题层级不变。**只输出修改后的完整文章**，不要解释。"""

_CONCRETE_RE = re.compile(
    r"\d{4}\s*年|\d+\.?\d*\s*多?\s*[%个条位名岁年天倍万亿级种次轮人元月日周]|\[\d{2}:\d{2}:\d{2}\]"
)


def _concrete_tokens(text: str) -> Counter:
    """可查证的具体细节（数字、日期、时间戳），用来校验复检有没有抽掉血肉。"""
    return Counter(_CONCRETE_RE.findall(text))


def _density_ratio(before: str, after: str) -> float:
    """复检前后的「具体细节密度」之比。

    用密度而不是绝对数量：删掉整节时细节会同比减少（合理），
    把细节改写成抽象说法才会让密度掉下来（不合格）。
    """
    b, a = _concrete_tokens(before), _concrete_tokens(after)
    bd = sum(b.values()) / max(len(before), 1) * 1000
    ad = sum(a.values()) / max(len(after), 1) * 1000
    if bd == 0:
        return 1.0
    return ad / bd


def _polish(client, model: str, article: str, mode_key: str, log=print, on_chars=None) -> str:
    """最小改动的编辑复检 + 机械闸门：细节保留率不达标就退回上一稿。"""
    lo, hi = MODE_TARGETS.get(mode_key, MODE_TARGETS[DEFAULT_MODE])
    system = _POLISH_SYSTEM.replace("{lo}", str(lo)).replace("{hi}", str(hi))
    current = article

    for attempt in (1, 2, 3):
        if attempt == 1:
            ask = f"当前文章 {len(current)} 字（目标 {lo}-{hi} 字）。请按清单做最小改动。"
        else:
            ask = (
                f"当前文章仍有 {len(current)} 字，超出目标 {len(current) - hi} 字。"
                f"现在只做一件事：**整节删除**。挑出信息增量最少的一节（连同它的小标题）删掉，"
                f"让篇幅减少约 {len(current) - hi} 字。其余内容一字不改——保留所有数字、日期、时间戳与引文。"
            )
        log(f"[llm] 编辑复检 第 {attempt} 次（{len(current)} 字 → 目标 {lo}-{hi} 字）…")
        try:
            revised = _chat(client, model, system, f"{ask}\n\n{current}", log=log, on_chars=on_chars,
                            max_tokens=MODE_MAX_TOKENS.get(mode_key, 8192) + 500,
                            temperature=0.3, thinking=False)  # 关思考时 temperature 才生效
        except Exception as exc:  # 复检失败不该让整篇文章失败
            log(f"[llm] 复检跳过（{type(exc).__name__}: {exc}）")
            return current

        if len(revised.strip()) < len(current) * 0.35:
            log("[llm] 复检结果过短（疑似截断），保留上一稿")
            return current
        ratio = _density_ratio(current, revised)
        if ratio < 0.9:
            log(f"[llm] 复检让具体细节密度掉了 {(1 - ratio):.0%}（细节被抽象化）→ 判定不合格，保留上一稿")
            return current
        log(f"[llm] 复检通过：{len(current)} → {len(revised)} 字，细节密度 {ratio:.2f}×")
        current = revised
        if len(current) <= hi * 1.05:
            break
    return current


def _build_user_message(
    title: str,
    podcast: str,
    author: str,
    shownotes_html: str | None,
    transcript_text: str,
    profile: str = "",
) -> str:
    notes = html_to_text(shownotes_html)
    meta = f"节目标题：{title}\n播客/频道：{podcast}\n主播/作者：{author or '未知'}"
    parts = [meta]
    if profile:
        parts.append(profile)
    if notes:
        parts.append(f"以下是主播自己写的节目笔记（Official Show Notes），可作为内容地图参考：\n{notes[:4000]}")
    parts.append(f"以下是完整文字稿：\n\n{transcript_text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 分段精读

_CHUNK_SYSTEM = """你是内容编辑的助手。给你一期节目文字稿的一个片段（行首 [时:分:秒] 是时间戳），请提炼为结构化素材，供后续写成一篇让人愿意读完的文章。要求：
- 保留**可查证的具体细节**：人名、数字、时间、地点、物件、原话片段（这些是成文的血肉，不要只留抽象结论）
- 记录论点与推理链条、案例与数据，全部标注真实时间戳
- 摘录 1-3 条最锋利、最有画面感的原话（标注时间戳）
- 注意记录**冲突、反转、代价、分歧**——成文时它们比平铺的结论有价值得多
- 忠于原文，不要评论、不要脑补
- 用简体中文，输出紧凑的 bullet list，不要客套话"""


def _chunk_segments(segments: list[dict], chunk_chars: int) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for seg in segments:
        cur.append(seg)
        size += len(seg["text"])
        if size >= chunk_chars:
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        if chunks and size < chunk_chars * 0.3:  # 尾巴太小并入上一块
            chunks[-1].extend(cur)
        else:
            chunks.append(cur)
    return chunks


def _segments_to_text(segments: list[dict]) -> str:
    return "\n".join(f"[{ts_clock(s['start'])}] {s['text']}" for s in segments)


def _digest_segments(client, model: str, segments: list[dict], max_chars: int,
                     log=print, progress=None) -> str:
    """长文字稿：分段精读成素材摘要（供分节写作或单次汇总使用）。"""
    chunks = _chunk_segments(segments, chunk_chars=max(4_000, max_chars // 3))
    log(f"[llm] 文字稿较长，分段精读：{len(chunks)} 段")
    briefs: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        log(f"[llm] 精读第 {i}/{len(chunks)} 段…")
        if progress:
            progress("llm", {"chunk": i, "total": len(chunks)})
        briefs.append(
            _chat(client, model, _CHUNK_SYSTEM, _segments_to_text(chunk), log=log,
                  max_tokens=4096, thinking=False)
        )
    return "\n\n".join(f"## 第 {i} 段素材\n{b}" for i, b in enumerate(briefs, 1))


def _guard(article: str, log=print) -> str:
    """空产出守卫：宁可让任务报错重试，也不要保存一篇空文章。"""
    text = (article or "").strip()
    if len(text) < 400:
        raise RuntimeError(
            f"生成结果异常：仅 {len(text)} 字。常见原因是思考模式吃光 token 预算，"
            "或 token 上限过小。"
        )
    return text


def _finalize(article: str, mode_key: str, log=print) -> str:
    """确定性收尾：拆超长段落、修孤立时间戳、删套话、规范标点、按档位裁到篇幅内。"""
    text, report = postprocess.finalize(article, mode_key)
    bits = [f"{k}={v}" for k, v in report.items() if k != "最终字数" and v]
    log(f"[llm] 排版收尾：{('，'.join(bits) if bits else '无需修正')}，最终 {len(text)} 字")
    return text


def _system_prompt(mode: str | None) -> str:
    """拼出该篇幅档位的系统提示词（用 replace 而非 format，避免结构里的花括号被解析）。"""
    structure = STRUCTURES.get(mode or DEFAULT_MODE, STRUCTURES[DEFAULT_MODE])
    return _EDITOR_SYSTEM.replace("{structure}", structure)


def write_article(
    segments: list[dict],
    title: str,
    podcast: str,
    author: str,
    shownotes_html: str | None,
    max_chars: int = 75_000,
    llm_model: str | None = None,
    mode: str | None = None,
    polish: bool = False,
    outlined: bool = True,
    log=print,
    progress=None,
) -> str:
    """输入带时间戳的转写片段，输出 Markdown 文章。progress 同 Pipeline。

    mode:     篇幅档位（concise / standard / deep），决定节数与各节预算
    outlined: 是否用「大纲 → 逐节写作」流程（篇幅可控、质量更稳）；失败自动退回单次生成
    polish:   是否额外让模型自查一遍（可选，默认关；格式纪律已由确定性后处理保证）
    """
    client = _client()
    model = llm_model or config.deepseek_model()
    profile = settings.profile_text()  # 设置页里填的个人资料（可为空）
    system = _system_prompt(mode)
    mode_key = mode if mode in STRUCTURES else DEFAULT_MODE

    def on_chars(n: int) -> None:
        if progress:
            progress("llm", {"chars": n})

    user_msg = _build_user_message(
        title, podcast, author, shownotes_html, _segments_to_text(segments), profile
    )
    short_enough = len(user_msg) <= max_chars

    # ---- 主路径：大纲 + 逐节写作（篇幅是算出来的，不是求出来的）
    if outlined:
        try:
            if short_enough:
                material_body = _segments_to_text(segments)
            else:
                material_body = _digest_segments(client, model, segments, max_chars, log, progress)
            material = outline.build_material(
                title, podcast, author, shownotes_html, material_body
            )
            log(f"[llm] 分节写作 · {mode_key} 档｜素材 {len(material_body)} 字符，"
                f"预算约 {outline.budget_total(mode_key)} 字")
            article = outline.write_outlined(client, model, material, mode_key, log, progress)
            return _finalize(_guard(article, log), mode_key, log)
        except Exception as exc:
            log(f"[llm] 分节写作失败（{type(exc).__name__}: {exc}），退回单次生成")

    if short_enough:
        log(f"[llm] 单次直读 · {mode_key} 档：全文 {len(user_msg)} 字符，模型 {model}")
        article = _chat(client, model, system, user_msg, log=log, on_chars=on_chars,
                        max_tokens=MODE_MAX_TOKENS.get(mode_key, 8192) + THINKING_ALLOWANCE,
                        thinking=False)
        if polish:
            article = _polish(client, model, article, mode_key, log=log, on_chars=on_chars)
        return _finalize(article, mode_key, log)

    # 分段精读 -> 汇总
    digest = _digest_segments(client, model, segments, max_chars, log, progress)
    notes = html_to_text(shownotes_html)
    final_user = (
        f"节目标题：{title}\n播客/频道：{podcast}\n主播/作者：{author or '未知'}\n\n"
        + (f"{profile}\n\n" if profile else "")
        + (f"主播的节目笔记：\n{notes[:4000]}\n\n" if notes else "")
        + "以下是各段文字稿的结构化提炼素材（时间戳均来自原稿）：\n\n"
        + digest
        + "\n\n请基于这些素材，按系统要求的篇幅与结构写出最终文章。"
        "所有时间戳必须来自素材中真实出现的时间点；素材里的具体细节（人名、数字、场景、原话）要写进正文，不要压成抽象结论。"
    )
    log(f"[llm] 汇总成文 · {mode_key} 档（{len(final_user)} 字符素材）")
    article = _chat(client, model, system, final_user, log=log, on_chars=on_chars,
                    max_tokens=MODE_MAX_TOKENS.get(mode_key, 8192) + THINKING_ALLOWANCE,
                    thinking=False)
    if polish:
        article = _polish(client, model, article, mode_key, log=log, on_chars=on_chars)
    return _finalize(_guard(article, log), mode_key, log)
