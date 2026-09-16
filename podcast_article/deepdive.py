"""划词深挖：选中一段话 + 一个疑问 → 结合本期原文与网络资料的解读（流式）。

链路：`retrieve()`（在这一集文字稿里找相关片段）→ 可选联网 → `build_prompt()` → `stream_answer()`

设计取舍
--------
- **不引依赖、不分词**。项目里没有分词库，也不打算为一个侧边抽屉装 jieba：`keywords()`
  用「英文按词 + 中文 2/3-gram」造检索词，靠文字稿里的**子串命中**就够用——多造的候选词
  如果没命中，只会让 idf 表里多几个用不上的条目，不会带来假命中（真命中了还能加分）。
- **让 IDF 决定谁排前面**。文字稿里「今天」「我们」遍地都是，命中它说明不了任何事；
  有用的是罕见词。所以打分是 Σ idf(词)，不是数命中次数（公式见 `_idf` 与 `retrieve`）。
- **引文必须可核对**。每条片段的行首都带 `[时:分:秒]`（与文字稿、与文章流水线同一格式），
  提示词里要求模型引用原话时逐字照抄并带上行首那个时间戳——前端靠它做「点回听」。
- **不编造**由提示词负责（见 `_SYSTEM`），代码这边只做一件事：把「原文里有什么、没有什么」
  如实摆到模型面前（片段为空时明确说没有），让它能照实说「这一期的原文里没有覆盖」。
- 模型调用复用 `summarize._chat`（流式、关思考、usage 记账、空正文报错、截断告警都在那里）。
  它只暴露 `on_chars(累计字数)`，给不出逐块文本，所以这里用 `_tee_client` 在客户端外面包一层，
  顺手把 `delta.content` 交给 `on_delta`，它的逻辑一行不改。
- **这一层不抛异常**。调用方是流式界面，抛出去用户什么都看不到：所有失败都变成
  `{"error": "...人话..."}`，能出的部分照出。
"""
from __future__ import annotations

import importlib
import json
import math
import re
import types
from pathlib import Path

from . import config, outline, settings, summarize, usage
from .summarize import _chat
from .util import ts_clock

TRANSCRIPT_FILE = "transcript.json"

MIN_SEGMENTS = 2      # 少于 2 个片段就没有「相邻上下文」可言，多半是转写没跑完
KEYWORD_LIMIT = 24    # 交给检索的候选词上限（长词优先，见 keywords）
TF_LOG = 0.25         # 同一片段里词频的次线性加成（与 search.COUNT_LOG 同量级）
POS_BONUS = 0.25      # 位置偏置上限：最前段 1.25 倍，末尾段 1.0 倍

# 回答篇幅档位。默认 concise —— 这是用户反馈改的：
# 原来的 400-900 字实际产出 914 字 / 7 段，但**只有 1 段是回答他问的那个问题的**，
# 其余是模型自己觉得「也很有意思」的点（「另一个容易误读的点是…」「…也值得停一下」）。
# 所以问题不只是长，而是**答非所问**：只把字数调小仍会跑偏，必须在提示词里禁止主动扩展。
ANSWER_MODES: dict[str, dict] = {
    # lo/hi 是给模型看的字数区间；ask 是按 outline.stated() 打折后的报价
    # （outline 的实测结论：模型对字数稳定写到报价的 1.5-2 倍）
    "concise": {"lo": 120, "hi": 320, "para_lo": 1, "para_hi": 3,
                "quotes": 2, "followups": 1, "label": "简洁"},
    "detail": {"lo": 400, "hi": 900, "para_lo": 3, "para_hi": 6,
               "quotes": 4, "followups": 2, "label": "详细"},
}
DEFAULT_ANSWER_MODE = "concise"


def answer_mode(mode: str | None) -> dict:
    """取档位配置（未知值回退到默认档）。"""
    return ANSWER_MODES.get((mode or "").strip() or DEFAULT_ANSWER_MODE,
                            ANSWER_MODES[DEFAULT_ANSWER_MODE])


def answer_limits(mode: str | None = None) -> tuple[int, int]:
    """返回 (报价字数, token 机械上限)。"""
    plan = answer_mode(mode)
    ask = outline.stated(plan["hi"])
    # 上限只挡「跑飞成几千字」，不承担篇幅控制（靠它压字数会在半句话处截断）
    cap = max(200, int(plan["hi"] * outline.TOKENS_PER_CHAR * 1.6))
    return ask, cap


ANSWER_TEMPERATURE = 0.4
# 这是「讲清楚 + 不许编」的任务，不是散文创作：thinking 关闭时 temperature 才生效，
# 取 0.4 让它贴住材料（写文章那条链路用的是 0.9，那是要它发挥）。
ANSWER_TEMPERATURE = 0.4

WEB_SNIPPETS = 5      # 给模型看的网络结果条数上限
# 「清点材料」的闸门先攒多少字再检查。实测那几句废话都出现在前 260 字内
# （第一段或第二段开头），攒到这个长度足够判断，又不会让首字延迟太久。
GATE_CHARS = 260
# 单条片段最多保留多少行原文。实测（3528 段的长访谈）命中区域可能连着几十句都在讲同一个词，
# 那种片段不是更好的证据，只会把其他片段挤掉、把 token 和注意力都吃掉。
MAX_PASSAGE_SEGMENTS = 24


# ---------------------------------------------------------------- 文字稿

def _num(value) -> float | None:
    """把 start 之类的字段转成有限浮点数；转不了给 None（不抛）。"""
    if isinstance(value, bool):        # bool 是 int 的子类，但 True 不是时间点
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None   # NaN/Infinity 会让 ts_clock 炸掉


