"""「问你的库」的作答规则：一处定义、三处共用。

这个文件存在的直接原因是一个真实故障：`/api/kb/ask`、CLI `ask`、MCP `ask_library`
当初各自手拼模型调用，三处全传错形状（messages 数组当第一个参数、model=None），
一跑就 `TypeError: _chat() missing 2 required positional arguments`。
所以这里测的不只是措辞，而是**三个入口真的共用同一份实现** —— 以后再分家就会红。
"""
from __future__ import annotations

import pytest

from podcast_article import library_ask


def _hits():
    return [{"title": "测试单集", "podcast": "测试台", "heading": "第一节", "start_sec": 607,
             "doc_kind": "article", "text": "推理芯片的账要算每百万 token 成本。"},
            {"title": "测试单集", "podcast": "测试台", "heading": "", "start_sec": 120,
             "doc_kind": "transcript", "text": "这是文字稿里的一句话。"}]


def test_build_puts_provenance_and_memory_in_the_prompt():
    """片段要带出处（哪一集 · 小标题 · 时间戳），记忆要标明是读者的背景。"""
    _system, user = library_ask.build("成本怎么算", _hits(), ["我偏好看数字"])
    assert "[1] 测试单集（测试台） · 10:07 · 第一节" in user
    assert "[2] 测试单集（测试台） · 02:00 · 文字稿" in user
    assert "【关于这位读者的已知信息】\n- 我偏好看数字" in user
    assert "【资料片段】" in user


def test_no_memory_is_stated_not_left_blank():
    """没有记忆时要写明「（没有）」，不能留个空标题让模型自己猜。"""
    _system, user = library_ask.build("问题", _hits(), [])
    assert "【关于这位读者的已知信息】\n（没有）" in user


def test_system_forbids_inventorying_the_snippets():
    """核心规则：先给结论、不要逐条复述片段、不要点评片段本身。

    这条是实测出来的：早先要求「每条依据标编号」，模型就把 6 条片段挨个点评一遍
    （"只提到…但没有展开"），看着合规，实际什么也没回答。
    """
    s = library_ask.SYSTEM
    assert "先给结论" in s
    assert "不要逐条复述" in s
    assert "资料里没有" in s


def test_answer_delegates_to_ask_once_with_real_signature(monkeypatch):
    """作答必须走 summarize.ask_once（签名 (system, user)），别再手拼 messages。"""
    from podcast_article import summarize

    seen = {}

    def fake_ask_once(system, user, **kw):
        seen["system"], seen["user"] = system, user
        return "答案是 X"

    monkeypatch.setattr(summarize, "ask_once", fake_ask_once)
    assert library_ask.answer("问题", _hits(), ["记忆一条"]) == "答案是 X"
    assert seen["system"] == library_ask.SYSTEM
    assert "记忆一条" in seen["user"]


def test_all_three_entrypoints_share_one_implementation(client, monkeypatch, capsys):
    """Web / CLI / MCP 三个入口都调 library_ask.answer —— 一处改，三处生效。"""
    import json

    import webapp

    from podcast_article import kb, mcp_server

    kb.index_dir(kb.ensure_schema(kb.connect()),
                 webapp.OUTPUT_ROOT / "20240101-测试台-测试单集")

    calls: list[str] = []

    def fake_answer(question, hits, memories, **kw):
        calls.append(question)
        return "统一答案 [1]"

    monkeypatch.setattr(library_ask, "answer", fake_answer)

    # 1) Web
    r = client.post("/api/kb/ask", json={"question": "旧金山地震是什么时候"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "统一答案" in r.get_json()["answer"]

    # 2) CLI
    from podcast_article import cli
    assert cli.main(["ask", "旧金山地震是什么时候"]) == 0
    assert "统一答案" in capsys.readouterr().out

    # 3) MCP
    monkeypatch.setattr(mcp_server, "OUTPUT_ROOT", webapp.OUTPUT_ROOT)
    assert "统一答案" in mcp_server.ask_library("旧金山地震是什么时候")

    assert calls == ["旧金山地震是什么时候"] * 3, "三个入口应各调一次同一份实现"
    assert json.dumps(calls, ensure_ascii=False)      # 便于失败时看出差异
