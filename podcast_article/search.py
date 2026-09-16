"""跨单集全文检索：线性扫描每集的 article.md 与 transcript.txt（供 webapp 搜索框用）。

    from podcast_article.search import parse_query, search, stats
    search(OUTPUT_ROOT, "强化学习|RL 训练")

设计取舍
--------
- **不建持久化索引**。一个 output/ 也就 30~100 集，单集几十 KB 到几百 KB，
  线性扫描单集 <10ms，比维护索引文件（生成、失效、增量更新、并发写）省事得多，
  也不会往 output/ 里塞额外文件。
- **纯标准库**。只用 re 做大小写不敏感的定位，且直接在原文上匹配：
  偏移量本来就属于原文，切 before/match/after 时天然对得上，也不会切坏中文。
- root 由调用方传入（webapp 用 PA_OUTPUT_DIR 解出的 OUTPUT_ROOT），模块里不存
  全局路径，测试才能把它指到临时目录。

相关度公式（score，越大越相关）
------------------------------
    score = (Σ_i W(field_i) × pos_i) × (1 + COUNT_LOG × log2(命中次数)) + 标题加成
    命中次数 = 该集所有字段里所有候选词的全部命中次数（含没进 hits 的那些）
    pos_i    = 1 + POS_BONUS × (1 - 命中位置 / 文档长度)     ∈ [1.0, 1.5]
    标题加成 = TITLE_BONUS，只要 title 命中过一次就给

为什么这么定：
- W_TITLE=12 远高于正文 1：标题命中说明「这一集讲的就是这个词」，而不是顺带提一句，
  必须压过正文里出现的几次。
- article 的 H1/H2 是文章骨架（W=4），`> ` 引语是摘要作者挑出来当「重点句」的
  （W=3），正文基线 1；文字稿 0.6 —— ASR 口语又长又碎、还常和文章重复，
  不该盖过文章本身。
- pos：文章和播客都爱把主旨放在开头，命中越靠前越可能是主题而不是路过。
- 次数用 log2 而非线性：两个小时的文字稿随便提二十次就能碾压短文章，
  次线性成长让「命中密集」加分，但不会靠体量取胜。
- 同一 span 被多个 OR 候选重复命中只算一次（见 _find），
  否则 ``AI|ai`` 这种查询会把计数翻倍。
"""
from __future__ import annotations

import bisect
import json
import math
import re
from pathlib import Path

# --------------------------------------------------------------- 权重常量
W_TITLE = 12.0        # meta.json 的 title 命中
W_HEADING = 4.0       # article.md 的一级/二级（及更深）标题行
W_QUOTE = 3.0         # article.md 的引语行（"> ..."）
W_BODY = 1.0          # article.md 正文
W_TRANSCRIPT = 0.6    # transcript.txt（口语转写，噪声大）

POS_BONUS = 0.5       # 位置加成上限：最前 1.5 倍，末尾 1.0 倍
COUNT_LOG = 0.25      # 次数加成系数（log2）
TITLE_BONUS = 6.0     # 标题命中的「主题加成」
CONTEXT = 40          # 命中点左右各取的上下文字符数

# field -> 行类别 -> 权重；行类别为空串表示该字段不分行
_WEIGHTS: dict[str, dict[str, float]] = {
    "title": {"": W_TITLE},
    "article": {"heading": W_HEADING, "quote": W_QUOTE, "body": W_BODY},
    "transcript": {"": W_TRANSCRIPT},
}

# 文字稿行首时间戳，形如 [00:10:07]（小时 1~2 位）
_TS_RE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]")


# --------------------------------------------------------------- 查询解析

def _tokenize(query: str) -> list[tuple[str, bool]]:
    """按空白切词，双引号内的空白不拆；返回 [(词, 是否带引号)]。

    引号字符本身被丢掉；不成对的引号按「从这里到结尾都在引号内」处理
    （用户少打一个引号时不会把剩下的词丢掉）。
    """
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    in_quote = False        # 词法状态：现在是否在引号里
    quoted = False          # 当前这个词有没有碰到过引号
    for ch in query or "":
        if ch == '"':
            in_quote = not in_quote
            continue
        if not in_quote and ch.isspace():
            if buf:
                tokens.append(("".join(buf), quoted))
                buf, quoted = [], False
            continue
        buf.append(ch)
        if in_quote:
            quoted = True
    if buf:
        tokens.append(("".join(buf), quoted))
    return tokens


def parse_query(query: str) -> list[list[str]]:
    """把用户输入拆成 AND 组，每组是 OR 候选。

    规则：空格分词 = AND；同一词用 | 分隔 = OR（例如 `强化学习|RL`）；
    英文/中文都按大小写不敏感匹配；`"两个词"` 里的空格不拆（当成一个词组）。
    返回 [] 表示空查询。

    引号里的内容按字面处理，不再按 | 拆分（`"a|b"` 就是包含竖线的词组）。
    """
    groups: list[list[str]] = []
    for text, quoted in _tokenize(query):
        if quoted:
            alts = [text.strip()]                       # 词组：整体一个候选
        else:
            alts = [part.strip() for part in text.split("|")]
        alts = [a for a in alts if a]                   # 丢掉空候选（"a||b"、"词|"）
        if alts:
            groups.append(alts)
    return groups


