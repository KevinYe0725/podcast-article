"""全文检索：查询解析、AND/OR 语义、相关度排序、时间戳与上下文。

全部跑在 tmp_output 上，绝不碰仓库里的真实 output/。
"""
from __future__ import annotations

import json

from podcast_article.search import parse_query, search, stats


def _mk(root, name, *, title=None, podcast="测试台", article=None, transcript=None,
        meta: bool | str = True):
    """造一集：meta.json / article.md / transcript.txt 都是可选的。

    meta=False 不写 meta.json；meta 传字符串就把它当 meta.json 的原始内容（用来造坏文件）。
    """
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if meta is True:
        (d / "meta.json").write_text(
            json.dumps({"title": title if title is not None else name, "podcast": podcast},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    elif isinstance(meta, str):
        (d / "meta.json").write_text(meta, encoding="utf-8")
    if article is not None:
        (d / "article.md").write_text(article, encoding="utf-8")
    if transcript is not None:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


# ------------------------------------------------------------------ 查询解析

def test_parse_query_and_or_phrase():
    assert parse_query("") == []
    assert parse_query("   ") == []
    # 空格 = AND，| = OR
    assert parse_query("强化学习|RL 训练") == [["强化学习", "RL"], ["训练"]]
    # 引号里的空格不拆
    assert parse_query('"exact phrase" 其他') == [["exact phrase"], ["其他"]]
    # 引号里按字面处理，| 不再当 OR
    assert parse_query('"a|b" c') == [["a|b"], ["c"]]
    # 丢掉空候选
    assert parse_query("a||b |") == [["a", "b"]]
    assert parse_query('"   "') == []
    # 原样返回大小写，大小写不敏感只体现在匹配阶段
    assert parse_query("SQLite") == [["SQLite"]]


# ------------------------------------------------------------------ 匹配语义

def test_english_case_insensitive(tmp_output):
    _mk(tmp_output, "ep-sqlite", title="SQLite 入门",
        article="# SQLite 入门\n\nSQLite 是一个嵌入式数据库。\n")
    for q in ("sqlite", "SQLITE", "SqLiTe"):
        got = search(tmp_output, q)
        assert [r["dir"] for r in got] == ["ep-sqlite"], q
        assert got[0]["hits"][0]["field"] == "title"
        assert got[0]["hits"][0]["match"] == "SQLite"       # 原文大小写
    assert search(tmp_output, "postgres") == []


def test_chinese_search(tmp_output):
    _mk(tmp_output, "ep-quake", title="鱼不存在",
        article="# 鱼不存在\n\n1906 年旧金山地震，标本馆的鱼重回混沌。\n")
    _mk(tmp_output, "ep-other", title="别的", article="# 别的\n\n今天天气不错。\n")
    got = search(tmp_output, "地震")
    assert [r["dir"] for r in got] == ["ep-quake"]
    assert got[0]["title"] == "鱼不存在"
    assert got[0]["match_count"] == 1
    assert got[0]["hits"][0]["match"] == "地震"


def test_and_semantics_needs_every_term(tmp_output):
    _mk(tmp_output, "both", title="both", article="# 标题\n\n强化学习很好。\n",
        transcript="[00:00:01] 这里讲 RL\n")
    _mk(tmp_output, "only-one", title="only", article="# 标题\n\n强化学习很好。\n")
    # 两个词都要出现；一个在文章里、一个在文字稿里也算（AND 是「整集」级别）
    assert [r["dir"] for r in search(tmp_output, "强化学习 RL")] == ["both"]
    assert [r["dir"] for r in search(tmp_output, "强化学习 不存在的词")] == []


def test_or_semantics(tmp_output):
    _mk(tmp_output, "ep-a", title="第一集", article="# 标题\n\nRL 训练很有趣。\n")
    _mk(tmp_output, "ep-b", title="第二集", article="# 标题\n\n今天天气不错。\n")
    got = search(tmp_output, "强化学习|RL")
    assert [r["dir"] for r in got] == ["ep-a"]
    assert got[0]["hits"][0]["match"] == "RL"


def test_phrase_query_keeps_space(tmp_output):
    _mk(tmp_output, "p1", title="p1", article="# 标题\n\n数据库 系统 设计是重点。\n")
    _mk(tmp_output, "p2", title="p2", article="# 标题\n\n数据库和系统都会被提到。\n")
    assert [r["dir"] for r in search(tmp_output, '"数据库 系统"')] == ["p1"]
    # 同样两个词分开写（AND）时两集都会被搜到
    assert {r["dir"] for r in search(tmp_output, "数据库 系统")} == {"p1", "p2"}


# ------------------------------------------------------------------ 时间戳

def test_transcript_ts_from_episode_fixture(tmp_output, episode):
    """复用公共 fixture：命中第 2 行 [00:10:07]。"""
    got = search(tmp_output, "原话")
    assert [r["dir"] for r in got] == [episode.name]
    item = got[0]
    assert item["title"] == "测试单集" and item["podcast"] == "测试台"

    ts_hits = [h for h in item["hits"] if h["field"] == "transcript"]
    assert ts_hits, "文字稿里应当有命中"
    assert ts_hits[0]["ts"] == "00:10:07"
    assert ts_hits[0]["line"] == 2
    assert ts_hits[0]["match"] == "原话"

    # ts 只给文字稿：文章里的引语命中（同一句话在正文中也出现）不给 ts
    article_hits = [h for h in item["hits"] if h["field"] == "article"]
    assert article_hits and all(h["ts"] is None for h in article_hits)
    assert all(h["field"] != "title" or h["ts"] is None for h in item["hits"])


def test_ts_none_when_line_has_no_clock(tmp_output):
    _mk(tmp_output, "ep", title="ep",
        transcript="[00:00:01] 开场\n没有时间戳的 关键词\n[00:00:03] 关键词又来了\n")
    item = search(tmp_output, "关键词")[0]
    assert [(h["line"], h["ts"]) for h in item["hits"]] == [(2, None), (3, "00:00:03")]


# ------------------------------------------------------------------ 排序

def test_title_hit_ranks_before_body_hit(tmp_output):
    _mk(tmp_output, "ep-title", title="地震与城市",
        article="# 地震与城市\n\n这里提到地震。\n又提到一次地震。\n")
    _mk(tmp_output, "ep-body", title="无关标题",
        article="# 无关标题\n\n地震。地震。地震。地震。\n")
    got = search(tmp_output, "地震")
    assert [r["dir"] for r in got] == ["ep-title", "ep-body"]
    assert got[0]["hits"][0]["field"] == "title"            # 标题命中排最前
    assert got[0]["score"] > got[1]["score"]
    assert [r["score"] for r in got] == sorted((r["score"] for r in got), reverse=True)


def test_heading_and_quote_outrank_body(tmp_output):
    """article 的一级/二级标题与引语权重高于正文。"""
    _mk(tmp_output, "ep-head", title="A", article="# 标题\n\n## 地震成因\n\n普通正文。\n")
    _mk(tmp_output, "ep-quote", title="B", article="# 标题\n\n普通正文。\n\n> 地震来了\n")
    _mk(tmp_output, "ep-body", title="C", article="# 标题\n\n普通正文，结尾提一句地震。\n")
    got = search(tmp_output, "地震")
    assert [r["dir"] for r in got] == ["ep-head", "ep-quote", "ep-body"]


def test_more_hits_score_higher(tmp_output):
    _mk(tmp_output, "few", title="few", article="# 标题\n\n地震。\n")
    _mk(tmp_output, "many", title="many", article="# 标题\n\n地震。地震。地震。地震。\n")
    got = {r["dir"]: r for r in search(tmp_output, "地震")}
    assert got["many"]["match_count"] > got["few"]["match_count"]
    assert got["many"]["score"] > got["few"]["score"]


def test_title_hit_beats_many_transcript_mentions(tmp_output):
    """次线性 + 权重：文字稿里刷十次，也压不过标题命中一次。"""
    _mk(tmp_output, "ep-title", title="地震", article="# 标题\n\n普通正文。\n")
    _mk(tmp_output, "ep-spoken", title="无关标题",
        transcript="".join(f"[00:00:{i:02d}] 地震\n" for i in range(10)))
    got = search(tmp_output, "地震")
    assert [r["dir"] for r in got] == ["ep-title", "ep-spoken"]
    assert got[1]["match_count"] == 10 and got[0]["match_count"] == 1


def test_limit_and_per_field_caps(tmp_output):
    _mk(tmp_output, "ep", title="关键词大全",
        article="# 关键词大全\n\n关键词一。关键词二。关键词三。关键词四。关键词五。\n",
        transcript="[00:00:01] 关键词六\n[00:00:02] 关键词七\n[00:00:03] 关键词八\n")
    item = search(tmp_output, "关键词", per_field=2)[0]
    assert item["match_count"] == 1 + 6 + 3          # 标题 1 + 正文 6 + 文字稿 3
    assert len(item["hits"]) == 4                    # 最多 per_field*2 条
    assert [h["field"] for h in item["hits"]] == ["title", "article", "article", "transcript"]

    for i in range(3):
        _mk(tmp_output, f"ep-{i}", title=f"ep {i}", article=f"# 标题\n\n关键词 {i}\n")
    assert len(search(tmp_output, "关键词", limit=2)) == 2
    assert len(search(tmp_output, "关键词", limit=99)) == 4


# ------------------------------------------------------------------ 上下文

def test_context_splices_back_to_source(tmp_output):
    """before/match/after 拼得回原文，边界（行首/行尾/文档首尾）也要对。"""
    prefix, suffix = "前" * 50, "后" * 50
    article = f"# 关键词开头\n\n{prefix}关键词{suffix}\n\n最后一行的关键词"
    _mk(tmp_output, "ep-ctx", title="上下文", article=article)
    item = search(tmp_output, "关键词")[0]
    head_hit, mid_hit, tail_hit = item["hits"]

    assert (head_hit["line"], mid_hit["line"], tail_hit["line"]) == (1, 3, 5)
    for h in item["hits"]:
        assert h["field"] == "article" and h["match"] == "关键词"
        before = h["before"][1:] if h["before"].startswith("…") else h["before"]
        after = h["after"][:-1] if h["after"].endswith("…") else h["after"]
        assert (before + h["match"] + after) in article

    # 每侧约 40 个字符，左右都被截断时补 …
    assert mid_hit["before"] == "…" + "前" * 40
    assert mid_hit["after"] == "后" * 40 + "…"
    # 文档开头的命中：左边没被截断，不给 …
    assert head_hit["before"] == "# " and head_hit["after"].endswith("…")
    # 文档末尾的命中：右边没被截断，不给 …
    assert tail_hit["after"] == "" and tail_hit["before"].startswith("…")


def test_context_on_transcript_line_boundaries(tmp_output):
    doc = "[00:00:01] 关键词在行首\n[00:00:02] 行尾是关键词\n"
    _mk(tmp_output, "ep", title="ep", transcript=doc)
    item = search(tmp_output, "关键词")[0]
    first, second = item["hits"]

    assert (first["line"], first["ts"]) == (1, "00:00:01")
    assert first["before"] == "[00:00:01] "        # 命中在行首，左边没被截断
    assert first["match"] == "关键词"

    assert (second["line"], second["ts"]) == (2, "00:00:02")
    assert second["after"] == "\n"                 # 命中在行尾，右边只剩换行
    assert second["match"] == "关键词"

    # 上下文按字符切，拼回去就是整篇文字稿（中文没被切坏）
    for h in item["hits"]:
        assert h["before"] + h["match"] + h["after"] == doc
        assert len(h["before"]) <= 40 and len(h["after"]) <= 40


# ------------------------------------------------------------------ 容错与统计

def test_empty_query_and_missing_root(tmp_output):
    _mk(tmp_output, "ep", title="ep", article="# 标题\n\n正文内容\n")
    assert search(tmp_output, "") == []
    assert search(tmp_output, "   ") == []
    assert search(tmp_output, "\t \n") == []
    missing = tmp_output / "根本不存在的目录"
    assert search(missing, "正文") == []
    assert stats(missing) == {"episodes": 0, "indexed": 0}


def test_broken_meta_does_not_raise(tmp_output):
    _mk(tmp_output, "坏-meta", article="# 标题\n\n这里有关键词\n", meta="{ 这不是 json")
    _mk(tmp_output, "meta 是数组", article="# 标题\n\n这里也有关键词\n", meta="[1, 2]")
    _mk(tmp_output, "meta 是目录", article="# 标题\n\n还有关键词\n", meta=False)
    (tmp_output / "meta 是目录" / "meta.json").mkdir()          # meta.json 居然是个目录
    (tmp_output / "散落文件.txt").write_text("关键词", encoding="utf-8")  # 根目录下的散落文件

    got = search(tmp_output, "关键词")
    assert {r["dir"] for r in got} == {"坏-meta", "meta 是数组", "meta 是目录"}
    assert all(r["podcast"] == "" and r["has_article"] is True for r in got)
    assert {r["dir"]: r["title"] for r in got}["坏-meta"] == "坏-meta"   # 缺 title 时用目录名


def test_unreadable_article_is_skipped(tmp_output):
    """article.md 读不了（这里是目录）时不当文章搜，也不能抛异常。"""
    d = tmp_output / "文章是目录"
    d.mkdir()
    (d / "meta.json").write_text('{"title": "文章是目录"}', encoding="utf-8")
    (d / "article.md").mkdir()
    (d / "transcript.txt").write_text("[00:00:01] 关键词\n", encoding="utf-8")

    got = search(tmp_output, "关键词")
    assert [r["dir"] for r in got] == ["文章是目录"]
    assert [h["field"] for h in got[0]["hits"]] == ["transcript"]


def test_no_meta_uses_dir_name_and_has_article_flag(tmp_output):
    d = tmp_output / "没有-meta"
    d.mkdir()
    (d / "article.md").write_text("# 标题\n\n关键词\n", encoding="utf-8")
    item = search(tmp_output, "关键词")[0]
    assert item["title"] == "没有-meta" and item["has_article"] is True

    _mk(tmp_output, "只有稿子", title="只有稿子", transcript="[00:00:01] 关键词\n")
    by_dir = {r["dir"]: r for r in search(tmp_output, "关键词")}
    assert by_dir["只有稿子"]["has_article"] is False
    assert by_dir["只有稿子"]["hits"][0]["field"] == "transcript"


def test_stats_counts(tmp_output):
    _mk(tmp_output, "有文章", title="a", article="# 标题\n\n正文\n")
    _mk(tmp_output, "只有稿子", title="b", transcript="[00:00:01] 正文\n")
    _mk(tmp_output, "只生成了-meta", title="c")                 # 有 meta，没正文/文字稿
    only_audio = tmp_output / "只有音频"
    only_audio.mkdir()
    (only_audio / "audio.m4a").write_bytes(b"\x00" * 8)
    (tmp_output / "散落.txt").write_text("x", encoding="utf-8")

    assert stats(tmp_output) == {"episodes": 3, "indexed": 2}


# ------------------------------------------------ 英文字词边界（子串匹配的噪音）
#
# 回归：搜 "AI" 曾经会把 "Fails"、"derail"、"OpenAI" 都算命中，
# 于是一篇完全无关的文章排进了结果；中文则必须保持子串匹配。


def _episode_with(tmp_output, name: str, body: str, *, transcript: str = ""):
    d = tmp_output / name
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"title": name, "podcast": "测试台"}, ensure_ascii=False), encoding="utf-8"
    )
    (d / "article.md").write_text(body, encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    return d


def test_ascii_query_does_not_match_inside_english_words(tmp_output):
    _episode_with(tmp_output, "相关", "# AI infra 的未来\n\nAI infra 正在重塑一切。")
    _episode_with(tmp_output, "无关", "# Clarity Act 投票失败\n\nit will derail the progress, and it fails.")

    hits = search(tmp_output, "AI")
    dirs = [h["dir"] for h in hits]
    assert dirs == ["相关"], f"搜 AI 不该命中 Fails / derail / progress 这类词内子串，实际 {dirs}"


def test_ascii_query_matches_at_word_boundaries(tmp_output):
    _episode_with(tmp_output, "相关", "# 关于 AI 与 ai 的文章\n\nAI 很重要。")
    hits = search(tmp_output, "ai")
    assert hits and hits[0]["match_count"] >= 2, f"大小写不同的整词都应命中，实际 {hits}"
    assert "AI" in hits[0]["hits"][0]["match"] or "ai" in hits[0]["hits"][0]["match"], \
        f"命中词应原样保留大小写，实际 {hits[0]['hits'][0]['match']}"


def test_ascii_term_with_punctuation_boundary(tmp_output):
    """带下划线/连字符的标识符要能整段命中，不能被当成两个词切开。"""
    _episode_with(tmp_output, "标识符", "# gpt-4o 与 claude_opus\n\ngpt-4o 是模型名。")
    hits = search(tmp_output, "gpt-4o")
    assert hits, "带连字符的查询应当能命中"


def test_chinese_query_still_matches_substring(tmp_output):
    """中文没有词边界，必须继续用子串匹配（否则「鱼」搜不到「鱼不存在」）。"""
    _episode_with(tmp_output, "中文", "# 鱼不存在\n\n他把名字缝在鱼身上。")
    hits = search(tmp_output, "鱼")
    assert hits, "单字中文查询应当能命中（子串匹配）"


def test_chinese_query_ignores_word_boundary_rule(tmp_output):
    """中英混排时，中文词按子串、英文词按整词，各管各的。"""
    _episode_with(tmp_output, "混排", "# RL 训练与强化学习方法\n\n强化学习（RL）是关键，derail 不是。")
    hits = search(tmp_output, "强化学习")
    assert hits, "中文词应能命中"
    hits2 = search(tmp_output, "RL")
    assert hits2, "英文缩写 RL 应能作为整词命中"


def test_ascii_query_matches_hyphenated_compounds(tmp_output):
    """回归：边界规则一度把连字符也算成「词内」，于是搜 AI 漏掉 AI-infra / AI-driven。

    连字复合词在英文里极常见，必须能命中；而字母直接相连的 Fails/derail 仍要挡住。
    """
    _episode_with(tmp_output, "连字", "# AI-infra 与 AI-driven 的产品\n\nAI-driven 的设计，AI-infra 的底座。")
    _episode_with(tmp_output, "噪音", "# Fails and derail\n\nThe plan fails and will derail.")

    hits = search(tmp_output, "AI")
    dirs = [h["dir"] for h in hits]
    assert dirs == ["连字"], f"AI 应命中 AI-infra / AI-driven，且不该命中 Fails / derail，实际 {dirs}"
    assert hits[0]["match_count"] >= 4, f"四个连字写法都该算命中，实际 {hits[0]['match_count']}"


def test_ascii_term_with_underscore_is_exact(tmp_output):
    """下划线连接的标识符整体匹配（claude_opus 不应被查 opus 命中）。"""
    _episode_with(tmp_output, "标识符", "# claude_opus\n\n这里说的是 claude_opus 模型。")
    assert search(tmp_output, "claude_opus"), "带下划线的标识符应能整体命中"
