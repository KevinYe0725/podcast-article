"""知识库与记忆：分词、切片、检索、实体、记忆，以及整套 HTTP 接口。

设计取舍都在这份测试里被钉住：
- 中文必须自己切词（SQLite 的 FTS5 不切中文，整句当"一个词"等于没检索）
- 检索命中必须带出处（哪一集 / 小标题 / 时间戳），否则答案无法追溯
- 没有嵌入模型时**功能不残缺**：退回词法检索，且状态里如实说明原因
- 记忆只写不猜：没有显式写入就没有记忆
"""
from __future__ import annotations

import json
import time

import pytest

from podcast_article import kb


@pytest.fixture()
def kb_env(tmp_output, monkeypatch):
    """把知识库指到临时文件（conftest 已经把 DB_PATH 打到 tmp_path）。"""
    kb.reindex(tmp_output, embed=False)
    return tmp_output


def _write_episode(root, name, article, transcript=None, meta=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "article.md").write_text(article, encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta or {
        "title": name, "podcast": "测试台", "url": "https://example.com/x"}, ensure_ascii=False),
        encoding="utf-8")
    return d


# ------------------------------------------------------------------ 分词

def test_tokenize_chinese_is_segmented():
    toks = kb.tokenize("朱邦华在英伟达做强化学习")
    assert " " in toks, f"中文必须被切成词：{toks!r}"
    assert "英伟达" in toks, f"用户词典里的专名要能整词切出：{toks}"
    assert "强化" in toks and "学习" in toks, toks
    assert "在" not in toks.split(), "停用词应该被丢掉"


def test_tokenize_keeps_english_and_numbers():
    toks = kb.tokenize("SGLang 2.0 与 DeepSeek-V3")
    assert "sglang" in toks and "deepseek" in toks, toks
    assert "2.0" in toks or "2" in toks


# ------------------------------------------------------------------ 切片

def test_split_article_keeps_heading_context():
    md = ("# 大标题\n\n" + "第一段正文。" * 20 + "\n\n## 小标题\n\n"
          + "小标题下的正文。" * 20 + "\n\n> 一句引语 [00:10:07]\n")
    chunks = kb._split_article(md)
    assert any(c["kind"] == "heading" and c["text"] == "大标题" for c in chunks)
    small = [c for c in chunks if c.get("heading") == "小标题"]
    assert small, "切片必须记住自己属于哪个小标题"
    quote = [c for c in chunks if c["kind"] == "quote"][0]
    assert quote["start_sec"] == 607 and "00:10:07" not in quote["text"], "引语要带时间戳且不把标记念进去"


def test_split_transcript_windows_with_timestamps():
    lines = "\n".join(f"[00:{i:02d}:00] 第 {i} 句内容够长一些" for i in range(0, 12))
    chunks = kb._split_transcript(lines)
    assert chunks and all(c["start_sec"] is not None for c in chunks)
    assert chunks[0]["start_sec"] == 0 and chunks[-1]["end_sec"] >= 9 * 60


def test_split_transcript_without_timestamps_still_chunks():
    chunks = kb._split_transcript("没有时间戳的纯文本。" * 200)
    assert chunks and chunks[0]["start_sec"] is None


# ------------------------------------------------------------------ 索引

def test_index_and_count(kb_env):
    _write_episode(kb_env, "20240101-甲", "# 标题\n\n" + "这是关于强化学习与推理系统的一段正文。" * 30,
                   transcript="[00:00:10] 一句文字稿内容\n[00:00:20] 另一句也够长一点")
    r = kb.index_all(kb_env, embed=False)
    st = kb.stats()
    assert r["episodes"] == 1, r
    assert st["docs"] == 2, f"一篇集应产出文章 + 文字稿两条文档记录：{st}"
    assert st["passages"] > 0


def test_index_is_incremental(kb_env):
    _write_episode(kb_env, "20240102-乙", "# 标题\n\n" + "旧内容 legacytoken 应该被换掉。" * 40)
    kb.index_all(kb_env, embed=False)
    first = kb.stats()["passages"]
    # 内容没变 → 再来一次不会增加切片（用 sha 判断，不是无脑重跑）
    kb.index_all(kb_env, embed=False)
    assert kb.stats()["passages"] == first
    # 改了正文 → 会重新索引这一篇（用「能不能搜到新词」判断，比数字数切片数可靠）
    (kb_env / "20240102-乙" / "article.md").write_text(
        "# 标题\n\n" + "改过的正文，里面有个独特词 zebracorn，旧的标记已经被删掉了。" * 60,
        encoding="utf-8")
    kb.index_all(kb_env, embed=False)
    assert kb.search("zebracorn", k=3)["hits"], "改过的内容应该被重新索引"
    # 用一个不会与其它词部分重合的假词判断"旧内容被换掉"（FTS 是 OR 语义，
    # 拿中文短语去查会撞上仍然存在的其它词）。
    # 注意新正文里**不能**再出现 legacytoken 这个词 —— 之前这行写的是
    # 「旧标记 oldmarker 已删除」，于是搜索命中的是新正文里那个词，
    # 断言失败被误当成"旧切片没清掉"。查旧词必须让它在库里彻底不存在。
    assert not kb.search("legacytoken", k=3)["hits"], "旧切片没被清掉"
    assert kb.search("zebracorn", k=3)["hits"][0]["dir"] == "20240102-乙"


