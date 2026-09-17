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
        "# 标题\n\n" + "改过的正文，里面有个独特词 zebracorn，旧标记 oldmarker 已删除。" * 60,
        encoding="utf-8")
    kb.index_all(kb_env, embed=False)
    assert kb.search("zebracorn", k=3)["hits"], "改过的内容应该被重新索引"
    # 用一个不会与其它词部分重合的假词判断"旧内容被换掉"（FTS 是 OR 语义，
    # 拿中文短语去查会撞上仍然存在的其它词）
    assert not kb.search("oldmarker", k=3)["hits"]
    assert kb.search("zebracorn", k=3)["hits"][0]["dir"] == "20240102-乙"


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
