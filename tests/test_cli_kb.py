"""CLI 子命令：search / ask / remember / recall / forget / index / status。

命令行这层最容易坏在「参数解析」上，尤其这里有个历史包袱：`podcast-article <链接>`
是最老也最常用的用法，加子命令时**不能**把它变成必须写 `run`。所以测试里专门钉了
「第一个参数不是子命令时仍然当链接处理」。
"""
from __future__ import annotations

import pytest

from podcast_article import cli, kb


def _write_episode(root, name, article, transcript=None):
    import json
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "article.md").write_text(article, encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"title": "测试单集", "podcast": "测试台"},
                                            ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture()
def cli_env(tmp_output, monkeypatch):
    """把 CLI 的 output 根指到临时目录，并索引一集。"""
    # 正文长度必须超过切片下限（MIN_PASSAGE_CHARS），否则它压根不会被索引 ——
    # 这是规则不是 bug，但测试数据踩过：太短的文章搜不到任何东西。
    _write_episode(tmp_output, "20240101-测试台-测试单集",
                   "# 测试标题\n\n## 第一节\n\n"
                   "1906 年 4 月 18 日，旧金山发生里氏 7.9 级地震，此后城市重建持续了多年。\n",
                   transcript="[00:10:07] 这是原话，讲的是地震预警系统当年是怎么被提出来的。\n")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_output.parent)
    kb.index_all(tmp_output, embed=False, log=lambda *_: None)
    return tmp_output


def _out(capsys) -> str:
    return capsys.readouterr().out


def test_search_prints_provenance(cli_env, capsys):
    """命令行检索也要给出「哪一集 · 小标题 · 时间戳」，不然结果无法回溯。"""
    assert cli.main(["search", "旧金山地震"]) == 0
    out = _out(capsys)
    assert "测试单集" in out and "1906" in out


def test_search_tells_you_when_nothing_matches(cli_env, capsys):
    assert cli.main(["search", "qqqzzzxyzzy"]) == 1
    assert "没有命中" in _out(capsys)


def test_remember_recall_forget_roundtrip(cli_env, capsys):
    assert cli.main(["remember", "我偏好看有数字的播客", "--kind", "preference",
                     "--dir", "20240101-测试台-测试单集"]) == 0
    assert "已记住" in _out(capsys)

    assert cli.main(["recall"]) == 0
    out = _out(capsys)
    assert "我偏好看有数字的播客" in out and "从未用过" in out

    mid = kb.memory_list()[0]["id"]
    assert cli.main(["forget", str(mid)]) == 0
    assert "已删除" in _out(capsys)
    assert kb.memory_list() == []


def test_forget_unknown_id_exits_nonzero(cli_env, capsys):
    """删不存在的 id 要报错退出（脚本里靠退出码判断成败）。"""
    assert cli.main(["forget", "999"]) == 1
    assert "没有" in _out(capsys)


def test_recall_never_used_filter(cli_env, capsys):
    kb.memory_add("用过的", pinned=True)
    kb.memory_add("没用过的")
    kb.memory_for_prompt("随便问点什么")          # 置顶的算用上
    assert cli.main(["recall", "--never-used"]) == 0
    out = _out(capsys)
    # 注意别用 "用过的" not in out 这种子串断言 —— 它是 "没用过的" 的子串，永远为假
    assert "没用过的" in out and "★" not in out, "置顶的那条不该出现在「从未用过」里"


def test_remember_rejects_empty_text(cli_env, capsys):
    assert cli.main(["remember", "  "]) == 1
    assert "没存" in _out(capsys)


def test_status_prints_library_and_memory(cli_env, capsys):
    kb.memory_add("一条记忆")
    assert cli.main(["status"]) == 0
    out = _out(capsys)
    assert "书库" in out and "记忆：1 条" in out


def test_index_command_reports_counts(cli_env, capsys):
    assert cli.main(["index", "--no-embed"]) == 0
    assert "索引完成" in _out(capsys)


def test_ask_uses_library_and_cites(cli_env, monkeypatch, capsys):
    """ask 要给结论 + 依据编号；模型调用被打桩，不花钱。"""
    from podcast_article import summarize

    seen = {}

    # 打桩必须用 _chat 的**真实签名**（client, model, system, user）。之前这里写成
    # `fake_chat(messages, **kw)`，于是调用方把 messages 数组当第一个参数传、还漏了
    # system/user，测试照样通过 —— 真跑一次就 TypeError。宽松的打桩会吃掉这类错误。
    def fake_chat(client, model, system, user, **kw):
        seen["system"], seen["user"] = system, user
        return "结论：1906 年 [1]。"

    monkeypatch.setattr(summarize, "_client", lambda: object())
    monkeypatch.setattr(summarize, "_chat", fake_chat)
    kb.memory_add("我在跟踪地震预警", pinned=True)
    assert cli.main(["ask", "旧金山地震是什么时候"]) == 0
    out = _out(capsys)
    assert "1906" in out and "依据" in out
    assert "我在跟踪地震预警" in seen["user"] and "1906" in seen["user"]


def test_plain_url_still_runs_the_pipeline(tmp_output, monkeypatch, capsys):
    """老用法 `podcast-article <链接>` 不能被新子命令挤掉。"""
    called: dict = {}

    class FakePipe:
        def __init__(self, **kw):
            called.update(kw)

        def run(self):
            p = tmp_output / "20240101-测试台-测试单集"
            p.mkdir(parents=True, exist_ok=True)
            return p / "article.md"

    monkeypatch.setattr(cli, "Pipeline", FakePipe)
    assert cli.main(["https://example.com/ep1"]) == 0
    assert called["url"] == "https://example.com/ep1"
    assert "完成" in _out(capsys)


def test_cli_generation_also_indexes_the_new_article(tmp_output, monkeypatch, capsys):
    """命令行生成的也要进收集库 —— 否则「收集库全不全」取决于你用哪种方式生成。"""
    import json

    class FakePipe:
        def __init__(self, **kw):
            pass

        def run(self):
            p = tmp_output / "20240202-乙台-乙单集"
            p.mkdir(parents=True, exist_ok=True)
            (p / "article.md").write_text(
                "# 乙标题\n\n## 一节\n\n这段话足够长，应该被切成一个可检索的切片，讲的是记忆系统。\n",
                encoding="utf-8")
            (p / "meta.json").write_text(json.dumps({"title": "乙单集", "podcast": "乙台"},
                                                    ensure_ascii=False), encoding="utf-8")
            return p / "article.md"

    monkeypatch.setattr(cli, "Pipeline", FakePipe)
    assert cli.main(["https://example.com/ep2"]) == 0
    assert "已存入知识库" in _out(capsys)
    assert kb.search("记忆系统")["hits"], "命令行生成的这篇应该马上能搜到"
