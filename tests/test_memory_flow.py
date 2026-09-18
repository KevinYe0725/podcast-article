"""记忆「接通」到各个功能上的测试（不是记忆本身的增删改查，那些在 test_kb.py）。

这一层测的是**产品功能**：新文章有没有自动进收集库、单集助手有没有真的带上记忆、
搜索能不能一次把资料和记忆都搜出来。所以断言都落在「用户能感受到的结果」上：

    · 生成完一篇文章 → 不点任何按钮，知识库就能搜到它
    · 阅读页问一个问题 → 模型收到的 prompt 里真的有他存下的记忆，而且界面能看见是哪些
    · 搜索一个词 → 资料命中和「你记过的」一起回来

记忆注入有个必须守住的底线：记忆是**背景**，不能被当成这一期的原文。
测试里专门钉了那句话，因为它一旦丢，模型就会拿记忆冒充原文回答。
"""
from __future__ import annotations

import json
import time

import pytest


def _wait_ask(ask_id: str, timeout: float = 5.0) -> dict:
    """等后台线程把这次提问跑完（api_ask 是 worker 线程 + 轮询模型）。"""
    import webapp

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = webapp._ASKS.get(ask_id)
        if job and job.get("status") != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"提问 {ask_id} 在 {timeout}s 内没跑完：{webapp._ASKS.get(ask_id)}")


def _stub_ask(monkeypatch) -> dict:
    """把模型调用换成假的，抓住它实际收到的那份 prompt。"""
    from podcast_article import deepdive

    seen: dict = {}

    def fake_retrieve(workdir, query, limit=6):
        seen["retrieve_query"] = query
        return [{"text": "这是原话", "ts": "00:10:07"}]

    def fake_stream_answer(**kw):
        seen.update(kw)
        if kw.get("on_delta"):
            kw["on_delta"]("回答正文")
        return {"answer": "回答正文", "passages": kw.get("passages") or [], "web": None}

    monkeypatch.setattr(deepdive, "retrieve", fake_retrieve)
    monkeypatch.setattr(deepdive, "stream_answer", fake_stream_answer)
    return seen