def _needs_word_boundary(term: str) -> bool:
    """纯 ASCII 的词要按「整词」匹配，中文/日文这类没有词边界的则按子串匹配。

    原因很实际：搜 `AI` 时按子串匹配会把 `Fails`、`derail` 也算命中（实测搜 "AI" 时
    一篇讲 Clarity Act 的文章排进了结果，命中的是 "F**ai**ls"）。而中文本来就靠子串
    检索（"鱼" 要能命中 "鱼不存在"），加 \\b 反而会失效。
    """
    return bool(term) and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in term)


def _compile(groups: list[list[str]]) -> list[tuple[int, str, re.Pattern]]:
    """(组号, 词, 大小写不敏感的正则)。用 re.escape 把词当字面量，偏移量属于原文。"""
    patterns: list[tuple[int, str, re.Pattern]] = []
    for gi, alts in enumerate(groups):
        for alt in alts:
            body = re.escape(alt)
            # (?<![\\w-]) / (?![\\w-]) 而不是 \\b：术语里常带连字符（gpt-4、tcp-ip），
            # \\b 在连字符处也成立，会把 "gpt-4" 切成两半。
            pattern = (
                rf"(?<![\w-]){body}(?![\w-])" if _needs_word_boundary(alt) else body
            )
            patterns.append((gi, alt, re.compile(pattern, re.IGNORECASE)))
    return patterns


# --------------------------------------------------------------- 文本工具