def load_segments(workdir: Path) -> list[dict]:
    """读 `output/<dir>/transcript.json` → `[{"start": 秒, "text": "..."}]`。

    文件缺失、坏 JSON、根不是 list、片段不是 dict、正文缺失或空白、start 不是有限数字，
    一律**跳过或返回空表，绝不抛异常**（转写失败的集、手改坏的 json 都会走到这里）。
    结果按 start 升序——合并窗口时依赖这个顺序。
    """
    try:
        raw = json.loads((Path(workdir) / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):      # 含 JSONDecodeError / UnicodeDecodeError
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        start = _num(item.get("start"))
        if not isinstance(text, str) or not text.strip() or start is None:
            continue
        out.append({"start": max(0.0, start), "text": text.strip()})
    out.sort(key=lambda seg: seg["start"])
    return out


# ---------------------------------------------------------------- 检索词

_CJK = r"\u4e00-\u9fff"
_CJK_RUN_RE = re.compile(f"[{_CJK}]+")
_EN_RE = re.compile(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*")

# 中文停用词。两层用法：
#   1. 词本身等于停用词 → 丢（「一个」「因为」）
#   2. 词里**每个字**都是虚词字 → 丢（「的都」「就是」这种纯虚词组合没有检索价值；
#      只要有一个实词字就留下，「一致」「中美」「有人」都能活下来）
CH_STOPWORDS = frozenset("""
的 了 是 在 和 就 都 而 及 与 着 或 一个 我们 你们 他们 这个 那个 因为 所以 但是 如果
没有 可以 什么 怎么 也 还 很 把 被 这 那 我 你 他 她 它 们 个 之 并 且 为 对 从 到 有 不
上 下 中 里 会 能 要 用 说 去 来 又 再 只 才 更 但 却 让 给 地 得 过 嘛 啊 吧 呢 吗 呀 其
以 于 由 内 外 时 就 算 好 多 少 大 小 前 后 一 二 三 两 些 这里 那里 什么时候
""".split())
EN_STOPWORDS = frozenset("""
a an the and or but if then than that this these those is are was were be been being am
do does did doing done have has had having will would shall should can could may might must
i you he she it we they me him her us them my your his its our their mine yours
of to in on at by for with from into over under about after before up down out off
as so such not no nor only own same too very just also there here what which who whom
when where why how all any both each few more most other some again once because while
during between through against above below s t d ll re ve m don isn aren wasn weren
""".split())

_FUNC_CHARS = frozenset("".join(CH_STOPWORDS))


def _is_stopword(term: str) -> bool:
    """停用词判定（规则见 CH_STOPWORDS 的注释）。"""
    if term in CH_STOPWORDS or term in EN_STOPWORDS:
        return True
    if _CJK_RUN_RE.fullmatch(term):
        return all(ch in _FUNC_CHARS for ch in term)
    return False


def keywords(text: str, *, limit: int = 24) -> list[str]:
    """从选中文字里抽检索词，用来在文字稿里做**子串命中**（不需要真的分词）。

    - 英文/数字：按词抽（连字符、撇号算词内，如 `gpt-4o`、`don't`），长度 >= 2，转小写
    - 中文：每段连续汉字切出全部 **2-gram 与 3-gram**（「强化学习」→ 强化/化学/学习/
      强化学/化学习），另外把长度 <= 4 的整段（「强化学习」本身）也保留
    - 丢掉中英文停用词（规则见 CH_STOPWORDS）
    - 去重后**长词优先**排序（长词更精确，先在检索里用上），同长按出现顺序，截断到 limit

    多抽几个候选词是免费的：命中了算加分，没命中就躺在 idf 表里不参与打分。
    """
    if not text or not isinstance(text, str):
        return []
    limit = max(0, int(limit))
    if not limit:
        return []

    seen: dict[str, None] = {}          # 保序去重

    def add(term: str) -> None:
        if term and term not in seen:
            seen[term] = None

    for run in _CJK_RUN_RE.findall(text):
        n = len(run)
        if n <= 4:
            add(run)
        for size in (2, 3):
            for i in range(n - size + 1):
                add(run[i : i + size])
    for word in _EN_RE.findall(text):
        lowered = word.lower()
        if len(lowered) >= 2:
            add(lowered)

    kept = [t for t in seen if not _is_stopword(t)]
    order = {term: i for i, term in enumerate(kept)}
    kept.sort(key=lambda term: (-len(term), order[term]))
    return kept[:limit]


# 网络搜索的查询串上限：搜索引擎对超长中文短语会退化成「只匹配其中一个字」
QUERY_MAX_CHARS = 60
QUERY_MAX_TERMS = 4
QUERY_MAX_CJK = 6          # 中文片段最长留几个字（更长的多半是整句描述，检索价值低）
# 片段首尾要剥掉的虚词/代词。**只收明确的虚词**：
# 教训是别把「时、上、下、前、里、中、会、来」这类也放进来 —— 「时机选择」
# 会被剥成「机选择」，「多模态上的表现」会被剥成奇怪的东西。
_EDGE_CHARS = "的了是在和与就都而及之其为以于对也把被让给从当这那他我你们个什么吗呢吧啊"
# 切分长片段用的虚词表（比 _EDGE_CHARS 更激进一点：长片段按它们切成小段）。
# 注意名字不要跟上面的 _FUNC_CHARS 撞 —— 那是「停用词字集合」，被覆盖掉会让
# _is_stopword 失效（「是一」这种纯虚词 2-gram 就会漏进关键词里）。
_SPLIT_CHARS = _EDGE_CHARS + "候再就很要有没有不"


def search_terms(selection: str, question: str = "", *,
                 max_terms: int = QUERY_MAX_TERMS) -> list[str]:
    """挑出用于**联网检索**的词（也是相关性过滤的判据，两者必须是同一批词）。

    真机实测结论（决定了这里的取舍）：
      `SGLang` / `SGLang 朱邦华` → 好结果；`朱邦华` / `月球大叔 播客` → 退化成单字释义。
    也就是说**拉丁术语能把检索锚住**，而中文长尾专名搜索引擎自己也认不出来。
    所以：拉丁词全要（它们通常是专有名词与技术术语），中文只取「去掉首尾虚词后
    长度 2-6」的片段，并且**选文里的片段优先于追问里的**（追问多是「这是什么意思」
    这类没有检索价值的句子）。剩下的交给 `websearch` 的相关性过滤兜底：
    宁可退化成「这次没联网」，也不要把「汉字朱的字典释义」当资料喂给模型。
    """
    latin: list[str] = []
    for word in _EN_RE.findall(selection or ""):
        w = word.lower()
        if len(w) >= 2 and w not in latin:
            latin.append(w)
    for word in _EN_RE.findall(question or ""):
        w = word.lower()
        if len(w) >= 2 and w not in latin:
            latin.append(w)
    # 长的优先（"deepseek" 比 "ai" 有价值），再按出现顺序稳定排序
    latin.sort(key=len, reverse=True)

    def cjk_of(text: str) -> list[str]:
        """中文片段：长片段先按虚词切开（「在多模态上的表现」→ 多模态 / 表现），
        再剥首尾虚词，只留长度 2-6 的（更长的是整句描述，检索价值低，
        而且实测搜索引擎对这种长中文短语会退化成单字匹配）。"""
        out: list[str] = []
        for run in _CJK_RUN_RE.findall(text or ""):
            pieces = [p for p in re.split(f"[{_SPLIT_CHARS}]+", run)]
            if len(pieces) == 1:
                pieces = [run]                    # 没有虚词可切，保留整段
            for piece in pieces:
                t = piece.strip(_EDGE_CHARS)
                if 2 <= len(t) <= QUERY_MAX_CJK and t not in out and not _is_stopword(t):
                    out.append(t)
        out.sort(key=len, reverse=True)           # 长片段更具体
        return out

    # 选文里的片段优先于追问里的：追问多是「这是什么意思」这类没有检索价值的句子
    cjk = cjk_of(selection) + cjk_of(question)
    if not cjk:
        # 兜底：整段中文没有虚词可切、又超过长度上限（例如「月球大叔聊低学历逆袭」11 个字）。
        # 中文的机构名/人名/术语通常是 2-4 字**并且出现在开头**，所以取最长片段的前 4 个字。
        # 实测「月球大叔聊低学历逆袭」这样能拿到「月球大叔」（单独搜是对的），
        # 而退回整句让搜索引擎处理是**必然退化**的（见 search_query 的实测表）。
        longest = max(_CJK_RUN_RE.findall(selection or "") + _CJK_RUN_RE.findall(question or ""),
                      key=len, default="")
        head = longest.strip(_EDGE_CHARS)[:4]
        if len(head) >= 2:
            cjk = [head]

    terms = latin[: max_terms - 1] if latin else []
    for t in cjk:
        if len(terms) >= max_terms:
            break
        if t not in terms and not any(t in other for other in terms):
            terms.append(t)
    if not terms:                                 # 只有拉丁词时也至少留一个中文片段
        terms = cjk[:max_terms]
    if not terms:
        # 极端情况：选文只有一两个字、追问又全是虚词（例如选「甲」问「为什么」）。
        # 这时关键词一个都抽不出来，但**不能因此放弃联网** —— 退回截断后的原句，
        # 让搜索引擎自己去处理（结果好坏由搜索层的相关性过滤兜底）。
        raw = " ".join(p for p in ((question or "").strip(), (selection or "").strip()[:40]) if p)
        return [raw[:QUERY_MAX_CHARS]] if raw.strip() else []
    return terms[:max_terms]


def search_query(selection: str, question: str = "") -> str:
    """给搜索引擎的查询串。**词序与词数是硬约束**，不是风格问题。

    真机实测（cn.bing.com，2026-09）出的一张表，这条规则完全照着它写：

        「月球大叔」        ✓ 正确（B 站/知乎的个人空间）
        「推理优化」        ✓ 正确（大模型推理优化长文）
        「多模态」          ✓ 正确
        「月球大叔 播客」    ✗ 退化成「月球（地球的唯一卫星）」
        「推理优化 区别」    ✗ 退化成「推理（思维的基本形式）」
        「多模态 表现」      ✗ 退化成「多（汉语文字）」
        「多模态 gpt-4o」    ✗ 中文在前照样退化
        「sglang 推理优化 区别 vllm」 ✓ 正确（拉丁词在第一位的锚住了整条查询）

    两条结论：
    1. **有拉丁术语就把它放最前面**，后面跟几个中文词都不会退化（检索会被锚住）；
    2. **纯中文时只能发一个词** —— 两个中文词必退化（搜索引擎拆碎后退化成首字匹配）。
       所以纯中文场景我们宁可只带最有信息量的那一个词，也不拼一串。

    抽词见 `search_terms()`；`query_terms()` 给出**实际发出去的词**，
    那一批同时交给搜索层做相关性过滤——过滤的语义是「引擎有没有为我真正问的东西
    返回结果」，所以两边必须是同一批（否则词表里那些被截断的候选词
    `多模态上`、`推荐系统里` 永远不可能作为完整子串出现在结果里，
    会把本来相关的结果一起误杀掉，白白退化成「没联网」）。
    """
    terms = query_terms(selection, question)
    if not terms:
        # 极端情况：选文只有一两个字、追问又全是虚词（例如选「甲」问「为什么」）。
        # 关键词一个都抽不出来，但**不能因此放弃联网** —— 退回截断后的原句让搜索引擎
        # 自己去处理；这种情况不做相关性过滤（我们无从判断什么算相关）。
        raw = " ".join(p for p in ((question or "").strip(), (selection or "").strip()[:40]) if p)
        return raw[:QUERY_MAX_CHARS].strip()
    return " ".join(terms)[:QUERY_MAX_CHARS].strip()


def query_terms(selection: str, question: str = "", *,
                max_terms: int = QUERY_MAX_TERMS) -> list[str]:
    """**实际放进查询串**的词（也是相关性过滤的判据，见 `search_query` 的说明）。"""
    terms = search_terms(selection, question, max_terms=max_terms)
    latin = [t for t in terms if t.isascii()]
    cjk = [t for t in terms if not t.isascii()]
    if latin:
        return latin[:2] + cjk[:2]
    if cjk:
        return cjk[:1]                 # 纯中文只发一个词
    return []                          # 退化成原句的情况：不做过滤


# ---------------------------------------------------------------- 片段检索

def _ascii_term(term: str) -> bool:
    """纯 ASCII 的词按「整词」匹配；中文靠子串匹配（没有词边界可用）。

    理由与 search._needs_word_boundary 一样：搜 `ai` 时按子串匹配会把 `said`、`chair`
    也算命中；中文加了边界反而失效（「鱼」要能命中「鱼不存在」）。
    """
    return all(ch.isascii() and (ch.isalnum() or ch in "-_'") for ch in term)


def _matcher(term: str) -> re.Pattern:
    body = re.escape(term)
    pattern = rf"(?<!\w){body}(?!\w)" if _ascii_term(term) else body
    return re.compile(pattern, re.IGNORECASE)


def _idf(total: int, df: int) -> float:
    """BM25 口径的 idf：`ln(1 + (N - df + 0.5) / (df + 0.5))`。

    df 越小（越罕见）权重越高。极端情况也是正的：df=N 时得 `ln(1 + 0.5/(N+0.5)) > 0`，
    所以「到处都是的词」不是没有贡献，只是几乎不影响排序——这正是想要的效果。
    """
    return math.log(1.0 + (total - df + 0.5) / (df + 0.5))


def _segment_score(found: dict[str, int], idf: dict[str, float], index: int, total: int) -> float:
    """单片段得分 = Σ idf(词) × 词频次线性加成 × 位置偏置。

    次线性（log2）而不是线性：两个小时的口语文字稿里同一个词随便重复二十次，
    线性计数会让它靠「啰嗦」赢过真正切题但只提了一次的片段。
    """
    raw = sum(idf[t] * (1.0 + TF_LOG * math.log2(count)) for t, count in found.items())
    position = 1.0 + POS_BONUS * (1.0 - index / max(1, total - 1))
    return raw * position


def _merge_spans(spans: list[list[int]], gap: int = 0) -> list[list[int]]:
    """把闭区间 [起, 止] 里相邻/重叠的合成一段；gap>0 时容许中间空 gap 个下标。"""
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1 + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def retrieve(workdir: Path, query: str, *, limit: int = 6, window: int = 1) -> list[dict]:
    """在**这一集**的文字稿里找出与 query 最相关的片段。

    返回 `[{"start": 秒, "ts": "00:10:07", "text": "合并后的原文", "score": float}]`，
    按 score 降序、最多 limit 条。`ts` 用 `util.ts_clock()` 生成（前端靠它做「点回听」）。

    打分与合并策略
    --------------
    - 词表来自 `keywords(query)`；命中判定是子串匹配（ASCII 词加整词边界）
    - `df` = 该词出现在多少个片段里；`idf = ln(1 + (N - df + 0.5)/(df + 0.5))`，
      罕见词权重高 → 命中一个罕见词的片段能压过命中几个常用词的片段
    - 片段分 = Σ idf(词) × (1 + 0.25·log2(词频))，再乘位置偏置
      `1 + 0.25·(1 - 下标/末尾下标)`：播音/访谈常把主旨放前段，同分时靠前的先出
    - window：命中片段前后各并入 window 个片段（这样引文不至于是半句话）。先按
      「下标相邻或重叠」合并命中，再各自展开窗口；**展开后仍然重叠的再合并一次**，
      所以不会返回两条互相覆盖的结果。合并后的分数是所含命中片段分数之和
      （连续讨论越密集越相关），时间戳取合并后**第一行**的时间
    - `text` 是多行原文，每行行首带 `[时:分:秒]`：与文字稿/文章流水线同一格式，
      这样模型引用任意一行时都能抄对它自己的时间戳（只给片段级时间戳会让引文标错分钟）；
      单条最多 `MAX_PASSAGE_SEGMENTS` 行（命中区域连着几十句时只留开头这些行）
    - 空 query / 没有文字稿 / 少于 2 个片段 / 一个词都没命中 → `[]`
    """
    segments = load_segments(workdir) if workdir is not None else []
    if len(segments) < MIN_SEGMENTS:
        return []
    terms = keywords(query, limit=KEYWORD_LIMIT)
    if not terms:
        return []

    matchers = [(term, _matcher(term)) for term in terms]
    total = len(segments)
    hits: dict[int, dict[str, int]] = {}
    df: dict[str, int] = {}
    for index, seg in enumerate(segments):
        found: dict[str, int] = {}
        for term, pattern in matchers:
            count = len(pattern.findall(seg["text"]))
            if count:
                found[term] = count
        if found:
            hits[index] = found
            for term in found:
                df[term] = df.get(term, 0) + 1
    if not hits:
        return []

    idf = {term: _idf(total, df.get(term, 0)) for term in terms}
    scored = {index: _segment_score(found, idf, index, total) for index, found in hits.items()}

    window = max(0, int(window))
    spans = _merge_spans([[i, i] for i in sorted(hits)])
    spans = _merge_spans(
        [[max(0, a - window), min(total - 1, b + window)] for a, b in spans]
    )

    items: list[dict] = []
    for a, b in spans:
        b = min(b, a + MAX_PASSAGE_SEGMENTS - 1)     # 命中区域太密集时只保留开头这些行
        lines = [
            f"[{ts_clock(seg['start'])}] {seg['text']}" for seg in segments[a : b + 1]
        ]
        items.append({
            "start": segments[a]["start"],
            "ts": ts_clock(segments[a]["start"]),
            "text": "\n".join(lines),
            "score": round(sum(v for i, v in scored.items() if a <= i <= b), 4),
        })
    items.sort(key=lambda item: (-item["score"], item["start"]))
    return items[: max(0, int(limit))]


# ---------------------------------------------------------------- 提示词

_SYSTEM = """你是这位读者的**阅读助手**。他在一篇由播客（或视频）整理成的文章里选中了一段话，有疑问。你要把这一点讲透：这句话在这期节目里到底是什么意思、他可能卡在哪里、接着该往哪看。**不是复述文章，也不是复述这期节目。**

## 手上有什么材料（依据优先级从高到低）
1. **这一期的原文片段**（下面给了若干条，逐字来自转写文字稿；它是自动语音识别的结果，可能有错别字、漏字和口语碎片）：这是最可信的依据
2. **网络搜索结果**（如果下面给了）：**仅供参考**，可信度明显低于原文，而且可能已经过时

## 直接给答案，不要交代「材料里有什么」（用户明确要求过的）
- **读者要的是答案，不是你对材料的清点。** 直接回答，不要复述片段讲了什么、没讲什么
- 绝对不要写这类句子（一条都不许出现）：
  - 「这一期的原文里没有覆盖这一点」「原文里没有覆盖」
  - 「原文没有提到，以下是背景补充」「以下是背景补充」
  - 「凭我已有的知识」「建议你自己再核一次」
  - 「片段只讲到 A、B、C，没有一句说 D」——**清点材料本身就是在浪费他的时间**
- 需要说明「这句不是本集原文」时，只用一个**极短的括号标记**，贴在**那句话的末尾**：
  `（原文未提及）` —— 四个字，不解释、不展开、不写第二句
- 引用网络资料时同样只标 `（据网络资料）`，不要说来源性质、不要提示可能过时
- 有本集原文支撑的内容，正常引用原话并带时间戳，**不需要任何标记**
- **回答里不要出现这些词**：这期节目、这一期、本期、节目里、原文、片段、素材、文字稿、转写。
  一个直接回答问题的回答**不需要提到材料**：引用原文靠引号 + 时间戳就够了，
  说「原文里他说…」「这期节目没讲…」本身就是废话（实测：禁掉一种说法后模型会换一种继续清点，
  所以这里直接禁用这批词）

## 绝不编造（这部分比上面更硬，违反任意一条即为不合格）
- 不许虚构原话、时间戳、数字、人名、作品名。引用原话必须**逐字**来自上面给的片段，并在同一行标出它行首那个 `[时:分:秒]`
- 时间戳只能用片段里给出的那些，不许自己估算或推算；片段里没有的细节，一个数字都不要编
- 不确定就写不确定（一句话，例如「这一点我不确定」），但**不要用解释代替回答**
- 转写常把人名、术语听错：片段之间说法互相矛盾时，用一两句指出矛盾，不要替它圆场

## 引用网络信息时
- 写明「据网络资料」，并说清来源性质（官方文档 / 主流媒体 / 个人博客或自媒体）
- 不要把某个小站、某条自媒体的说法当成定论；有分歧就写「说法不一」
- 涉及价格、政策、软件版本、任职这类会变的信息，要说明可能是较旧的信息，建议他自己再核对一次

## 只回答被问的那一点（最容易违反的一条）
- 你的任务是讲清楚**他选中的这一段、他问的这一个问题**。**不是**把这期节目里所有
  「也很有意思」「也值得说」的点讲一遍。他没有问的，就**不要主动展开**
- 具体禁止这些句式（用户没问就删掉）：
  「另外……也值得说」「顺带提一下」「还有一个容易误读的点是……」
  「……这个说法也值得停一下」「至于……」「值得一提的是」
- 如果他没写疑问（只选了文字），**只解释这段话本身最可能卡住的那一处**，
  不要做通篇赏析、不要逐句点评
- 如果这段里显然有多个可疑点，只讲**最要紧的那一个**；其余的宁可不说
- 宁可少讲一个点，也不要答非所问。用户要的是重点，不是全面

## 输出格式（严格遵守）
- **第一句直接给答案**。不要铺垫、不要写「这段话最容易被卡住的地方是……」这种绕圈开场，
  不要写「根据您提供的…」「你提到的那段话…」，不要重复他的疑问，不要写标题
- {para_lo}-{para_hi} 个自然段，每个自然段不超过 120 字；全文 {lo}-{hi} 字，按约 {ask} 字写
  （这是侧边抽屉，不是文章；超过 {hi} 字就是在浪费他的时间）
- 说人话：句子长短交替，可以口语；不堆术语，不写没有信息量的过渡句
- 引原话单独成行，写成 `> 原话 [00:10:07]`；全文引用**不超过 {quotes} 条**，每条只出现一次
- 结尾用一行 `**还可以往哪追**`，最多 {followups} 条**具体**方向（能直接拿去搜、去听、
  去问的那种）。**如果这条问题本身已经答完、没有真正值得追的方向，就整块省略**，
  不要为了凑格式写「可以继续深入了解」这类废话
- 禁用这些套话：值得注意的是、总的来说、综上所述、不难看出、毫无疑问、毋庸置疑、发人深省、引人深思、深刻的、让我们、众所周知、可以说、从某种意义上
- 简体中文；人名、作品名、机构名、术语保留原文写法，不要硬翻成中文
- 如果下面给了【读者画像】，解读要照顾他关注的方向，但绝不能因此偏离原文"""

_PASSAGE_HEADER = (
    "【这一期的原文片段（逐字来自转写文字稿；每行行首 [时:分:秒] 是真实时间戳，"
    "引用时必须原样带上）】"
)
_PASSAGE_EMPTY = (
    "（本次没有检索到与选中文字相关的原文片段：可能这一集还没有文字稿、转写没跑完，"
    "或者这段话在文字稿里找不到对应内容。）"
    "因此这一条回答里**不要引用任何原话、也不要给任何时间戳**；"
    "**直接用你自己的知识回答他的疑问**，不要交代「原文里没有」、不要写「以下是背景补充」，"
    "只在相关的句子末尾标一次 `（原文未提及）`。"
)

_WEB_HEADER = "【网络搜索结果（仅供参考）】"
_WEB_EMPTY = (
    "（本次没有联网检索结果：可能联网检索被关闭、检索失败，或者没有找到有用的资料。）"
    "因此不要写「据网络资料」；需要补充背景时**直接用你已有的知识回答**，"
    "不要交代「没联网」，也不要解释来源性质或提示可能过时。"
)



# 回答里**不该出现**的「交代出处」句式。用户明确反馈过：
# 「直接给出回答就好，不要解释原文有没有，很浪费观看时间」。
# 曾经提示词是反着写的（要求「第一句就写这一期的原文里没有覆盖这一点」），
# 于是模型真的这么写，占掉了整个第一段。现在改成正向要求 + 这道检查兜底。
_META_PHRASES = (
    "这一期的原文里没有覆盖", "原文里没有覆盖", "原文没有覆盖", "原文里没有提到",
    "原文没有提到", "以下是背景补充", "以下为背景补充", "以下是背景信息",
    "凭我已有的知识", "凭我的已有知识", "建议你自己再核", "建议你再核一次",
    "片段只讲到", "片段只提到", "片段里只讲", "素材里没有提到", "材料里没有提到",
    "文字稿里没有提到", "转写里没有提到",
)


# 回答里不该出现的**材料名词**。
# 为什么用「句子级 + 材料名词」而不是只列短语：实测列短语挡不住 ——
# 禁掉「片段只讲到」之后，模型换成「这期节目里没讲…只从…切入，讲的是 A、B、C」照样清点。
# 而一个直接给答案的回答**根本不需要提到材料**：引用原文靠引号 + 时间戳就够，
# 说「原文里他说」本身就是废话。
_MATERIAL_WORDS = (
    "这期节目", "这一期", "本期节目", "本期", "节目里", "播客里",
    "原文", "片段", "素材", "文字稿", "转写",
)

_SENTENCE_RE = re.compile(r"[^。！？!?\n]*[。！？!?]")
# 允许的极短标记：检查前先剥掉，否则「（原文未提及）」里的「原文」会被误判成清点材料
_ALLOWED_TAGS_RE = re.compile(r"[（(](?:原文未提及|据网络资料|原文未提|网络资料)[）)]")


def answer_problems(text: str) -> list[str]:
    """检查回答里有没有「清点材料」的废话，返回问题清单（空 = 通过）。

    两层判定：
    1. **句子级**：任何提到「这期节目 / 原文 / 片段 / 素材 / 文字稿 / 转写」的句子都算 —— 
       一个直接回答问题的回答不需要提到材料。
    2. 兜底短语表：收一些不说材料名词、但同样是交代出处的说法。

    这一条只做**记录与告警**，不触发重写：回答是流式的，文字已经推给用户了，
    中途换掉会让界面上的内容突然被替换，比留一句废话更糟。
    所以这里先量、先暴露，用数据判断提示词够不够硬（实测见 README）。
    """
    body = _ALLOWED_TAGS_RE.sub("", text or "")
    problems: list[str] = []
    for sentence in _SENTENCE_RE.findall(body):
        hit = next((w for w in _MATERIAL_WORDS if w in sentence), "")
        if hit:
            problems.append(f"这句话在讲材料而不是回答：「{hit}」→ {sentence.strip()[:38]}")
    for w in _META_PHRASES:
        if w in body and not any(w in p for p in problems):
            problems.append(f"出现交代出处的句子「{w}」")
    return problems


def build_prompt(*, selection: str, question: str, title: str, podcast: str,
                 passages: list[dict], web: str, profile: str = "",
                 mode: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, str]:
    """拼出 (system, user)。

    system：角色 + 依据优先级 + 「绝不编造」+ 「只回答被问的那一点」+ 输出格式（见 `_SYSTEM`）
    user：元信息、读者画像、选中的文字、他的疑问、原文片段（带时间戳）、网络资料
    mode：篇幅档位（concise 默认 / detail），决定字数区间、段数与引用条数

    没有原文片段、没有网络资料时，user 里都有一句**明确的说明**（不是留空段落）：
    模型缺什么都看得见，才不会拿记忆里的东西冒充这一期的内容。
    """
    plan = answer_mode(mode)
    ask = outline.stated(plan["hi"])
    system = (
        _SYSTEM.replace("{ask}", str(ask))
        .replace("{lo}", str(plan["lo"]))
        .replace("{hi}", str(plan["hi"]))
        .replace("{para_lo}", str(plan["para_lo"]))
        .replace("{para_hi}", str(plan["para_hi"]))
        .replace("{quotes}", str(plan["quotes"]))
        .replace("{followups}", str(plan["followups"]))
    )

    blocks = [f"文章标题：{title or '（未知）'}\n播客/频道：{podcast or '（未知）'}"]
    if profile:
        blocks.append(profile)
    blocks.append(
        "【读者选中的文字】\n" + (selection.strip() or "（没有给出选中文字）")
    )
    blocks.append(
        "【他的疑问】\n"
        + (question.strip() or "（他没有写出具体疑问，请就这段话里最可能引起疑问的地方展开）")
    )

    if passages:
        lines = [_PASSAGE_HEADER]
        for i, passage in enumerate(passages, 1):
            text = str(passage.get("text") or "").strip()
            ts = str(passage.get("ts") or "").strip()
            head = f"片段 {i}" + (f"（起始 {ts}）" if ts else "")
            lines.append(f"{head}：\n{text}")
        blocks.append("\n".join(lines))
    else:
        blocks.append(f"{_PASSAGE_HEADER}\n{_PASSAGE_EMPTY}")

    web_text = (web or "").strip()
    blocks.append(f"{_WEB_HEADER}\n{web_text or _WEB_EMPTY}")
    # 连续追问：把之前的轮次也放进 user 块。虽然 _chat 的 history 已经带了真实角色，
    # 这里再落一份纯文本副本是有意的 —— 模型对着「上一轮我答了什么」不容易跑题，
    # 而且这一段是**可核对**的（出问题时能直接从日志看出它看到了什么）。
    turns = [t for t in (history or []) if isinstance(t, dict) and str(t.get("content") or "").strip()]
    if turns:
        lines = ["【之前的对话（他一直在追问同一段文字，回答要接着上次说，不要重复已经讲过的内容）】"]
        for turn in turns:
            who = "他" if turn.get("role") == "user" else "你（上次的回答）"
            lines.append(f"{who}：{str(turn.get('content')).strip()}")
        blocks.append("\n".join(lines))
        blocks.append("请接着上面的对话回答【他的疑问】，按系统要求给出解读。")
    else:
        blocks.append("请按系统要求给出解读。")
    return system, "\n\n".join(blocks)


# ---------------------------------------------------------------- 模型调用

def _client():
    """取 DeepSeek 客户端（与 summarize 同源）。

    单测打桩这个函数就能完全离线跑：假客户端只要能满足
    `chat.completions.create(**kwargs) -> iterable[chunk]`，`summarize._chat` 的
    流式拼装、usage 记账、空正文报错全都会真跑一遍。
    """
    return summarize._client()


def _tee_client(client, on_delta, log=print):
    """包一层客户端：chunk 路过时把 `delta.content` 交给 on_delta，chunk 原样放行。

    `summarize._chat` 只给 `on_chars(累计字数)`，拿不到逐块文本，而 SSE 需要增量文本。
    改它的签名会影响整条文章流水线，所以在调用方这边包：_chat 拿到的 chunk 一模一样，
    它自己的思考模式处理、usage 记账、截断告警全部照旧生效。
    """
    state = {"warned": False}

    def create(**kwargs):
        stream = client.chat.completions.create(**kwargs)

        def gen():
            for chunk in stream:
                try:
                    choices = getattr(chunk, "choices", None) or []
                    delta = getattr(choices[0], "delta", None) if choices else None
                    content = getattr(delta, "content", None) if delta is not None else None
                    if content:
                        on_delta(content)
                except Exception as exc:   # 回调出问题不该打断生成（yield 在 try 之外）
                    if not state["warned"]:
                        state["warned"] = True
                        log(f"[deepdive] 增量回调异常（已忽略同类错误）：{type(exc).__name__}: {exc}")
                yield chunk

        return gen()

    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )


def _call_model(*, system: str, user: str, model: str, log=print, on_delta=None,
                max_tokens: int | None = None, history: list[dict] | None = None) -> str:
    """流式调用，返回正文全文（复用 summarize._chat 的全部既有约定）。"""
    client = _client()
    return _chat(
        _tee_client(client, on_delta, log) if on_delta else client,
        model, system, user,
        log=log, max_tokens=max_tokens or answer_limits()[1], temperature=ANSWER_TEMPERATURE,
        thinking=False,   # 关思考：抽屉场景要的是快而稳，且关掉后 temperature 才生效
        history=history,  # 连续追问：把之前的轮次按真实角色带上（见 _chat 的说明）
    )


def _friendly_error(exc: Exception) -> str:
    """把异常翻成一句人能看懂的话（上层是流式界面，只显示这段文字）。"""
    if isinstance(exc, config.MissingKeyError):
        return "还没有配置 DeepSeek API Key，无法生成解读：请在「设置」里填入 key 后重试。"
    return f"解读生成失败（{type(exc).__name__}）：{exc}"


# ---------------------------------------------------------------- 联网检索

_WEB_UNAVAILABLE = "联网检索模块还不可用（未安装，或还没有实现 search()）"


def _load_websearch(log=print):
    """惰性 import `podcast_article.websearch`；拿不到就返回 None（不抛）。

    这里 except 得宽：那个模块正在并发开发，可能还不存在、可能被写到一半
    （语法错、半成品属性）。任何情况都只降级成「未联网」，绝不让抽屉崩掉。
    """
    try:
        return importlib.import_module(f"{__package__}.websearch")
    except Exception as exc:
        log(f"[deepdive] 联网检索模块不可用，本次按未联网处理（{type(exc).__name__}: {exc}）")
        return None


def _fallback_web_text(web: dict, limit: int) -> str:
    """内置的容错格式化：认几种常见字段名，认不出来就返回空串（当未联网处理）。

    为什么不借着这个函数去猜 websearch 的字段：注入式 search（单测、或上层换检索源）
    返回的结构没有约定，与其硬猜，不如按几个常见名字尽力而为，剩下交给 prompt。
    """
    lines: list[str] = []
    for key in ("answer", "summary", "abstract", "text"):
        value = web.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(value.strip())
            break
    items = None
    for key in ("results", "items", "snippets", "sources"):
        value = web.get(key)
        if isinstance(value, list):
            items = value
            break
    count = 0
    for item in items or []:
        if count >= limit:
            break
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or item.get("link") or "").strip()
            snippet = str(
                item.get("snippet") or item.get("content") or item.get("text") or ""
            ).strip()
        elif isinstance(item, str):
            title, url, snippet = item.strip(), "", ""
        else:
            continue
        if not (title or snippet):
            continue
        count += 1
        line = f"- {title or snippet[:40]}" + (f"（{url}）" if url else "")
        if snippet and snippet != title:
            line += f"：{snippet[:400]}"
        lines.append(line)
    return "\n".join(lines).strip()


