"""阅读助手问答记录的存储层（qa_store）。

全部落在 tmp_path，绝不碰真实 output/。
"""
from __future__ import annotations

import json

import pytest

from podcast_article import qa_store


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "20240101-测试台-测试单集"
    d.mkdir()
    return d


def _add(workdir, **kw):
    base = {"selection": "选中的原文", "question": "这是什么意思", "answer": "解读正文"}
    base.update(kw)
    return qa_store.append(workdir, **base)


def test_append_creates_file_and_returns_record(workdir):
    rec = _add(workdir)
    assert rec["id"].startswith("a") and len(rec["id"]) == 9, f"id 形态不对：{rec['id']}"
    assert rec["at"] > 0, "应记录时间戳"
    assert (workdir / "qa.json").exists(), "应落到这一集的 qa.json"

    saved = json.loads((workdir / "qa.json").read_text(encoding="utf-8"))
    assert len(saved["items"]) == 1, f"文件里应有 1 条，实际 {saved}"
    assert saved["items"][0]["answer"] == "解读正文"


def test_load_returns_newest_first(workdir):
    _add(workdir, answer="第一条")
    _add(workdir, answer="第二条")
    _add(workdir, answer="第三条")

    items = qa_store.load(workdir)
    assert [i["answer"] for i in items] == ["第三条", "第二条", "第一条"], \
        f"应按时间倒序（最新在前），实际 {[i['answer'] for i in items]}"


def test_load_tolerates_missing_and_broken_files(workdir):
    assert qa_store.load(workdir) == [], "文件不存在应返回 []"

    (workdir / "qa.json").write_text("{不是合法 json", encoding="utf-8")
    assert qa_store.load(workdir) == [], "坏 JSON 应返回 []"

    (workdir / "qa.json").write_text(json.dumps(["这不是对象"]), encoding="utf-8")
    assert qa_store.load(workdir) == [], "顶层不是对象应返回 []"

    (workdir / "qa.json").write_text(json.dumps({"items": "不是列表"}), encoding="utf-8")
    assert qa_store.load(workdir) == [], "items 不是列表应返回 []"

    (workdir / "qa.json").write_text(json.dumps({"items": ["字符串", 42, None]}), encoding="utf-8")
    assert qa_store.load(workdir) == [], "列表里的非字典项应被丢掉"


def test_capacity_keeps_latest(workdir, monkeypatch):
    monkeypatch.setattr(qa_store, "MAX_ITEMS", 3)
    for i in range(5):
        _add(workdir, answer=f"第 {i} 条")

    items = qa_store.load(workdir)
    assert len(items) == 3, f"最多留 MAX_ITEMS 条，实际 {len(items)}"
    assert [i["answer"] for i in items] == ["第 4 条", "第 3 条", "第 2 条"], \
        f"应保留最新的 3 条，实际 {[i['answer'] for i in items]}"


def test_long_fields_are_truncated(workdir):
    rec = _add(workdir,
               selection="选" * 5000, question="问" * 2000, answer="答" * 30000)
    assert len(rec["selection"]) == qa_store.MAX_SELECTION
    assert len(rec["question"]) == qa_store.MAX_QUESTION
    assert len(rec["answer"]) == qa_store.MAX_ANSWER
    # 落盘后读回来也必须是被截断的（不然文件还是会被撑大）
    assert len(qa_store.load(workdir)[0]["answer"]) == qa_store.MAX_ANSWER


def test_passages_are_slimmed(workdir):
    rec = _add(workdir, passages=[
        {"ts": "00:10:07", "start": 607, "text": "原文" * 1000, "score": 9.9},
        "不是字典应被丢掉",
        {"ts": None, "text": "没有时间戳的片段"},
    ])
    assert len(rec["passages"]) == 2, f"非字典项应被丢掉，实际 {rec['passages']}"
    assert rec["passages"][0]["ts"] == "00:10:07" and rec["passages"][0]["start"] == 607
    assert len(rec["passages"][0]["text"]) == 800, "片段文本应截断到 800 字"
    assert "score" not in rec["passages"][0], "内部打分不该写进存储"


def test_web_results_are_slimmed(workdir):
    rec = _add(workdir, web={
        "ok": True, "provider": "bing", "query": "不该存的字段",
        "results": [{"title": "标题", "url": "https://a.example", "snippet": "摘要",
                     "raw_html": "<div>不该存</div>"}],
    })
    web = rec["web"]
    assert web["ok"] is True and web["provider"] == "bing"
    assert "query" not in web, "只留展示需要的字段"
    assert "raw_html" not in web["results"][0], f"结果里不该带原始 HTML：{web['results'][0]}"


def test_web_none_and_ok_false(workdir):
    assert _add(workdir, web=None)["web"] is None
    rec = _add(workdir, web={"ok": False, "error": "连不上"})
    assert rec["web"]["ok"] is False and rec["web"]["error"] == "连不上"
    assert rec["web"]["results"] == [], "失败时结果应为空列表"


def test_remove_and_clear(workdir):
    a = _add(workdir, answer="A")
    _add(workdir, answer="B")

    assert qa_store.remove(workdir, a["id"]) is True, "删除已存在的记录应返回 True"
    assert [i["answer"] for i in qa_store.load(workdir)] == ["B"], "只应删掉指定的那条"
    assert qa_store.remove(workdir, "不存在") is False, "删除不存在的记录应返回 False"

    assert qa_store.clear(workdir) == 1, "clear 应返回删掉的条数"
    assert qa_store.load(workdir) == []
    assert qa_store.clear(workdir) == 0, "已经空了再 clear 应返回 0"


def test_count(workdir):
    assert qa_store.count(workdir) == 0
    _add(workdir)
    _add(workdir)
    assert qa_store.count(workdir) == 2


def test_write_is_atomic_no_tmp_left(workdir):
    _add(workdir)
    leftover = list(workdir.glob("*.tmp"))
    assert leftover == [], f"原子写不该留下临时文件：{leftover}"


def test_empty_question_or_selection_is_allowed(workdir):
    """只有选文没提问、或只提问没选文，都应该能存下来。"""
    assert _add(workdir, question="")["question"] == ""
    assert _add(workdir, selection="")["selection"] == ""