def _read_text(path: Path) -> str | None:
    """读文本；不存在、是目录、无权限、编码坏掉都返回 None（不抛）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _load_meta(d: Path) -> dict:
    """读 meta.json；坏 JSON / 不是对象 / 读不了都当成空 dict。"""
    try:
        data = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _title_of(meta: dict, fallback: str) -> str:
    raw = meta.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return fallback


def _line_index(text: str) -> tuple[list[int], list[str]]:
    """每行的起始偏移与原始行文本（保留换行符，方便按偏移换算行号）。"""
    starts: list[int] = []
    lines: list[str] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        starts.append(pos)
        lines.append(line)
        pos += len(line)
    if not starts:                       # 空文本也保证有一个起点
        starts, lines = [0], [""]
    return starts, lines


def _locate(starts: list[int], lines: list[str], offset: int) -> tuple[int, str]:
    """偏移 -> (1-based 行号, 该行原文)。"""
    idx = max(0, min(bisect.bisect_right(starts, offset) - 1, len(lines) - 1))
    return idx + 1, lines[idx]


def _ts_of(line: str) -> str | None:
    """从行首解析 `[hh:mm:ss]`；解析不出来给 None。"""
    m = _TS_RE.match(line.lstrip())
    return m.group(0)[1:-1] if m else None


def _article_kind(line: str) -> str:
    """article.md 的行类别：标题 / 引语 / 正文。"""
    stripped = line.strip()
    if stripped.startswith("#"):
        return "heading"
    if stripped.startswith(">"):
        return "quote"
    return "body"


def _find(text: str, patterns: list[tuple[int, str, re.Pattern]]) -> list[tuple[int, int, int, str]]:
    """在 text 里找出所有候选词的命中，按位置排序；同 span 只保留一次。

    返回 [(起始偏移, 结束偏移, 组号, 命中词)]，偏移相对于原文。
    """
    found: list[tuple[int, int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for gi, term, pat in patterns:
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if span in seen:                # "AI|ai" 之类重复命中的同一个 span
                continue
            seen.add(span)
            found.append((span[0], span[1], gi, term))
    found.sort(key=lambda o: (o[0], o[1]))
    return found


def _snippet(field: str, text: str, start: int, end: int, line_no: int, ts: str | None) -> dict:
    """命中点前后各取 CONTEXT 个字符（按字符切，中文安全），被截断的一端补 …。"""
    b0 = max(0, start - CONTEXT)
    a1 = min(len(text), end + CONTEXT)
    before = text[b0:start]
    after = text[end:a1]
    if b0 > 0:
        before = "…" + before
    if a1 < len(text):
        after = after + "…"
    return {
        "field": field,
        "ts": ts,
        "before": before,
        "match": text[start:end],           # 原文大小写
        "after": after,
        "line": line_no,
    }


def _weight(field: str, kind: str) -> float:
    """字段 + 行类别 -> 单次命中权重。"""
    table = _WEIGHTS[field]
    return table.get(kind, table.get("", W_BODY))


# --------------------------------------------------------------- 单集扫描

def _scan(d: Path, groups: list[list[str]], patterns: list[tuple[int, str, re.Pattern]],
          per_field: int) -> dict | None:
    """扫一集；没有任何命中（或缺某一 AND 组）返回 None，否则返回结果项。"""
    meta = _load_meta(d)
    title = _title_of(meta, d.name)

    article_path = d / "article.md"
    has_article = article_path.exists()
    article = _read_text(article_path) if has_article else None
    transcript = _read_text(d / "transcript.txt")

    # 标题永远可搜（缺失/坏 meta.json 时就是目录名，所见即所搜）
    sources: list[tuple[str, str]] = [("title", title)]
    if article:
        sources.append(("article", article))
    if transcript:
        sources.append(("transcript", transcript))

    hits: dict[str, list[dict]] = {"title": [], "article": [], "transcript": []}
    matched_groups: set[int] = set()
    count = 0
    score = 0.0
    title_hit = False

    for field, text in sources:
        length = max(1, len(text))
        starts: list[int] = []
        lines: list[str] = []
        if field != "title":                    # 标题只有一行，不需要行索引
            starts, lines = _line_index(text)
        for start, end, gi, _term in _find(text, patterns):
            matched_groups.add(gi)
            count += 1
            if field == "title":
                line_no, kind, ts = 1, "", None
            else:
                line_no, raw_line = _locate(starts, lines, start)
                if field == "transcript":
                    kind, ts = "", _ts_of(raw_line)
                else:
                    kind, ts = _article_kind(raw_line), None
            weight = _weight(field, kind)
            score += weight * (1.0 + POS_BONUS * (1.0 - start / length))
            if field == "title":
                title_hit = True
            if len(hits[field]) < per_field:        # 每字段只留前 per_field 条
                hits[field].append(_snippet(field, text, start, end, line_no, ts))

    if count == 0 or len(matched_groups) < len(groups):   # AND：每组都要命中
        return None

    score *= 1.0 + COUNT_LOG * math.log2(count)           # 命中次数次线性加成
    if title_hit:
        score += TITLE_BONUS

    merged = (hits["title"] + hits["article"] + hits["transcript"])[: per_field * 2]
    return {
        "dir": d.name,
        "title": title,
        "podcast": str(meta.get("podcast") or ""),
        "has_article": has_article,
        "match_count": count,
        "score": round(score, 4),
        "hits": merged,
    }


# --------------------------------------------------------------- 对外接口

def search(root: Path, query: str, *, limit: int = 30, per_field: int = 3) -> list[dict]:
    """在 root 下每一集里检索 article.md 与 transcript.txt（若存在）。

    返回按相关度倒序的列表（同分按目录名排，保证结果稳定），每项：

        {
          "dir": "<目录名>",
          "title": "<meta.json 的 title，缺失时用目录名>",
          "podcast": "<meta.json 的 podcast，可能为空串>",
          "has_article": bool,
          "match_count": int,          # 该集所有命中次数
          "score": float,              # 相关度，用于排序（公式见模块 docstring）
          "hits": [                    # 最多 per_field*2 条，标题命中优先
            {
              "field": "title" | "article" | "transcript",
              "ts": "00:10:07" | None, # 仅 transcript 命中时给出该行的时间戳，供前端跳到音频
              "before": "...",         # 命中点之前的上下文
              "match":  "...",         # 命中的原文（原样大小写）
              "after":  "...",         # 之后的上下文
              "line": int              # 1-based 行号
            }
          ]
        }

    没有命中任何一集时返回 []。空查询、root 不存在同样返回 []；读不了的文件、
    坏掉的 meta.json 都跳过而不抛异常。
    """
    groups = parse_query(query)
    if not groups:
        return []
    root = Path(root)
    if not root.is_dir():
        return []
    patterns = _compile(groups)
    per_field = max(1, int(per_field))

    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    results: list[dict] = []
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        try:
            item = _scan(d, groups, patterns, per_field)
        except OSError:                     # 目录半路消失 / 无权限：跳过这一集
            continue
        if item is not None:
            results.append(item)

    results.sort(key=lambda r: (-r["score"], r["dir"]))
    return results[: max(0, int(limit))]


def stats(root: Path) -> dict:
    """{"episodes": 可检索集数, "indexed": 有 article 或 transcript 的集数}，供界面显示。

    口径：root 下的子目录里，只要存在 meta.json / article.md / transcript.txt 之一
    就算一集（meta.json 坏掉也算，因为它仍然会被列出来）；「indexed」指其中有正文
    或文字稿、真正能搜到内容的集数。目录不存在时两项都是 0。
    """
    root = Path(root)
    if not root.is_dir():
        return {"episodes": 0, "indexed": 0}
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return {"episodes": 0, "indexed": 0}

    episodes = indexed = 0
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        has_meta = (d / "meta.json").exists()
        has_article = (d / "article.md").exists()
        has_transcript = (d / "transcript.txt").exists()
        if not (has_meta or has_article or has_transcript):
            continue
        episodes += 1
        if has_article or has_transcript:
            indexed += 1
    return {"episodes": episodes, "indexed": indexed}