def _format_web(module, web: dict, log=print) -> str:
    """检索结果 → 给模型看的文本。

    有 websearch.format_for_prompt 就优先用它（格式化是检索模块自己的口径，字段由它负责）；
    没有、或它抛异常，就用内置的 `_fallback_web_text` 兜底。
    """
    formatter = getattr(module, "format_for_prompt", None) if module is not None else None
    if callable(formatter):
        try:
            text = formatter(web, limit=WEB_SNIPPETS)
            if text and str(text).strip():
                return str(text).strip()
        except Exception as exc:
            log(f"[deepdive] websearch.format_for_prompt 失败，改用内置格式化"
                f"（{type(exc).__name__}: {exc}）")
    return _fallback_web_text(web, WEB_SNIPPETS)


def _web_query(question: str, selection: str) -> str:
    """联网检索用的 query：短关键词串（`search_query`）。

    这里曾经直接用「疑问 + 截断的选中文字」，**真机实测证明那是错的**：
    把选文原句（「朱邦华复盘判断力、时机选择与 AI infra 的未来格局」）丢给 Bing，
    返回的是**汉字「朱」的字典释义** —— 长中文短语会被搜索引擎拆碎后退化成单字匹配。
    而 `SGLang`、`SGLang 朱邦华` 这类带拉丁术语的短查询结果都很好。
    所以联网与文字稿检索必须用两套 query：后者要原句（子串命中越长越准），
    前者要短关键词（见 `search_terms` 的注释）。
    """
    return search_query(selection, question)


