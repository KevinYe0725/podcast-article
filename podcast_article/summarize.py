"""DeepSeek 精读提炼：文字稿 -> 深度文章。

策略：
- 文字稿不太长（<= max_chars）：单次直读全文，整体把握最好
- 太长：分段精读（保留时间戳）-> 汇总成文
"""
from __future__ import annotations

from openai import OpenAI

from . import config
from .util import html_to_text, ts_clock


def _client() -> OpenAI:
    return OpenAI(api_key=config.deepseek_api_key(), base_url=config.DEEPSEEK_BASE_URL)


def _chat(client: OpenAI, model: str, system: str, user: str, log=print, max_tokens: int = 8192, on_chars=None) -> str:
    """流式调用，边生成边打印一个简单的进度点。on_chars(已生成字数) 用于实时进度。"""
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=1.0,
        max_tokens=max_tokens,
        stream=True,
    )
    parts: list[str] = []
    printed = 0
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            parts.append(delta)
            n = len("".join(parts))
            if on_chars:
                on_chars(n)
            if len(parts) >= printed + 400:
                printed = len(parts)
                log(f"[llm] 已生成 {n} 字…")
    return "".join(parts).strip()


# ---------------------------------------------------------------- 单次直读

_EDITOR_SYSTEM = """你是一位顶尖的内容编辑与专栏作者。你会收到一期播客或视频的完整文字稿（每行行首是 [时:分:秒] 时间戳），任务是把它从"听完就会忘"变成"读完能记住、值得收藏转发"的深度文章。

写作要求：
1. 先通读全文建立骨架（主题 → 论点 → 论据 → 结论），再动笔；不要按时间流水账复述。
2. 忠于原意，绝不编造；把"事实/数据"与"个人观点"区分开；嘉宾说了什么、主持人怎么追问，该归属清楚。
3. 你提炼的洞察密度要高：删掉寒暄、广告、重复、跑题，留下真正有信息增量的内容。
4. 所有时间戳必须直接取自文字稿中真实存在的位置。
5. 用简体中文写作（专有名词保留原文）；面向没听过这期节目的读者，行文自成一体。

输出为 Markdown，结构如下：
# {自拟标题：准确且有钩子，不要照抄原标题}
> {一句话核心总结，50 字以内}

## 内容速览
{3-6 条 bullet，每条一句话，让人 30 秒了解全貌}

## 核心内容
{按内容的自然逻辑组织成 2-5 个章节。每章：
### {章节小标题}
- 这一节讲了什么：论点、论据、案例、数据，写清楚推理链条
- 引用 1-2 处最有价值的原文（标注 [时:分:秒]）}

## 金句摘录
{3-8 条直接引自文字稿的原话，每条标注 [时:分:秒]}

## 提及的书影音 / 人物 / 概念
{Markdown 表格：名称 | 是什么 | 一句话说明为什么值得了解；没有则写"无"}

## 编辑点评
{你的批判性思考，3-5 条：哪些论证薄弱或以偏概全、哪些观点值得商榷、哪些点值得深挖成新内容}
"""


def _build_user_message(
    title: str, podcast: str, author: str, shownotes_html: str | None, transcript_text: str
) -> str:
    notes = html_to_text(shownotes_html)
    meta = f"节目标题：{title}\n播客/频道：{podcast}\n主播/作者：{author or '未知'}"
    parts = [meta]
    if notes:
        parts.append(f"以下是主播自己写的节目笔记（Official Show Notes），可作为内容地图参考：\n{notes[:4000]}")
    parts.append(f"以下是完整文字稿：\n\n{transcript_text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 分段精读

_CHUNK_SYSTEM = """你是内容编辑的助手。给你一期节目文字稿的一个片段（行首 [时:分:秒] 是时间戳），请提炼为结构化素材，供后续汇总成文。要求：
- 提炼该片段的主题、论点与推理链条、案例与数据（全部标注真实时间戳）
- 摘录 1-3 条最有价值的原话（标注时间戳）
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


def write_article(
    segments: list[dict],
    title: str,
    podcast: str,
    author: str,
    shownotes_html: str | None,
    max_chars: int = 75_000,
    llm_model: str | None = None,
    log=print,
    progress=None,
) -> str:
    """输入带时间戳的转写片段，输出 Markdown 文章。progress 同 Pipeline。"""
    client = _client()
    model = llm_model or config.deepseek_model()

    def on_chars(n: int) -> None:
        if progress:
            progress("llm", {"chars": n})

    user_msg = _build_user_message(title, podcast, author, shownotes_html, _segments_to_text(segments))

    if len(user_msg) <= max_chars:
        log(f"[llm] 单次直读模式：全文 {len(user_msg)} 字符，模型 {model}")
        return _chat(client, model, _EDITOR_SYSTEM, user_msg, log=log, on_chars=on_chars)

    # 分段精读 -> 汇总
    chunks = _chunk_segments(segments, chunk_chars=max(4_000, max_chars // 3))
    log(f"[llm] 文字稿较长（{len(user_msg)} 字符），分段精读：{len(chunks)} 段")
    briefs: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        log(f"[llm] 精读第 {i}/{len(chunks)} 段…")
        if progress:
            progress("llm", {"chunk": i, "total": len(chunks)})
        briefs.append(
            _chat(client, model, _CHUNK_SYSTEM, _segments_to_text(chunk), log=log,
                  max_tokens=4096, on_chars=on_chars)
        )

    digest = "\n\n".join(f"## 第 {i} 段素材\n{b}" for i, b in enumerate(briefs, 1))
    notes = html_to_text(shownotes_html)
    final_user = (
        f"节目标题：{title}\n播客/频道：{podcast}\n主播/作者：{author or '未知'}\n\n"
        + (f"主播的节目笔记：\n{notes[:4000]}\n\n" if notes else "")
        + "以下是各段文字稿的结构化提炼素材（时间戳均来自原稿）：\n\n"
        + digest
        + "\n\n请基于这些素材，按系统要求写出最终文章。所有时间戳必须来自素材中真实出现的时间点。"
    )
    log(f"[llm] 汇总成文（{len(final_user)} 字符素材）")
    return _chat(client, model, _EDITOR_SYSTEM, final_user, log=log, on_chars=on_chars)
