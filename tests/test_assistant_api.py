"""AI 阅读助手的接口层：/api/ask 流式、/api/qa、/api/search-service。

三层硬规矩：
1. **绝不联网**：搜索一律注入假的，`deepdive.stream_answer` 一律打桩；
2. **绝不真的调 LLM**；
3. 全部落在 tmp_path（conftest 的 `tmp_output` 已经把 output/ 与各 json 指到临时目录）。
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """把问答存储也指到临时目录（qa_store 无模块级路径，这里只需保证 output 隔离）。"""
    return tmp_path


def _wait_status(client, ask_id, timeout=5.0):
    """轮询 /api/ask/<id> 直到不再是 running。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/ask/{ask_id}").get_json()
        if last and last["status"] != "running":
            return last
        time.sleep(0.02)
    return last


def _fake_deepdive(monkeypatch, *, answer="这是解读正文。", error="", deltas=None,
                   passages=None, web=None, record=None):
    """把 deepdive 的两个入口换成假的：retrieve 立刻返回，stream_answer 逐段回调。"""
    from podcast_article import deepdive

    chunks = deltas if deltas is not None else ["这是", "解读正文。"]
    ret = passages if passages is not None else [{"ts": "00:10:07", "start": 607, "text": "原文片段"}]

    monkeypatch.setattr(deepdive, "retrieve", lambda *a, **k: ret)

    def fake_stream(**kw):
        if record is not None:
            record.update(kw)
        cb = kw.get("on_delta")
        for c in chunks:
            if cb:
                cb(c)
        return {"answer": answer, "passages": ret, "web": web, "error": error}

    monkeypatch.setattr(deepdive, "stream_answer", fake_stream)
    return deepdive


def _fake_search(monkeypatch, result=None):
    from podcast_article import websearch

    calls = []
    payload = result if result is not None else {
        "ok": True, "provider": "bing", "query": "x",
        "results": [{"title": "标题", "url": "https://a.example", "snippet": "摘要"}],
    }

    def fake(query, **kw):
        calls.append((query, kw))
        return payload

    monkeypatch.setattr(websearch, "search", fake)
    return calls


# ------------------------------------------------------------------ /api/ask


def test_ask_requires_selection_or_question(client):
    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集"})
    assert resp.status_code == 400, f"既没有选文也没有问题时应 400，实际 {resp.status_code}"
    assert "选中" in resp.get_json()["error"], f"错误信息要能指导用户，实际 {resp.get_json()}"


def test_ask_rejects_unknown_dir(client):
    resp = client.post("/api/ask", json={"dir": "../etc", "selection": "x"})
    assert resp.status_code == 404, f"目录不存在应 404，实际 {resp.status_code}"


def test_ask_streams_deltas_and_sources(client, monkeypatch):
    _fake_deepdive(monkeypatch)
    search_calls = _fake_search(monkeypatch)

    resp = client.post("/api/ask", json={
        "dir": "20240101-测试台-测试单集", "selection": "选中的一段话", "question": "这是什么",
    })
    assert resp.status_code == 200, resp.get_json()
    ask_id = resp.get_json()["id"]

    final = _wait_status(client, ask_id)
    assert final["status"] == "done", f"应正常结束，实际 {final}"
    assert final["answer"] == "这是解读正文。", f"答案不对：{final['answer']}"
    assert final["sources"]["passages"][0]["ts"] == "00:10:07", "依据里应带原文片段"
    assert final["sources"]["web"]["ok"] is True, "默认应联网"
    assert search_calls, "默认应调用搜索"

    # SSE 一定要能推出 delta（前端靠它做流式）
    stream = client.get(f"/api/ask/{ask_id}/stream")
    body = stream.get_data(as_text=True)
    assert "event: delta" in body, f"SSE 应推送 delta，实际：{body[:200]}"
    assert "event: done" in body, "SSE 结束时应有 done 事件"
    assert "event: sources" in body, "SSE 应先把依据推给界面（不用等写完）"


def test_ask_can_disable_web(client, monkeypatch):
    _fake_deepdive(monkeypatch)
    calls = _fake_search(monkeypatch)

    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集",
                                         "selection": "x", "web": False})
    ask_id = resp.get_json()["id"]
    final = _wait_status(client, ask_id)
    assert final["status"] == "done"
    assert calls == [], "web=False 时不该发起搜索"
    assert final["sources"]["web"] is None, "web=False 时不该有网络结果"


def test_ask_web_default_follows_settings(client, monkeypatch):
    """不传 web 时跟随设置里的 assistant.web_default。"""
    from podcast_article import settings as st

    _fake_deepdive(monkeypatch)
    calls = _fake_search(monkeypatch)
    st.save(assistant={"web_default": False})

    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集", "selection": "x"})
    assert resp.get_json()["web"] is False, "应跟随设置里的默认值"
    _wait_status(client, resp.get_json()["id"])
    assert calls == [], "设置关掉联网后不该搜索"


def test_ask_persists_to_qa_json(client, monkeypatch):
    _fake_deepdive(monkeypatch)
    _fake_search(monkeypatch)

    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集",
                                         "selection": "选中的话", "question": "为什么"})
    _wait_status(client, resp.get_json()["id"])

    listing = client.get("/api/qa?dir=20240101-测试台-测试单集").get_json()
    assert len(listing["items"]) == 1, f"问答应落盘，实际 {listing}"
    it = listing["items"][0]
    assert it["question"] == "为什么" and it["selection"] == "选中的话"
    assert it["answer"] == "这是解读正文。"