def _web_lookup(question: str, selection: str, *, log=print, search=None):
    """联网检索，返回 `(给前端的 web 字段, 给提示词的文本)`。

    - `search` 由调用方注入时只用它，不去 import websearch（单测、或上层想换检索源）
    - 否则走默认路径：惰性 import websearch，用它的 `search()`
    - 失败（import 不了、没有 search、抛异常、返回 ok=False、返回非 dict、格式化不出来）
      一律降级成「未联网」：web 字段里带失败原因给界面看，提示词按「没有网络资料」写
    - **把 terms 一起传下去**：搜索层要用同一批词做相关性过滤，
      拿不到相关结果时宁可报「没联网」，也不要把无关网页当资料交给模型
    """
    query = _web_query(question, selection)
    # 过滤判据 = 实际发出去的词（见 query_terms 的注释：用全量词表会误杀相关结果）
    terms = query_terms(selection, question) if query else []
    if not query:
        return ({"ok": False, "query": "", "error": "选中文字与疑问都是空的，没有可用于联网检索的内容"}, "")

    module = None
    func = search
    if func is None:
        module = _load_websearch(log)
        func = getattr(module, "search", None) if module is not None else None
        if not callable(func):
            if module is not None:
                log("[deepdive] websearch 里还没有可用的 search()，本次按未联网处理")
            return {"ok": False, "query": query, "error": _WEB_UNAVAILABLE}, ""

    try:
        try:
            raw = func(query, terms=terms)
        except TypeError:
            # 兼容只接受一个参数的检索函数（例如上层注入的简化实现）
            raw = func(query)
    except Exception as exc:
        reason = f"联网检索异常：{type(exc).__name__}: {exc}"
        log(f"[deepdive] {reason}")
        return {"ok": False, "query": query, "error": reason}, ""

    if not isinstance(raw, dict):
        reason = "联网检索返回了非预期的结果（不是 dict）"
        log(f"[deepdive] {reason}")
        return {"ok": False, "query": query, "error": reason}, ""

    web = {**raw}
    web.setdefault("query", query)
    web["ok"] = bool(raw.get("ok", True))
    if not web["ok"]:
        reason = str(raw.get("error") or raw.get("message") or "未给出原因")
        log(f"[deepdive] 联网检索失败：{reason}")
        web["error"] = reason
        return web, ""

    text = _format_web(module, web, log)
    if not text:
        log("[deepdive] 联网检索没有返回可用内容，按未联网处理")
        web["ok"] = False
        web["error"] = "联网检索没有返回可用内容"
        return web, ""
    return web, text


