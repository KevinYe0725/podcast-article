"""MCP 工具层的测试：外部 AI 客户端能不能真的「搜书库 / 记住我 / 忘掉」。

这层最容易出的问题是**看起来注册了、实际用不了**：MCP 客户端只会看到工具的 docstring
和返回值，所以这里断言两件事 ——
1. 工具真的存在且能被 mcp 服务器列举出来（名字写错等于这个能力不存在）；
2. 调用后**返回的文字里带得出处 / 带 id**，否则模型拿到一句"已记住"却无法引用、无法删除。

不测 analyze_podcast（要跑完整条生成链路、要钱），它已有别的测试覆盖。
"""
from __future__ import annotations

import json

import pytest

mcp_mod = pytest.importorskip("mcp.server.fastmcp", reason="未安装 mcp 依赖")


@pytest.fixture()
def mcp_env(tmp_output, episode, monkeypatch):
    """把 MCP 工具的目录常量指到临时书库，并先把这一集索引好。"""
    from podcast_article import kb, mcp_server

    monkeypatch.setattr(mcp_server, "OUTPUT_ROOT", tmp_output)
    kb.index_dir(kb.ensure_schema(kb.connect()), episode)
    return mcp_server


async def _tool_names(server) -> set[str]:
    return {t.name for t in await server.list_tools()}


def test_kb_and_memory_tools_are_registered(mcp_env):
    """名字就是契约：改错一个字母，客户端里这个能力就消失了（而且不会报错）。"""
    import asyncio

    names = asyncio.run(_tool_names(mcp_env.mcp))
    assert {"search_library", "ask_library", "remember", "recall", "forget"} <= names
    assert {"analyze_podcast", "list_episodes", "get_article"} <= names, "老工具不能被弄丢"


def test_search_library_returns_provenance(mcp_env):
    """检索结果必须带「哪一集 / 小标题」，模型才能把话落回原处。"""
    out = mcp_env.search_library("旧金山地震", k=3)
    data = json.loads(out)
    assert data["hits"], out
    first = data["hits"][0]
    assert first["从哪一期"] == "测试单集"
    assert first["dir"] == "20240101-测试台-测试单集"


def test_search_library_says_so_when_nothing_matches(mcp_env):
    """没命中要给一句人话，而不是空对象让模型自己猜（它会开始编）。

    注意查询词要用**纯外文乱码**：中文假词会被 jieba 切成「完全」「存在」这种真词，
    FTS 是 OR 语义，照样命中 —— 那是检索正确、测试写错。
    """
    data = json.loads(mcp_env.search_library("qqqzzzxyzzy"))
    assert data["hits"] == [] and data["note"]


def test_remember_then_recall_roundtrip(mcp_env):
    """记住 → 取回：id 要回给模型，否则下次没法 forget。"""
    said = mcp_env.remember("我偏好看有具体数字的播客", kind="preference",
                            episode_dir="20240101-测试台-测试单集")
    assert "已记住" in said and "id=" in said

    items = json.loads(mcp_env.recall())
    assert [i["text"] for i in items] == ["我偏好看有具体数字的播客"]
    assert items[0]["kind"] == "preference"
    assert items[0]["来源"] == "20240101-测试台-测试单集"
    assert items[0]["用过次数"] == 0


def test_forget_removes_it(mcp_env):
    mcp_env.remember("这条待会儿要删掉")
    mid = json.loads(mcp_env.recall())[0]["id"]
    assert "已删除" in mcp_env.forget(mid)
    assert json.loads(mcp_env.recall())["items"] == []
    assert "没有" in mcp_env.forget(mid)


def test_remember_rejects_empty_text(mcp_env):
    """空内容要被挡住，不能存一条空记忆（存进去就永远躺在那里）。"""
    assert mcp_env.remember("   ").startswith("没存")


def test_recall_marks_never_used(mcp_env):
    """「哪些记忆从没用过」要能问出来 —— 这是他清理记忆的依据。"""
    from podcast_article import kb

    kb.memory_add("用过的一条", pinned=True)
    kb.memory_add("没用过的一条")
    kb.memory_for_prompt("随便问点什么")

    rows = {i["text"]: i for i in json.loads(mcp_env.recall(limit=50))}
    assert rows["用过的一条"]["用过次数"] >= 1
    assert rows["没用过的一条"]["用过次数"] == 0


def test_ask_library_uses_only_library_and_cites_sources(mcp_env, monkeypatch):
    """跨集问答要带出处、且把记忆作为背景注入（不是拿记忆冒充原文）。"""
    from podcast_article import summarize

    seen: dict = {}

    def fake_chat(messages, **kw):
        seen["messages"] = messages
        return "结论：地震发生在 1906 年 [1]。"

    monkeypatch.setattr(summarize, "_chat", fake_chat)
    mcp_env.remember("我在跟踪地震预警", kind="preference")
    out = mcp_env.ask_library("旧金山地震是什么时候")

    assert "[1]" in out and "依据" in out
    user = seen["messages"][-1]["content"]
    assert "我在跟踪地震预警" in user, "记忆没进 prompt"
    assert "1906" in user, "资料片段没进 prompt"