def _ask(client, dir_name: str, **body):
    payload = {"dir": dir_name, "selection": "1906 年 4 月 18 日旧金山地震",
               "question": "这段到底在说什么", "web": False}
    payload.update(body)
    r = client.post("/api/ask", json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    return _wait_ask(r.get_json()["id"])


# ---------------------------------------------------------------- 自动入库

def test_done_indexes_new_article_without_any_click(client, monkeypatch):
    """文章生成完就自动进收集库 —— 用户不该为了「存进库」再点一次重建。"""
    import webapp

    from podcast_article import kb

    logs: list[str] = []
    webapp._after_done("20240101-测试台-测试单集", {}, logs.append)

    assert kb.stats()["passages"] > 0
    assert kb.search("旧金山 地震")["hits"], "自动入库后应该能搜到这篇"
    assert any("存入知识库" in line for line in logs), logs


def test_auto_index_failure_does_not_break_the_run(client, monkeypatch):
    """索引失败只留一行日志：文章已经生成好了，不能被锦上添花的事搞成失败。"""
    import webapp

    from podcast_article import kb

    def boom(*_a, **_k):
        raise RuntimeError("假设磁盘满了")

    monkeypatch.setattr(kb, "index_dir", boom)
    logs: list[str] = []
    webapp._after_done("20240101-测试台-测试单集", {}, logs.append)      # 不抛异常
    assert any("入库失败" in line for line in logs), logs


def test_auto_index_is_skipped_in_readonly(client, monkeypatch):
    """只读模式（算力在 Mac 上）不索引：那边没有这个文件，也不该写。"""
    import webapp

    from podcast_article import kb

    monkeypatch.setattr(webapp, "READONLY", True)
    called: list[str] = []
    monkeypatch.setattr(kb, "index_dir", lambda *a, **k: called.append("x") or {})
    webapp._after_done("20240101-测试台-测试单集", {}, lambda *_: None)
    assert called == []


# ---------------------------------------------------------------- 记忆进阅读助手

def test_reader_assistant_receives_memory(client, monkeypatch):
    """单集助手要知道「这位读者是谁」：置顶记忆必须出现在模型收到的 prompt 里。"""
    from podcast_article import kb

    kb.memory_add("我在跟踪地震预警系统的进展", kind="preference", pinned=True)
    seen = _stub_ask(monkeypatch)
    job = _ask(client, "20240101-测试台-测试单集")

    assert job["status"] == "done", job
    assert "地震预警" in seen.get("memory", ""), "记忆没进 prompt"
    assert job["sources"]["memory"] == ["我在跟踪地震预警系统的进展"], "界面要看得到用了哪几条"


def test_reader_assistant_will_not_use_memory_as_this_episode(client, monkeypatch):
    """记忆是背景，不能冒充这一期的内容 —— 这句护栏必须在 prompt 里。"""
    from podcast_article import deepdive

    kb_memory = "- 我在跟踪地震预警系统的进展"
    _system, user = deepdive.build_prompt(
        selection="选中了一段话", question="这是什么意思", title="测试单集", podcast="测试台",
        passages=[{"text": "原文片段", "ts": "00:00:10"}], web="", memory=kb_memory)

    assert kb_memory in user
    assert "不是这一期的内容" in user
    assert user.index("已知信息") < user.index("原文片段"), "背景要排在原文之前"


def test_no_memory_means_no_empty_block(client, monkeypatch):
    """没有记忆时不要塞一个空标题进去 —— 那只会让模型以为「读者什么都没记」。"""
    from podcast_article import deepdive

    _system, user = deepdive.build_prompt(
        selection="选中", question="问", title="t", podcast="p",
        passages=[], web="", memory="")
    assert "已知信息" not in user


def test_ask_still_works_when_memory_is_broken(client, monkeypatch):
    """记忆库坏了不能让提问失败：他是来问问题的，不是来修数据库的。"""
    from podcast_article import kb

    def boom(*_a, **_k):
        raise RuntimeError("库被锁住了")

    monkeypatch.setattr(kb, "memory_for_prompt", boom)
    _stub_ask(monkeypatch)
    job = _ask(client, "20240101-测试台-测试单集")
    assert job["status"] == "done" and job["answer"]
    assert job["sources"]["memory"] == []


# ---------------------------------------------------------------- 一个搜索框搜两样

def test_search_returns_materials_and_memory_together(client):
    """搜一个词，资料命中和「我记过的」一起回来，且带得出处。

    这个用例还是**向量缓存失效**的回归测试：先 index 再 search —— 只清 ids 不清 mat
    的老写法会在这里 500（`cache["ids"][idx]` 撞上 None），也就是「刚生成的文章一进库，
    跨集搜索就崩」。
    """
    import webapp

    from podcast_article import kb

    kb.index_dir(kb.ensure_schema(kb.connect()),
                 webapp.OUTPUT_ROOT / "20240101-测试台-测试单集")
    kb.memory_add("旧金山地震那次让我开始关注城市抗震", kind="insight", source_dir="20240101-测试台-测试单集")

    r = client.get("/api/kb/search?q=旧金山地震")
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["hits"], "资料命中丢了"
    assert [m["text"] for m in data["memory"]], "记忆命中丢了"
    assert data["memory"][0]["kind"] == "insight"
    assert data["memory"][0]["source_dir"] == "20240101-测试台-测试单集"


def test_search_memory_block_is_empty_not_missing_when_nothing_matches(client):
    """没命中也要返回空数组：前端靠它决定显示不显示那一块，缺字段会变成 undefined 崩掉。"""
    r = client.get("/api/kb/search?q=完全不相关的词xyz")
    assert r.status_code == 200
    assert r.get_json()["memory"] == []


# ---------------------------------------------------------------- 记忆腐化：看得见谁在用

def test_memory_usage_is_recorded(client):
    """被用过的记忆要留下次数与时间 —— 否则「哪条一直没在用」无从判断。"""
    from podcast_article import kb

    used = kb.memory_add("我关注算力成本", kind="preference", pinned=True)
    unused = kb.memory_add("这条谁也没用上", kind="fact")

    kb.memory_for_prompt("算力")
    after = {m["id"]: m for m in kb.memory_list()}
    assert after[used["id"]]["use_count"] >= 1
    assert after[used["id"]]["last_used_at"] > 0
    assert after[unused["id"]]["use_count"] == 0
    assert [m["id"] for m in kb.memory_never_used()] == [unused["id"]]


def test_old_memory_table_gains_new_columns_without_losing_rows(client, tmp_path, monkeypatch):
    """老库（没有 use_count 列）升级后要能自动补列，且一条记忆都不丢。"""
    import sqlite3 as _sqlite3

    from podcast_article import kb

    old = tmp_path / "old2.sqlite"
    conn = _sqlite3.connect(str(old))
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
            tags TEXT DEFAULT '', source_dir TEXT DEFAULT '', source_kind TEXT DEFAULT '',
            weight REAL DEFAULT 1.0, pinned INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0);
    """)
    conn.execute("INSERT INTO memory(kind, text, pinned, created_at, updated_at) "
                 "VALUES('fact','老库里的记忆',1,1,1)")
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','2')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(kb, "DB_PATH", old)
    items = kb.memory_list()
    assert [m["text"] for m in items] == ["老库里的记忆"]
    assert items[0]["use_count"] == 0 and items[0]["last_used_at"] == 0


# ---------------------------------------------------------------- 记忆不能被顺手清掉

def test_delete_memory_actually_works(client):
    """删掉就是删掉：列表里没了、搜索也搜不到了（只能加不能删 = 记忆会烂在库里）。"""
    from podcast_article import kb

    item = kb.memory_add("这条我反悔了，要删掉", kind="fact")
    assert kb.memory_list(query="反悔")

    r = client.delete(f"/api/memory/{item['id']}")
    assert r.status_code == 200, r.get_data(as_text=True)
    assert kb.memory_list() == []
    assert kb.memory_list(query="反悔") == [], "FTS 里还留着，等于删了又搜得到"


def test_updating_memory_reindexes_it(client):
    """改过之后要搜新说法能搜到、旧说法搜不到（否则等于记忆没更新）。"""
    from podcast_article import kb

    item = kb.memory_add("我关注英伟达的仓位变化", kind="fact")
    kb.memory_update(item["id"], text="我关注的是 Rubin 架构的交付节奏")
    assert kb.memory_list(query="Rubin")
    assert not kb.memory_list(query="英伟达"), "旧文本还留在索引里"


def test_memory_survives_schema_version_bump(client, monkeypatch):
    """升版本只重建派生表，**不能**动记忆 —— 记忆重建不回来。"""
    from podcast_article import kb

    kb.memory_add("升级也不能丢这条", kind="decision", pinned=True)
    monkeypatch.setattr(kb, "SCHEMA_VERSION", kb.SCHEMA_VERSION + 1)
    conn = kb.ensure_schema(kb.connect())                 # 触发一次「版本不一致 → 重建」
    conn.close()
    assert [m["text"] for m in kb.memory_list()] == ["升级也不能丢这条"]


def test_contentless_fts_from_old_db_is_repaired(client, tmp_path, monkeypatch):
    """老库（contentless FTS）要能就地修好：删得掉、搜得到。

    这是实测踩到的线上故障：真实 kb.sqlite 里两个 FTS 表都是 `content=''` 建的，
    于是「删记忆」直接 500、改过的文章重建索引也报错。修法不是让用户删库重建
    （那就把记忆一起删了），而是检测到就换成普通 FTS5 并从原文表重灌 tokens。
    """
    import sqlite3 as _sqlite3

    from podcast_article import kb

    old = tmp_path / "old.sqlite"
    conn = _sqlite3.connect(str(old))
    conn.executescript(kb._SCHEMA)                        # 先按现在的定义建好
    for name in ("passages_fts", "memory_fts"):           # 再伪装成老版本的 contentless
        conn.execute(f"DROP TABLE {name}")
        conn.execute(f"CREATE VIRTUAL TABLE {name} USING fts5(tokens, content='', "
                     "tokenize='unicode61')")
    conn.execute("INSERT INTO memory(kind, text, created_at, updated_at) "
                 "VALUES('fact','老库里的记忆',0,0)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(kb, "DB_PATH", old)
    kb.memory_add("新加的一条")                            # 走一遍 ensure_schema
    conn = kb.ensure_schema(kb.connect())
    assert not kb._fts_is_contentless(conn, "memory_fts")
    assert not kb._fts_is_contentless(conn, "passages_fts")
    conn.close()
    assert kb.memory_list(query="老库里") != [], "修 FTS 不能把记忆弄丢"

    item = [m for m in kb.memory_list() if m["text"] == "新加的一条"][0]
    assert kb.memory_delete(item["id"]) is True           # 老库现在删得掉了

def test_ui_can_save_selection_with_provenance(client):
    """「记住这条」走的就是这个接口：带上出自哪一集，否则以后没法回溯。"""
    r = client.post("/api/memory", json={"text": "选中里的这句话值得记",
                                         "kind": "insight", "dir": "20240101-测试台-测试单集",
                                         "source": "reader"})
    assert r.status_code == 200
    item = r.get_json()
    assert item["text"] == "选中里的这句话值得记"
    assert item["source_dir"] == "20240101-测试台-测试单集"
    assert item["source_kind"] == "reader"

    r = client.get("/api/memory")
    assert [m["id"] for m in r.get_json()["items"]] == [item["id"]]


def test_memory_survives_schema_ensure(client):
    """记忆不能被「重建索引」之类的动作顺手清掉（这正是它和 kb 索引的区别）。"""
    import webapp

    from podcast_article import kb

    kb.memory_add("这条必须留下来", kind="decision", pinned=True)
    kb.reindex(webapp.OUTPUT_ROOT, force=True)
    assert [m["text"] for m in kb.memory_list()] == ["这条必须留下来"]


@pytest.mark.parametrize("kind", ["preference", "fact", "entity", "decision", "insight"])
def test_all_memory_kinds_are_accepted_by_the_ui_endpoint(client, kind):
    r = client.post("/api/memory", json={"text": f"{kind} 类的一条", "kind": kind})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["kind"] == kind


def test_empty_memory_text_is_rejected(client):
    r = client.post("/api/memory", json={"text": "   "})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_memory_roundtrip_through_json_is_stable(client):
    """记忆是可导出的个人资产：字段名不能在序列化里变形。"""
    item = client.post("/api/memory", json={"text": "要能原样拿回来", "kind": "fact",
                                            "tags": "测试,导出", "pinned": True}).get_json()
    again = [m for m in client.get("/api/memory").get_json()["items"] if m["id"] == item["id"]][0]
    assert json.dumps(again, ensure_ascii=False, sort_keys=True) == \
        json.dumps(item, ensure_ascii=False, sort_keys=True)
    assert again["tags"] == "测试,导出" and bool(again["pinned"]) is True


# ---------------------------------------------------------------- 问答入口的调用姿势

def test_ask_once_calls_chat_with_the_real_signature(client, monkeypatch):
    """`_chat` 的签名是 (client, model, system, user, ...)，不是 messages 数组。

    这条测试是为一个真实故障写的：Web「问你的库」、CLI `ask`、MCP `ask_library`
    三处当初各自手拼调用，全写成「传一个 messages 数组 + model=None」，一跑就
    `TypeError: _chat() missing 2 required positional arguments: 'system' and 'user'`。
    之所以长期没被发现，是因为测试把这些调用点全都打了桩，而打桩函数签名宽松。
    现在三个入口都走 `summarize.ask_once`，这里用**真实签名**打桩把它钉死。
    """
    from podcast_article import summarize

    seen = {}

    def fake_chat(client, model, system, user, **kw):
        seen.update({"client": client, "model": model, "system": system, "user": user, **kw})
        return "答案"

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    monkeypatch.setattr(summarize, "_client", lambda: "假客户端")
    assert summarize.ask_once("系统提示", "用户问题") == "答案"

    assert seen["client"] == "假客户端"
    assert seen["system"] == "系统提示" and seen["user"] == "用户问题"
    assert isinstance(seen["model"], str) and seen["model"], "模型名必须落到具体名字，不能是 None"
    assert seen["max_tokens"] and seen["temperature"] is not None


def test_kb_ask_endpoint_reaches_the_model(client, monkeypatch):
    """跨集问答这条 HTTP 路径要真的调到模型（而不是在拼参数时先崩掉）。"""
    import webapp

    from podcast_article import kb, summarize

    kb.index_dir(kb.ensure_schema(kb.connect()),
                 webapp.OUTPUT_ROOT / "20240101-测试台-测试单集")   # 没索引就没得检索
    kb.memory_add("我在跟踪地震预警", kind="preference", pinned=True)

    def fake_chat(_client, _model, system, user, **kw):
        assert "资料片段" in user and "地震" in user
        return "结论：资料里说是 1906 年 [1]。"

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    monkeypatch.setattr(summarize, "_client", lambda: None)

    r = client.post("/api/kb/ask", json={"question": "旧金山地震是什么时候"})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert not d.get("error"), f"不该有错误：{d.get('error')}"
    assert "1906" in d["answer"]
    assert d["sources"], "回答要带出处"
    assert d["memory_used"] == ["我在跟踪地震预警"]