# ---------------------------------------------------------------- 用量记账

def _profile() -> str:
    """读者画像（设置页里填的）；读不出来就当没填——设置坏了不该让解读失败。"""
    try:
        return settings.profile_text()
    except (OSError, ValueError):
        return ""


def _start_usage(workdir: Path | None, model: str, log=print) -> bool:
    """需要时开一个用量记录器；返回「是不是我们开的」（决定要不要 stop）。

    两条纪律：
    - 已经有活动记录器（批量任务正在跑）就**不碰它**：usage 的活动记录器是模块级全局的，
      抢过来再 stop() 会把别人正在记的账一起收掉，也会把它的落盘目录搞错
    - 自己开的时候先把这一集已有的 usage.json 作为起点读进来：这次的 token 是**累加**到
      那一集的账上，而不是把生成文章时记的用量覆盖掉
    """
    if workdir is None:
        return False
    try:
        if usage.current() is not None:
            log("[deepdive] 已有用量记录器在跑：本次调用累加进它，不另开（避免打乱全局记账）")
            return False
        usage.start(model, workdir=Path(workdir), started=usage.load(workdir) or {})
        return True
    except Exception as exc:
        log(f"[deepdive] 用量记录器启动失败，本次不记账（{type(exc).__name__}: {exc}）")
        return False