def test_ask_records_error_without_crashing(client, monkeypatch):
    _fake_deepdive(monkeypatch, answer="", error="模型额度不足", deltas=[])
    _fake_search(monkeypatch)

    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集", "selection": "x"})
    final = _wait_status(client, resp.get_json()["id"])
    assert final["status"] == "error", f"没有答案时应标成 error，实际 {final['status']}"
    assert "额度" in final["error"], f"要把原因带回来，实际 {final['error']}"
    assert client.get("/api/qa?dir=20240101-测试台-测试单集").get_json()["items"] == [], \
        "没有答案就不该留下空记录"


def test_ask_survives_deepdive_exception(client, monkeypatch):
    """deepdive 内部炸了也要变成一句话给用户，而不是 500。"""
    from podcast_article import deepdive

    monkeypatch.setattr(deepdive, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(deepdive, "stream_answer",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    _fake_search(monkeypatch)

    resp = client.post("/api/ask", json={"dir": "20240101-测试台-测试单集", "selection": "x"})
    assert resp.status_code == 200, "创建任务本身不该失败"
    final = _wait_status(client, resp.get_json()["id"])
    assert final["status"] == "error" and "boom" in final["error"], f"实际 {final}"


def test_ask_unknown_id_404(client):
    assert client.get("/api/ask/nope").status_code == 404


def test_ask_stream_unknown_id_reports_error(client):
    body = client.get("/api/ask/nope/stream").get_data(as_text=True)
    assert "event: error" in body, f"不存在的提问应推 error 事件，实际 {body}"


# ------------------------------------------------------------------ /api/qa


def test_qa_list_unknown_dir_404(client):
    assert client.get("/api/qa?dir=没有这个目录").status_code == 404


def test_qa_delete_and_clear(client, monkeypatch):
    from podcast_article import qa_store

    _fake_deepdive(monkeypatch)
    _fake_search(monkeypatch)
    d = "20240101-测试台-测试单集"

    ids = []
    for q in ("问题一", "问题二"):
        r = client.post("/api/ask", json={"dir": d, "selection": "x", "question": q})
        _wait_status(client, r.get_json()["id"])
        ids.append(client.get(f"/api/qa?dir={d}").get_json()["items"][0]["id"])

    resp = client.delete(f"/api/qa/{d}/{ids[0]}")
    assert resp.status_code == 200 and len(resp.get_json()["items"]) == 1, \
        f"删一条后应剩 1 条，实际 {resp.get_json()}"

    assert client.delete(f"/api/qa/{d}/不存在").status_code == 404
    cleared = client.delete(f"/api/qa/{d}")
    assert cleared.status_code == 200 and cleared.get_json()["removed"] == 1
    assert client.get(f"/api/qa?dir={d}").get_json()["items"] == []


def test_qa_file_is_readable_via_file_api(client, monkeypatch):
    """qa.json 要能通过 /api/file 读到（前端历史面板的兜底路径）。"""
    from podcast_article import qa_store

    workdir = client.application and None
    _fake_deepdive(monkeypatch)
    _fake_search(monkeypatch)
    d = "20240101-测试台-测试单集"
    r = client.post("/api/ask", json={"dir": d, "selection": "x"})
    _wait_status(client, r.get_json()["id"])

    resp = client.get(f"/api/file/{d}/qa.json")
    assert resp.status_code == 200, f"qa.json 应在文件白名单里，实际 {resp.status_code}"
    assert resp.get_json()["items"], "应能读到内容"


# ------------------------------------------------------ /api/search-service


def test_search_service_status(client, monkeypatch):
    from podcast_article import websearch

    monkeypatch.setattr(websearch, "available", lambda: {
        "providers": {"bing": {"label": "Bing", "needs_key": False, "configured": True}},
        "default": "bing", "enabled": True,
    })
    d = client.get("/api/search-service").get_json()
    assert d["default"] == "bing" and d["enabled"] is True, f"实际 {d}"
    assert d["providers"]["bing"]["needs_key"] is False


def test_search_service_test_endpoint(client, monkeypatch):
    _fake_search(monkeypatch)
    d = client.post("/api/search-service/test", json={"query": "DeepSeek"}).get_json()
    assert d["ok"] is True and d["results"][0]["title"] == "标题", f"实际 {d}"


def test_search_service_test_reports_failure_without_500(client, monkeypatch):
    _fake_search(monkeypatch, result={"ok": False, "provider": "bing", "error": "连不上",
                                      "results": []})
    resp = client.post("/api/search-service/test", json={})
    assert resp.status_code == 200, "搜索失败不该是 500"
    assert resp.get_json()["ok"] is False and "连不上" in resp.get_json()["error"]


# ------------------------------------------------------------------ 设置


def test_settings_exposes_assistant_defaults(client):
    d = client.get("/api/settings").get_json()
    assert "assistant" in d, "设置里应带上 assistant 段"
    assert set(d["assistant"]) == {"enabled", "web_default"}, f"实际 {d['assistant']}"


def test_settings_saves_assistant_section(client):
    resp = client.post("/api/settings", json={"assistant": {"enabled": False,
                                                            "web_default": False,
                                                            "乱写的键": 1}})
    assert resp.status_code == 200
    got = resp.get_json()["assistant"]
    assert got["enabled"] is False and got["web_default"] is False, f"实际 {got}"
    assert "乱写的键" not in got, "未知键应被忽略"
    assert client.get("/api/settings").get_json()["assistant"]["enabled"] is False