# ------------------------------------------------------------------ 语义检索

def test_semantic_drops_hits_below_the_floor(kb_env, monkeypatch):
    """相似度低于下限的向量命中必须丢掉。

    没有下限时，余弦相似度对**任何**输入都有值，于是「完全无关的问题」也会拿到 k 条
    貌似相关的原文去喂模型 —— 比搜不到更坏。这里用打桩向量把两种情形分开：
    切片 A 与查询同向（相似度 1.0），切片 B 正交（0.0）。
    """
    import numpy as np

    _write_episode(kb_env, "20240104-丁",
                   "# 标题\n\n这是甲切片的内容，讲的是算力成本与推理单价的下降趋势。\n\n"
                   "## 乙\n\n这是乙切片的内容，讲的是完全另一件事情。\n")
    kb.index_all(kb_env, embed=False)
    ids = [r["id"] for r in kb.connect().execute(
        "SELECT id FROM passages ORDER BY id").fetchall()]

    monkeypatch.setattr(kb, "_embeddings_for", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(kb, "_load_vectors", lambda conn: {
        "mat": np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
        "ids": np.array(ids[:2], dtype="int64"), "model": kb.EMBED_MODEL,
        "db": str(kb.db_path())})

    res = kb.search("算力成本", k=5, mode="semantic")
    assert [h["id"] for h in res["hits"]] == [ids[0]], "正交的那条不该出现"
    assert res["hits"][0]["score"] == pytest.approx(1.0)

    # 把下限抬到 1.0 之上 → 连同向的那条也留不下（证明是下限在起作用，不是别的）
    monkeypatch.setattr(kb, "SEM_MIN_SIM", 1.01)
    assert kb.search("算力成本", k=5, mode="semantic")["hits"] == []


def test_semantic_floor_keeps_relevant_hits(kb_env, monkeypatch):
    """下限不能把该给的也砍掉：相似度高于下限的照常返回。"""
    import numpy as np

    _write_episode(kb_env, "20240105-戊",
                   "# 标题\n\n第一条内容是足够长的正文，用来占满一个切片的位置。\n\n"
                   "## 二\n\n第二条内容同样要足够长，不然会被切片规则丢掉。\n")
    kb.index_all(kb_env, embed=False)
    ids = [r["id"] for r in kb.connect().execute("SELECT id FROM passages ORDER BY id").fetchall()]

    monkeypatch.setattr(kb, "_embeddings_for", lambda texts: [[1.0, 0.0] for _ in texts])
    monkeypatch.setattr(kb, "_load_vectors", lambda conn: {
        "mat": np.array([[0.9, 0.4359], [0.95, 0.3122]], dtype="float32"),  # 0.9 / 0.95
        "ids": np.array(ids[:2], dtype="int64"), "model": kb.EMBED_MODEL,
        "db": str(kb.db_path())})

    hits = kb.search("内容", k=5, mode="semantic")["hits"]
    assert len(hits) == 2 and hits[0]["score"] == pytest.approx(0.95, abs=1e-3)


def test_embed_pending_backfills_an_old_library(kb_env, monkeypatch):
    """先建的库（当时没有嵌入模型）要能补上向量，而不是永远关着语义检索。

    `index_all` 只在**新增切片**时才嵌入，所以「内容没变」的老库点了重建也没用。
    """
    _write_episode(kb_env, "20240106-己",
                   "# 标题\n\n这段内容需要向量，所以它必须长到能被切成一个切片。\n")
    kb.index_all(kb_env, embed=False)
    assert kb.stats()["vectors"] == 0

    calls: list[int] = []

    def fake_embed(texts):
        calls.append(len(texts))
        return [[0.5, 0.5] for _ in texts]

    monkeypatch.setattr(kb, "_embeddings_for", fake_embed)
    conn = kb.ensure_schema(kb.connect())
    n = kb.embed_pending(conn, log=lambda *_: None)
    conn.close()

    assert n > 0 and kb.stats()["vectors"] == n
    assert calls == [n], "只该嵌入缺向量的那些"
    conn = kb.ensure_schema(kb.connect())
    assert kb.embed_pending(conn, log=lambda *_: None) == 0, "已经补过的不该重复嵌入"
    conn.close()


# ------------------------------------------------------------------ 检索

def test_search_returns_sources(kb_env):
    _write_episode(
        kb_env, "20240103-丙",
        "# 孙正义的赌注\n\n孙正义清仓英伟达、押注 OpenAI，这是他 2024 年最重要的仓位调整。\n\n"
        "## 软银的处境\n\n软银当时负债很高，被迫出售阿里巴巴的股份来换取现金。\n",
        transcript="[00:05:00] 孙正义谈软银的负债\n[00:05:10] 他说要靠 OpenAI 翻身\n")
    kb.index_all(kb_env, embed=False)
    res = kb.search("孙正义 软银", k=5)
    assert res["hits"], "应该能检索到"
    hit = res["hits"][0]
    assert hit["dir"] == "20240103-丙"
    pub = kb._hit_public(hit)
    for key in ("dir", "title", "heading", "text", "match"):
        assert key in pub, f"命中必须带出处字段 {key}"
    assert pub["text"].strip()


def test_search_handles_empty_and_nonsense(kb_env):
    assert kb.search("", k=3)["hits"] == []
    assert kb.search("   ", k=3)["hits"] == []
    kb.search("完全不存在的词组zzzz", k=3)          # 不该抛异常


def test_search_can_filter_by_episode(kb_env):
    _write_episode(kb_env, "20240104-丁", "# 甲集\n\n" + "共同关键词 alpha 出现。" * 20)
    _write_episode(kb_env, "20240105-戊", "# 乙集\n\n" + "共同关键词 alpha 也出现。" * 20)
    kb.index_all(kb_env, embed=False)
    all_hits = kb.search("alpha", k=10)
    one = kb.search("alpha", k=10, dirs=["20240104-丁"])
    assert {h["dir"] for h in one["hits"]} == {"20240104-丁"}
    assert len({h["dir"] for h in all_hits["hits"]}) >= 2


def test_vector_path_uses_fake_embedder(kb_env, monkeypatch):
    """没有真模型也要把向量那条路测到：用确定性假向量替换嵌入器。"""
    import numpy as np
    _write_episode(kb_env, "20240106-己",
                   "# 甲\n\n" + "关于对齐与安全的长文段落。" * 30)
    monkeypatch.setattr(kb, "_embeddings_for", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    lib = kb.index_all(kb_env, embed=True)
    assert lib["passages"] > 0
    st = kb.stats()
    assert st["vectors"] > 0, "假嵌入器应该写入向量"
    res = kb.search("任意问题", k=3, mode="semantic")
    assert res["mode"] == "semantic" and res["hits"], res
    assert all("rrf" in h or "score" in h for h in res["hits"])


# ------------------------------------------------------------------ 重排（RRF 之后）

# 为什么这一组测试必须存在：**BM25 与余弦都偏爱短句**，于是 20 字的 ASR 碎片能把
# 200 字的实质段落挤出前 6 —— 喂给模型的"资料"全是断句，回答只能是「资料里没有」。
# 真库实测：问「孙正义是怎么押注 AI 的？」，含答案的 115 字正文在词法排第 14、语义排
# 第 15。下面这些合成库用例把重排的三件事（覆盖率/长度、去重、每集上限）分别钉住，
# 全部 embed=False，不依赖任何模型。

def _passage_ids() -> dict:
    conn = kb.connect()
    try:
        return {r["text"]: r["id"] for r in conn.execute("SELECT id, text FROM passages")}
    finally:
        conn.close()


# 8 条**用词各不相同**但都含「推理成本」的段落：只有一集时用它来试每集上限。
# （不能拿"只差一个编号"的段落来试 —— 那种本来就是重复，会被去重先收走。）
_MANY_PASSAGES = "\n\n".join([
    "电价高企的数据中心里，机柜空间和散热才是真正吃钱的地方，这一层很少被算进推理成本。",
    "良率决定了晶圆级方案能不能量产，废掉的整片晶圆最后都会摊进推理成本。",
    "带宽从封装走线换到芯片内部光刻的线，连线密度的提升才是推理成本下降的来源。",
    "编译期静态调度一旦撞上 MoE，芯片空转的那部分也会记在推理成本上。",
    "散热方案决定了能不能把机柜塞满，装得越满，摊到每个 token 的推理成本越低。",
    "内存带宽的价格每年都在涨，涨得比算力还快，推理成本因此被拖着走。",
    "推理成本的算法很简单：用户愿意付的钱减去买卡加耗电，两边一减就是答案。",
    "模型越大，每产出一个 token 要读的权重越多，推理成本随参数线性上升。",
])


def _hit_ids(res) -> list:
    return [h["id"] for h in res["hits"]]


def test_rerank_prefers_a_substantive_paragraph_over_a_fragment(kb_env):
    """20 字的碎片不许把 200 字的实质段落挤下去。"""
    frag = "孙正义押注 AI 的动作非常快。"                     # 碎片：只覆盖部分查询词
    body = ("孙正义今天在 AI 上的仓位，几乎只剩两张牌：OpenAI 和 ARM。"
            "他清空了阿里巴巴和英伟达的股票，换来 OpenAI 累计 13% 的股份，"
            "成为微软之外持股最多的股东，软银因此被市场叫作「OpenAI 概念股」。"
            "这笔押注的代价是现金，换回来的是叙事；仓位集中到什么程度，"
            "才是判断他这次到底赌了多大的唯一办法，而不是看他说了什么。")
    assert 12 <= len(frag) <= 30, f"这条要是「碎片」：{len(frag)} 字"
    assert 150 <= len(body) <= 300, f"这条要是「实质段落」：{len(body)} 字"

    _write_episode(kb_env, "20240110-重排", f"# 标题\n\n{frag}\n\n{body}\n")
    kb.index_all(kb_env, embed=False)

    q = "孙正义押注 AI 的仓位"
    qset = kb._token_set(q)
    assert len(qset) >= 3, qset
    frag_cov = kb._coverage(qset, kb._token_set(frag))
    body_cov = kb._coverage(qset, kb._token_set(body))
    assert body_cov > frag_cov, f"前提没成立：碎片 {frag_cov} 应低于段落 {body_cov}"

    hits = kb.search(q, k=4)["hits"]
    ids = _passage_ids()
    assert ids[body] in _hit_ids(kb.search(q, k=4)), "实质段落必须被检索到"
    order = _hit_ids(kb.search(q, k=4))
    assert order.index(ids[body]) < order.index(ids[frag]), \
        "实质段落必须排在碎片前面（旧排序里碎片靠 BM25 长度偏置排第一）"
    assert hits[0]["id"] == ids[body]
    # 排序分必须能解释自己：覆盖率、长度、来源三项都要留下来
    top = next(h for h in hits if h["id"] == ids[body])
    assert top["rank_score"] > 0 and top["coverage"] == pytest.approx(body_cov)
    assert top["len_prior"] == pytest.approx(1.0) and top["src_prior"] == pytest.approx(1.0)
    # rrf 原样保留（界面上的 score 用的还是它），新分数只写在 rank_score 里
    assert top["rrf"] > 0
    frag_hit = next(h for h in hits if h["id"] == ids[frag])
    assert frag_hit["rank_score"] < frag_hit["rrf"] * 0.6, \
        "碎片的长度先验必须真的压到它的排序分上"


def test_rerank_dedups_near_identical_passages(kb_env):
    """词集合高度重合的切片只留一条：同一集里 6 条近义碎片等于只给模型一条信息。"""
    a = "推理芯片的成本结构决定了它贵不贵，判断办法就是算每百万 token 的钱。"
    b = "推理芯片的成本结构决定了它贵不贵，判断办法就是算每百万 token 的账。"   # 近乎重复
    c = "另一件完全不相干的事：这里的切片讲的是别的话题，用词也全不一样。"
    _write_episode(kb_env, "20240111-去重", f"# 标题\n\n{a}\n\n{b}\n\n{c}\n")
    kb.index_all(kb_env, embed=False)

    tokens = kb._token_set(a)
    assert kb._jaccard(tokens, kb._token_set(b)) >= kb.DEDUP_JACCARD, "前提：这两条算重复"

    hits = kb.search("推理芯片 成本结构 贵不贵", k=5)["hits"]
    texts = [h["text"] for h in hits]
    assert not (a in texts and b in texts), f"重复的两条只该留一条：{texts}"
    assert len([t for t in texts if "成本结构" in t]) == 1, texts


def test_rerank_caps_per_episode_but_never_returns_fewer(kb_env):
    """同一集最多 4 条；但**上限不能导致结果变少** —— 凑不够 k 就放宽补满。"""
    _write_episode(kb_env, "20240112-上限", f"# 标题\n\n{_MANY_PASSAGES}\n")
    kb.index_all(kb_env, embed=False)

    hits = kb.search("推理成本", k=6)["hits"]
    assert len(hits) == 6, f"只有一集相关时也要补满 k 条，不能因为上限变少：{len(hits)}"
    assert {h["dir"] for h in hits} == {"20240112-上限"}


def test_rerank_per_episode_cap_when_other_episodes_can_fill(kb_env):
    """有别的集能补时，上限必须生效：不能一集霸占全部名额。"""
    _write_episode(kb_env, "20240113-多", f"# 标题\n\n{_MANY_PASSAGES}\n")
    for j in range(3):
        _write_episode(kb_env, f"2024011{j}-少",
                       f"# 标题\n\n第 {j} 集也谈推理成本的算法，说法跟别人不一样。\n")
    kb.index_all(kb_env, embed=False)

    hits = kb.search("推理成本", k=6)["hits"]
    assert len(hits) == 6, len(hits)
    from collections import Counter
    per = Counter(h["dir"] for h in hits)
    assert per["20240113-多"] <= kb.PER_DIR_MAX, f"一集最多 {kb.PER_DIR_MAX} 条：{per}"
    assert len(per) >= 2, f"别的集要能挤进来：{per}"


def test_rerank_weights_do_not_exclude_transcript_only_episodes(kb_env):
    """来源先验只调顺序，不排除 —— 有些集只有文字稿，那时它必须还能被选中。"""
    d = kb_env / "20240114-只有文字稿"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.txt").write_text(
        "[00:00:10] 这一集没有文章，只有文字稿，讲的是核电与电价的关系。\n"
        "[00:00:20] 核电的边际成本很低，但前期建设投入巨大。\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"title": "只有文字稿"}, ensure_ascii=False),
                                encoding="utf-8")
    kb.index_all(kb_env, embed=False)

    hits = kb.search("核电 边际成本", k=3)["hits"]
    assert hits, "只有文字稿的集也必须能被检索到"
    assert hits[0]["doc_kind"] == "transcript"
    assert hits[0]["src_prior"] == pytest.approx(kb._SRC_TRANSCRIPT)


# ------------------------------------------------------------------ IDF 加权覆盖率

# 为什么覆盖率要按词的信息量加权（而不是数命中几个词）：等权口径下「只命中一个泛词」
# 与「命中一个稀有专名」拿到的覆盖率一模一样，于是只谈泛词的无关段落能和实质证据同档，
# 混进 top-10 挤掉答案。真库实测（问「孙正义是怎么押注 AI 的？」）：`ai` 出现在 382 条
# 切片、`孙正义` 79 条、`押注` 8 条 —— 等权口径下讲 Dario Amodei 的段落拿到 0.33，
# 与含答案的正文只差一档，最后它排到了第 5。
#
# 下面用合成库把口径钉死：稀有词只在这一集的这一段里出现，查询 = 稀有词 + 泛词。

def test_idf_coverage_prefers_the_passage_with_the_rare_word(kb_env):
    """命中稀有词的段落必须排在只命中泛词的段落前面。"""
    rare = "孙正义今天把仓位压在 OpenAI 和 ARM 两张牌上，这才是他真正的动作。"
    common = "有人押注的方向是另一条路径，跟上面那件事没有关系。"
    filler = "这一集讲的是别的事情，用来把库撑大。" * 4
    _write_episode(kb_env, "20240120-稀有", f"# 标题\n\n{rare}\n\n{filler}\n")
    for j in range(4):
        _write_episode(kb_env, f"2024012{j}-泛词", f"# 标题\n\n{common}\n\n{filler}\n")
    kb.index_all(kb_env, embed=False)

    conn = kb.connect()
    try:
        df = kb._df_table(conn)
    finally:
        conn.close()
    assert df, "df 表必须能算出来（拿不到时下面测的是退回路径，不是 IDF）"
    N = kb._DF_CACHE["total"]
    assert df.get("孙正义") == 1, f"稀有词只该出现在一处：df={df.get('孙正义')}"
    assert df.get("押注") >= 4, f"泛词该到处都有：df={df.get('押注')}"
    assert kb._idf("孙正义", df, N) > kb._idf("押注", df, N), "稀有词的 idf 必须更大"

    q = "孙正义 押注"
    qset = kb._token_set(q)
    assert kb._coverage(qset, kb._token_set(common)) == pytest.approx(
        kb._coverage(qset, kb._token_set(rare))), "前提：等权口径下两者完全一样"
    rare_idf = kb._coverage_idf(qset, kb._token_set(rare), df, N)
    common_idf = kb._coverage_idf(qset, kb._token_set(common), df, N)
    assert rare_idf > common_idf, f"加权口径下必须分得开：稀有 {rare_idf} vs 泛词 {common_idf}"

    hits = kb.search(q, k=5)["hits"]
    ids = _passage_ids()
    order = _hit_ids(kb.search(q, k=5))
    assert ids[rare] in order and ids[common] in order, "两条都该被检索到"
    assert order.index(ids[rare]) < order.index(ids[common]), \
        "命中稀有词的那条必须排在只命中泛词的前面"
    top = hits[0]
    assert top["id"] == ids[rare] and top["coverage_idf"] > top["coverage"] - 1e-9
    # coverage 保留等权口径（界面与既有断言按它读），加权口径另写在 coverage_idf 里
    assert top["coverage"] == pytest.approx(
        kb._coverage(qset, kb._token_set(f"{top.get('heading') or ''} {top['text']}")))


def test_idf_coverage_falls_back_when_df_is_unavailable(kb_env, monkeypatch):
    """拿不到 df 时必须退回等权覆盖率，绝不让搜索炸掉或排不出序。"""
    _write_episode(kb_env, "20240121-退回", "# 标题\n\n" + "这是一段讲推理成本的正文内容。" * 8)
    kb.index_all(kb_env, embed=False)

    monkeypatch.setattr(kb, "_df_table", lambda conn: None)
    hits = kb.search("推理成本", k=3)["hits"]
    assert hits, "df 拿不到也要能搜出结果"
    assert all(h["coverage_idf"] == pytest.approx(h["coverage"]) for h in hits), \
        "退回路径上两个口径必须一致（否则排序分与界面读数会对不上）"


def test_df_cache_invalidates_when_the_library_grows(kb_env):
    """索引后新增切片，df 必须重算 —— 吃到过期 df 不会报错，只会让排序悄悄错掉。"""
    rare = "孙正义今天把仓位压在 OpenAI 和 ARM 两张牌上，这才是他真正的动作。"
    common = "有人押注的方向是另一条路径，跟上面那件事没有关系。"
    filler = "这一集讲的是别的事情，用来把库撑大。" * 4
    _write_episode(kb_env, "20240122-旧", f"# 标题\n\n{rare}\n\n{filler}\n")
    for j in range(3):
        _write_episode(kb_env, f"2024013{j}-泛词", f"# 标题\n\n{common}\n\n{filler}\n")
    kb.index_all(kb_env, embed=False)

    conn = kb.connect()
    try:
        before = kb._df_table(conn).get("孙正义")
        n_before = kb._DF_CACHE["total"]
    finally:
        conn.close()
    hits = kb.search("孙正义 押注", k=5)["hits"]
    score_before = next(h["rank_score"] for h in hits if h["text"] == rare)

    # 新的一集也含这个稀有词 → df 变大 → 它的 idf 变小 → 覆盖率与排序分必须跟着变
    _write_episode(kb_env, "20240123-新", f"# 标题\n\n{rare}\n\n{filler}\n")
    kb.index_all(kb_env, embed=False)

    conn = kb.connect()
    try:
        after = kb._df_table(conn).get("孙正义")
        n_after = kb._DF_CACHE["total"]
    finally:
        conn.close()
    assert before == 1 and after == 2, f"df 必须反映新数据：{before} → {after}"
    assert n_after > n_before, f"切片总数也要跟着变：{n_before} → {n_after}"

    hits = kb.search("孙正义 押注", k=5)["hits"]
    score_after = next(h["rank_score"] for h in hits if h["text"] == rare)
    assert score_after < score_before, \
        f"稀有词不再稀有后排序分必须下降（吃到过期 df 就会纹丝不动）：{score_before} → {score_after}"


# ------------------------------------------------------------------ 每集上限（自适应）

# 上限为什么不能固定 4：问一个人名/专名时，答案所在的那一集本来就有 10 条实质证据。
# 真库实测：含「孙正义」的 79 条切片**全部来自同一集**，固定上限把它掐在 4 条，
# 剩下的名额被别的集里只是碰巧出现某个泛词的段落填满。
# 判据见 kb.DOMINANCE_RATIO：谁明显最强谁说了算，但最多也只放宽到 k。

def _fake_kept(n: int, dirname: str, score: float, prefix: str = "") -> list[dict]:
    return [{"dir": dirname, "rank_score": score, "text": f"{prefix}{dirname}-{i}"}
            for i in range(n)]


def test_adaptive_cap_lets_a_dominant_episode_fill_more_than_the_cap():
    """某一集明显是这个问题的答案所在时，允许它给出多于 PER_DIR_MAX 条。"""
    # 甲集 1.0 vs 别的集 0.6：0.6 < DOMINANCE_RATIO(0.8) × 1.0 → 甲集明显更强
    kept = (_fake_kept(8, "甲集", 1.0) + _fake_kept(2, "乙集", 0.6)
            + _fake_kept(2, "丙集", 0.6))
    out = kb._pick_diverse(kept, 10, kb.PER_DIR_MAX)
    from collections import Counter
    per = Counter(h["dir"] for h in out)
    assert len(out) == 10, f"要给满 k 条：{len(out)}"
    assert per["甲集"] > kb.PER_DIR_MAX, f"占优的那一集必须能超过 {kb.PER_DIR_MAX} 条：{per}"
    assert per["甲集"] <= 10, f"但最多也只能放宽到 k：{per}"
    assert per["甲集"] == 8, f"甲集有 8 条强证据就该全给：{per}"
    # 剩下 2 个名额给别的集里分数最高的条目（乙/丙 同分，按稳定序取）
    assert per["乙集"] + per["丙集"] == 2, f"别的集必须拿到剩下的名额：{per}"
    scores = [h["rank_score"] for h in out]
    assert scores == sorted(scores, reverse=True), f"输出必须严格按分数序：{scores}"


def test_adaptive_cap_keeps_diversity_when_episodes_are_comparable():
    """多集都有可比证据时，来源多样性必须保留（不会全被一集吃光）。"""
    # 乙/丙 0.9 > 0.8 × 1.0 → 没有谁明显更强，上限照旧
    kept = (_fake_kept(6, "甲集", 1.0) + _fake_kept(3, "乙集", 0.9)
            + _fake_kept(3, "丙集", 0.9))
    out = kb._pick_diverse(kept, 8, kb.PER_DIR_MAX)
    from collections import Counter
    per = Counter(h["dir"] for h in out)
    assert len(out) == 8, len(out)
    assert per["甲集"] <= kb.PER_DIR_MAX, f"没有谁明显更强时上限照旧：{per}"
    assert len(per) >= 3, f"三集都该有代表：{per}"


def test_adaptive_cap_never_returns_fewer_than_k():
    """放宽上限也不该让结果变少：只有一集相关时必须补满 k 条。"""
    kept = _fake_kept(2, "独集", 1.0)
    out = kb._pick_diverse(kept, 5, kb.PER_DIR_MAX)
    assert len(out) == 2, f"库里只有 2 条，就给 2 条（不能凭空变多）：{len(out)}"
    kept = _fake_kept(9, "独集", 1.0)
    out = kb._pick_diverse(kept, 6, kb.PER_DIR_MAX)
    assert len(out) == 6, f"只有一集相关时要补满 k：{len(out)}"


def test_search_lets_a_dominant_episode_supply_more_than_the_cap(kb_env):
    """端到端：主题集中的那一集要能拿到 > PER_DIR_MAX 条（真库「孙正义」就是这个形状）。"""
    _write_episode(kb_env, "20240124-主", f"# 标题\n\n{_MANY_PASSAGES}\n")
    for j in range(2):
        # 别的集只有一条与查询无关的长正文：分数远低于主集，不该抢名额
        _write_episode(kb_env, f"2024014{j}-旁",
                       "# 标题\n\n" + "这一集讲的是完全另一件事情，与检索词毫无关系。" * 15 + "\n")
    kb.index_all(kb_env, embed=False)

    hits = kb.search("推理成本", k=8)["hits"]
    from collections import Counter
    per = Counter(h["dir"] for h in hits)
    assert per["20240124-主"] > kb.PER_DIR_MAX, \
        f"主题集中的那一集必须能超过 {kb.PER_DIR_MAX} 条：{per}"
    assert per["20240124-主"] <= 8, f"但最多放宽到 k：{per}"


def test_length_and_source_priors_are_soft_weights():
    """两个先验的锚点值：长度是软权重（永远 > 0），小标题权重低于正文。"""
    assert kb._length_prior(20) == pytest.approx(0.55)
    assert kb._length_prior(300) == pytest.approx(1.0)
    assert kb._length_prior(2000) == pytest.approx(0.9)
    assert kb._length_prior(70) == pytest.approx(0.775, abs=0.01), "40–100 中间过渡"
    assert 0 < kb._length_prior(1) < 1, "再短也只是降权，不能归零（等于硬过滤）"
    assert kb._source_prior({"doc_kind": "article", "kind": "body"}) == 1.0
    assert kb._source_prior({"doc_kind": "article", "kind": "quote"}) == 1.0
    assert kb._source_prior({"doc_kind": "article", "kind": "heading"}) == 0.7
    assert kb._source_prior({"doc_kind": "transcript", "kind": "body"}) == 0.85
    assert 0 < kb._source_prior({"doc_kind": "transcript", "kind": "heading"}) < 1


# ------------------------------------------------------------------ 实体

def test_entity_extraction_keeps_names_drops_common_words():
    text = ("孙正义在软银的发布会上说，黄仁勋的英伟达卖得太贵，他读完了《穷查理宝典》。"
            "这件事他明白得很清楚，东西也不便宜，交易所的人却在买。")
    got = dict((n, t) for n, t, _w in kb.extract_entities(text))
    assert "孙正义" in got, f"人名应该被抽出来：{got}"
    assert got.get("穷查理宝典") == "media", f"书名号里的要当书影音：{got}"
    for junk in ("明白", "东西", "交易所", "太贵"):
        assert junk not in got, f"{junk} 不该被当实体：{got}"


def test_entity_detail_has_sources(kb_env):
    _write_episode(kb_env, "20240107-庚",
                   "# 孙正义\n\n" + "孙正义押注 OpenAI，这是他最重要的一次仓位调整。" * 6
                   + "\n\n## 软银\n\n" + "孙正义让软银卖掉阿里巴巴来换取现金。" * 6)
    kb.index_all(kb_env, embed=False)
    d = kb.entity_detail("孙正义")
    assert d["found"] and d["count"] >= 2
    assert any(p["heading"] for p in d["passages"]), "出处要带小标题"
    assert kb.entity_detail("不存在的人")["found"] is False


def test_entity_list_filters_generic_terms(kb_env):
    for i in range(6):
        body = ("孙正义投资了这家公司。" if i < 3 else "日本企业家在东京谈生意。") * 30
        _write_episode(kb_env, f"2024011{i}-辛", f"# 主题{i}\n\n" + body)
    kb.index_all(kb_env, embed=False)
    names = [e["name"] for e in kb.entities(limit=50, min_count=1)]
    assert "孙正义" in names, names[:20]
    assert "美国政府" not in names, "泛词不该出现在实体榜"


# ------------------------------------------------------------------ 记忆

def test_memory_crud_and_search(kb_env):
    m = kb.memory_add("用户偏好：开篇用具体场景切入", kind="preference", pinned=True)
    assert m["id"] > 0 and m["pinned"] == 1
    m2 = kb.memory_add("关注强化学习基础设施", kind="fact")
    assert len(kb.memory_list()) == 2
    assert [x["id"] for x in kb.memory_list(query="开篇")] == [m["id"]], "记忆检索要能用中文词查到"
    kb.memory_update(m2["id"], text="关注强化学习与推理系统", pinned=True)
    assert kb.memory_get(m2["id"])["pinned"] == 1
    assert "推理系统" in kb.memory_get(m2["id"])["text"]
    # 改完文本后，新的词也能被检索到（FTS 行要跟着重建）
    assert any(x["id"] == m2["id"] for x in kb.memory_list(query="推理系统"))
    assert kb.memory_delete(m["id"]) is True
    assert len(kb.memory_list()) == 1


def test_memory_rejects_empty(kb_env):
    with pytest.raises(ValueError):
        kb.memory_add("   ")


def test_memory_for_prompt_prefers_pinned(kb_env):
    kb.memory_add("普通记忆一条", kind="fact")
    pinned = kb.memory_add("置顶：文章开篇必须用场景切入", kind="preference", pinned=True)
    got = kb.memory_for_prompt("帮我写一篇文章的开头")
    assert got and got[0]["id"] == pinned["id"], "置顶的必须排在最前"
    assert len(got) <= 6


# ------------------------------------------------------------------ 接口

def test_kb_status_endpoint(client):
    d = client.get("/api/kb/status").get_json()
    for key in ("docs", "passages", "entities", "memory", "semantic", "db"):
        assert key in d
    assert d["semantic"] in (True, False)


def test_kb_reindex_and_search_endpoints(client, kb_env):
    r = client.post("/api/kb/reindex", json={})
    assert r.status_code == 200
    for _ in range(40):
        st = client.get("/api/kb/status").get_json()
        if st["indexing"]["state"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert st["indexing"]["state"] == "done", st["indexing"]
    hits = client.get("/api/kb/search?q=地震&k=3").get_json()
    assert "hits" in hits and hits["mode"] in ("lexical", "hybrid", "semantic")
    assert client.get("/api/kb/search").status_code == 400, "没有 q 要报 400"


def test_kb_entities_and_entity_endpoints(client, kb_env):
    (kb_env / "20240108-壬" / "article.md").parent.mkdir(parents=True, exist_ok=True)
    _write_episode(kb_env, "20240108-壬", "# 孙正义\n\n孙正义说过软银必须押注 AI。孙正义也谈过 ARM。\n")
    client.post("/api/kb/reindex", json={})
    for _ in range(40):
        if client.get("/api/kb/status").get_json()["indexing"]["state"] != "running":
            break
        time.sleep(0.2)
    d = client.get("/api/kb/entities?type=person&limit=10&min_count=1").get_json()
    assert "entities" in d and "types" in d
    names = [e["name"] for e in d["entities"]]
    if "孙正义" in names:
        detail = client.get("/api/kb/entity/孙正义").get_json()
        assert detail["found"] and detail["passages"]
    assert client.get("/api/kb/entity/查无此人").status_code == 404


def test_memory_endpoints_crud(client):
    r = client.post("/api/memory", json={"text": "用户偏好：少用形容词", "kind": "preference", "pinned": True})
    assert r.status_code == 200
    mid = r.get_json()["id"]
    items = client.get("/api/memory").get_json()
    assert any(x["id"] == mid for x in items["items"]) and "kinds" in items
    assert client.patch(f"/api/memory/{mid}", json={"pinned": False}).get_json()["pinned"] == 0
    assert client.get("/api/memory?q=形容词").get_json()["items"], "记忆检索接口要能用"
    assert client.post("/api/memory", json={"text": " "}).status_code == 400
    assert client.delete(f"/api/memory/{mid}").get_json()["ok"] is True
    assert client.patch(f"/api/memory/{mid}", json={"text": "x"}).status_code == 404


def test_readonly_blocks_kb_writes(client, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "READONLY", True)
    assert client.post("/api/kb/reindex", json={}).status_code == 503
    assert client.post("/api/kb/ask", json={"question": "问点什么"}).status_code == 503
    assert client.post("/api/memory", json={"text": "记一条"}).status_code == 503
    assert client.delete("/api/memory/1").status_code == 503
    # 只读的照常可用（镜像上也要能查、能看实体）
    assert client.get("/api/kb/status").status_code == 200
    assert client.get("/api/kb/search?q=x").status_code == 200
    assert client.get("/api/memory").status_code == 200


def test_kb_ask_returns_sources_even_without_model(client, kb_env, monkeypatch):
    """模型调不通时也要把检索结果给出去 —— 有出处的原文比一句报错有用。"""
    _write_episode(kb_env, "20240109-癸", "# 主题\n\n" + "关于 AI 安全与对齐的讨论。" * 40)
    client.post("/api/kb/reindex", json={})
    for _ in range(40):
        if client.get("/api/kb/status").get_json()["indexing"]["state"] != "running":
            break
        time.sleep(0.2)

    from podcast_article import summarize
    monkeypatch.setattr(summarize, "_chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key")))
    d = client.post("/api/kb/ask", json={"question": "AI 安全", "k": 3}).get_json()
    assert d["sources"], "应该返回检索结果"
    assert d.get("error"), "并说明模型为什么没答"