def _stop_usage(started: bool, log=print) -> None:
    """把记录器收掉（flush 会写进 workdir/usage.json）；失败只记日志，不抛。"""
    if not started:
        return
    try:
        snapshot = usage.stop()
        log(f"[deepdive] 用量已记账（累计 {snapshot.get('out_tokens', 0)} 输出 tokens、"
            f"{snapshot.get('calls', 0)} 次调用）")
    except Exception as exc:
        log(f"[deepdive] 用量落盘失败（{type(exc).__name__}: {exc}）")


# ---------------------------------------------------------------- 主入口

def stream_answer(*, workdir: Path | None, selection: str, question: str, title: str,
                  podcast: str, model: str | None = None, use_web: bool = True,
                  mode: str | None = None, log=print, on_delta=None, search=None,
                  history: list[dict] | None = None) -> dict:
    """主入口（流式）。返回 `{"answer", "passages", "web", "error"}`。

    - 先 `retrieve()` 拿原文片段（query = 选中文字 + 疑问，两者都可能在讲他关心的词）；
      再按需联网（`search` 是可注入的 `callable(query) -> dict`）
    - 正文逐块通过 `on_delta(text)` 回调给上层做 SSE；`answer` 就是这些块的顺序拼接
      （不做 strip，保证与流式增量严格一致，上层不必再拼一遍）
    - `workdir` 不为 None 时把这次的 token 用量记进去（累加，不覆盖已有记录）
    - **绝不抛异常**：任何失败都变成 `error` 里的一句人话，能出的部分照出

    返回的 `passages` 原样是 `retrieve()` 的结果；`web` 在 `use_web=False` 时是 None，
    否则是检索模块的字段加上 `ok` / `query` / 失败时的 `error`。
    """
    model = model or config.deepseek_model()
    selection = (selection or "").strip()
    question = (question or "").strip()

    retrieval_query = " ".join(p for p in (selection, question) if p)
    passages = retrieve(workdir, retrieval_query) if workdir is not None else []
    log(f"[deepdive] 原文检索：命中 {len(passages)} 段（选中 {len(selection)} 字）")

    if use_web:
        web, web_text = _web_lookup(question, selection, log=log, search=search)
    else:
        log("[deepdive] 按要求跳过联网检索")
        web, web_text = None, ""

    system, user = build_prompt(
        selection=selection, question=question, title=title or "", podcast=podcast or "",
        passages=passages, web=web_text, profile=_profile(), mode=mode, history=history,
    )

    started = _start_usage(workdir, model, log)
    pushed: list[str] = []          # 真正推给界面的文本（= 最终 answer）
    body = ""                       # 生成结果；generate() 抛异常时也要有值
    error = ""

    def push(text: str) -> None:
        if not text:
            return
        pushed.append(text)
        if on_delta:
            try:
                on_delta(text)
            except Exception as exc:   # SSE 连接断了不该让生成失败（正文仍会留着）
                log(f"[deepdive] on_delta 回调异常（已忽略）：{type(exc).__name__}: {exc}")

    tracker: dict = {"buf": []}

    def generate(*, gate: bool, system_override: str | None = None) -> tuple[str, bool]:
        """跑一次生成，返回 (正文, 是否因闸门拦下而没放行)。

        gate=True 时先攒住开头不推给界面，攒到 GATE_CHARS 做一次检查：
        - 通过 → 把攒下的放行，之后边收边推（流式体验保留）
        - 不通过 → 一个字都不推，交给调用方重写（用户看不到中间过程，也就不会看到内容被替换）

        为什么要机械闸门：提示词里点名禁掉「片段只讲到」之后，模型会**换一种说法**
        继续清点材料（「这期节目里没讲…只从…切入，讲的是 A、B、C」）—— 实测 3 次里还有 2 次。
        闸门判定见 answer_problems()，用的是「句子级 + 材料名词」，挡得住换说法。
        """
        buf: list[str] = []
        tracker["buf"] = buf          # 暴露给调用方：中途出错时用它保住已生成的部分
        live = [False]
        verdict = {"blocked": False}

        def emit(chunk: str) -> None:
            buf.append(chunk)
            if live[0]:
                push(chunk)
                return
            text = "".join(buf)
            if gate and len(text) < GATE_CHARS:
                return                       # 还没攒够，继续攒
            live[0] = True
            if gate:
                problems = answer_problems(text)
                if problems:
                    # 一个字都不推，也不会再推后续内容；本次生成作废，调用方会重写
                    live[0] = False
                    verdict["blocked"] = True
                    log(f"[deepdive] 回答开头在清点材料：{problems[0][:60]}")
                    return
            push(text)

        body = _call_model(system=system_override or system, user=user, model=model,
                           log=log, on_delta=emit,
                           max_tokens=answer_limits(mode)[1], history=history)
        if not buf and body:
            # 兜底：万一流里没有 content（例如上游换了实现），整段补发一次，
            # 保证「on_delta 收到的拼接」永远等于 answer
            emit(body)
        if gate and not live[0] and buf:
            # 正文比 GATE_CHARS 还短：流结束时缓冲里还攒着，这里补做一次检查再放行。
            # （漏了这一步的话，短回答会一个字都推不出去 —— 踩过。）
            text = "".join(buf)
            problems = answer_problems(text)
            if problems:
                verdict["blocked"] = True
                log(f"[deepdive] 回答在清点材料：{problems[0][:60]}")
            else:
                push(text)
        return "".join(buf), verdict["blocked"]

    try:
        body, blocked = generate(gate=True)
        if blocked:
            log("[deepdive] 带着问题重写一次（只重写一次，不再多花额度）…")
            retry_system = system + (
                "\n\n【重写要求】上一次的回答在**清点材料**（说明哪部分讲了、哪部分没讲），"
                "那是用户明确说过的废话，必须删掉。这次只回答他的问题："
                "不要提到这期节目/原文/片段/素材/文字稿；需要标注出处时只在句末写一个"
                "`（原文未提及）` 或 `（据网络资料）`。"
            )
            # 被拦时第一次一个字都没推出去，所以界面是干净的，直接拿重写版即可
            assert not pushed, "被闸门拦下时不该已经推过内容"
            body, _ = generate(gate=False, system_override=retry_system)
        if answer_problems(body):
            log("[deepdive] 重写后仍在清点材料，保留这一版（不再重试）")
    except Exception as exc:
        error = _friendly_error(exc)
        log(f"[deepdive] {error}")
        # 中途失败时，攒在闸门缓冲里、还没放行的正文不能丢 —— 有半篇也比一片空白强
        partial = "".join(tracker.get("buf") or [])
        if partial and not pushed:
            # 走到这里说明不会再有第二次生成了，那就把已生成的部分交出去：
            # 哪怕开头有一句清点材料的废话，也比让用户面对一片空白强
            push(partial)
            log(f"[deepdive] 已把中途生成的 {len(partial)} 字先交给界面")
    finally:
        _stop_usage(started, log)

    answer = "".join(pushed) or body
    if not answer and not error:
        error = "模型没有返回内容，请重试。"
    # 记录（不重写）：回答里还在「清点材料」的话，日志里应当看得见 —— 用这个数据判断
    # 提示词够不够硬。不触发重写的原因见 answer_problems 的注释（流式内容会突然被替换）。
    if answer:
        meta = answer_problems(answer)
        if meta:
            log(f"[deepdive] ⚠ 回答里有 {len(meta)} 处交代出处的话：" + "；".join(m[:30] for m in meta))
    return {"answer": answer, "passages": passages, "web": web, "error": error}
