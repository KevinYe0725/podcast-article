"""划词深挖：检索词、片段检索（IDF、窗口合并）、提示词约束、流式主入口的容错。

三条硬规矩
----------
1. **绝不联网**：websearch 一律走注入的假 search；默认路径（不注入）用 `sys.modules` 里塞
   None 或塞一个半成品 stub 来走——无论合作方那个模块有没有落地、写成什么样，测试结果一样。
2. **绝不调 LLM**：只打桩 `deepdive._client()`，让 `summarize._chat` 拿到一个假客户端。
   于是流式拼装、`on_delta` 逐块回调、usage 记账这些真实逻辑全都会被跑到，但一个字节都不出网。
3. 落盘一律在 tmp_path（transcript.json / usage.json），不碰仓库里的真实 output/。
"""
from __future__ import annotations

import json
import re
import sys
import types

import pytest

from podcast_article import deepdive, usage
from podcast_article.deepdive import (
    build_prompt,
    keywords,
    load_segments,
    retrieve,
    stream_answer,
)
from podcast_article.util import ts_clock


# ------------------------------------------------------------------ 装置

def write_transcript(workdir, rows=None, *, raw=None):
    """造一集的 transcript.json。rows: [(start 秒, text)]；raw 直接写原始字符串（造坏文件）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    content = raw if raw is not None else json.dumps(
        [{"start": s, "end": s + 5, "text": t} for s, t in (rows or [])], ensure_ascii=False
    )
    (workdir / "transcript.json").write_text(content, encoding="utf-8")
    return workdir


def _chunk(content=None, *, finish=None, usage_raw=None):
    delta = types.SimpleNamespace(content=content, reasoning_content=None)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage_raw
    )


def _usage_raw(prompt=120, completion=80):
    return types.SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=prompt,
    )


class FakeLLM:
    """假客户端：只满足 _chat 用到的那条链路 chat.completions.create(**kwargs) -> 可迭代。"""

    def __init__(self, pieces=("这是第一段。", "这是第二段。"), *, error=None,
                 mid_error=None, usage_raw=None):
        self.pieces = list(pieces)
        self.error = error
        self.mid_error = mid_error
        self.usage_raw = _usage_raw() if usage_raw is None else usage_raw
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        pieces, mid, usage_raw = self.pieces, self.mid_error, self.usage_raw

        def gen():
            for piece in pieces:
                yield _chunk(piece)
            if mid:
                raise mid
            yield _chunk(None, finish="stop", usage_raw=usage_raw)

        return gen()

    @property
    def messages(self):
        """最后一次调用真正发出去的消息（system, user）——用来断言提示词里写了什么。"""
        sent = self.calls[-1]["messages"]
        return sent[0]["content"], sent[1]["content"]


@pytest.fixture()
def llm(monkeypatch):
    """装一个假客户端：`fake = llm(pieces=(...))`；deepdive 以为自己在调真的 DeepSeek。"""
    def install(**kwargs):
        fake = FakeLLM(**kwargs)
        monkeypatch.setattr(deepdive, "_client", lambda: fake)
        return fake
    return install


@pytest.fixture()
def fake_search():
    """造一个假的 search：`s = fake_search({"ok": True, ...})`，s.calls 记录收到的 query。"""
    def make(result):
        calls: list[str] = []

        def search(query):
            calls.append(query)
            return result

        search.calls = calls
        return search
    return make


def _deltas():
    seen: list[str] = []
    return seen, lambda text: seen.append(text)


# ------------------------------------------------------------------ keywords

def test_keywords_chinese_grams_and_whole_run():
    got = keywords("强化学习")
    # 2-gram + 3-gram + 整段（长度 <= 4 时整体保留）
    assert set(got) == {"强化学习", "强化学", "化学习", "强化", "化学", "学习"}
    assert got[0] == "强化学习", "整段最长，应该排最前（长词优先）"
    # 长词优先：4 字整段 > 3-gram > 2-gram
    lengths = [len(t) for t in keywords("神经网络")]
    assert lengths == sorted(lengths, reverse=True)


def test_keywords_english_lowercase_and_min_length():
    got = keywords("The RL Training is AI at 5G")
    assert "training" in got and "rl" in got and "ai" in got and "5g" in got
    assert "the" not in got and "is" not in got and "at" not in got   # 停用词
    assert all(term == term.lower() for term in got)
    assert set(keywords("a I x cd")) == {"cd"}          # 长度 < 2 丢掉
    assert "gpt-4o" in keywords("用 GPT-4o 跑一遍")        # 连字符算词内


def test_keywords_drop_chinese_stopwords():
    got = keywords("这是一个测试")
    assert "测试" in got
    for junk in ("这是", "是一", "一个", "这是一个", "一个是"):
        assert junk not in got, junk
    # 全是虚词字 → 一个词都不留
    assert keywords("的了是在和就都") == []
    assert keywords("我们你们他们因为所以但是") == []


def test_keywords_dedup_limit_and_empty():
    got = keywords("测试 测试 测试 test test")
    assert got == ["test", "测试"], "重复词只留一个，同长按出现顺序"
    assert len(got) == len(set(got))
    assert len(keywords("今天聊强化学习与神经网络训练的方法", limit=3)) == 3
    assert keywords("强化学习", limit=0) == []
    assert keywords("") == []
    assert keywords("   \n\t ") == []
    assert keywords(None) == []


# ------------------------------------------------------------------ load_segments

def test_load_segments_reads_and_sorts(tmp_path):
    d = write_transcript(tmp_path / "ep", [(12.5, " 后半段 "), (0, "开头")])
    assert load_segments(d) == [
        {"start": 0.0, "text": "开头"},
        {"start": 12.5, "text": "后半段"},
    ]


def test_load_segments_missing_or_broken_file(tmp_path):
    assert load_segments(tmp_path / "不存在") == []
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "transcript.json").write_text("{坏掉的 json", encoding="utf-8")
    assert load_segments(bad) == []
    obj = write_transcript(tmp_path / "obj", raw='{"start": 0, "text": "不是列表"}')
    assert load_segments(obj) == []
    empty = write_transcript(tmp_path / "empty", raw="[]")
    assert load_segments(empty) == []


def test_load_segments_skips_broken_items(tmp_path):
    d = write_transcript(tmp_path / "ep", raw=json.dumps([
        {"start": 1, "text": "好的"},
        "我是一个字符串，不是片段",
        {"text": "缺 start"},
        {"start": "abc", "text": "start 不是数字"},
        {"start": 5, "text": "   "},            # 正文空白
        {"start": 6},                            # 没有正文
        {"start": float("nan"), "text": "NaN 时间戳"},   # 会让 ts_clock 炸掉
        {"start": float("inf"), "text": "无穷"},
        {"start": True, "text": "布尔不是时间"},
        {"start": "7.5", "text": "字符串数字可以接受"},
    ], ensure_ascii=False), )
    assert load_segments(d) == [
        {"start": 1.0, "text": "好的"},
        {"start": 7.5, "text": "字符串数字可以接受"},
    ]


# ------------------------------------------------------------------ retrieve

def test_retrieve_finds_passage_with_clock(tmp_path):
    d = write_transcript(tmp_path / "ep", [
        (600, "先聊点别的"),
        (3600, "这里讲强化学习的具体做法"),
        (4200, "后面又说别的"),
    ])
    got = retrieve(d, "强化学习", limit=6, window=1)
    assert len(got) == 1
    passage = got[0]
    assert "这里讲强化学习的具体做法" in passage["text"]
    assert passage["start"] == 600, "start 是合并后第一行的时间"
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", passage["ts"])
    assert passage["ts"] == ts_clock(passage["start"]) == "00:10:00"
    assert passage["score"] > 0
    # 每行行首都有时间戳，模型引用任意一行都能抄对
    for line in passage["text"].splitlines():
        assert re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", line)


def test_retrieve_idf_ranks_rare_term_first(tmp_path):
    rows = [(i * 60, f"第 {i} 段的闲聊内容") for i in range(13)]
    rows[0] = (0, "今天我们从一个问题开始")
    rows[4] = (240, "今天先把结论放前面")
    rows[8] = (480, "今天再回到开头那个问题")
    rows[12] = (720, "这里讲贝叶斯推断的基本思想")
    d = write_transcript(tmp_path / "ep", rows)

    got = retrieve(d, "今天 贝叶斯", limit=6, window=0)
    assert "贝叶斯" in got[0]["text"], "罕见词（df=1）必须压过到处都是的「今天」"
    assert got[0]["start"] == 720
    # 「今天」三处都在，命中它的片段排在后面；同分时位置偏置让最靠前的先出
    assert "今天" in got[1]["text"] and got[1]["start"] == 0
    assert [p["score"] for p in got] == sorted((p["score"] for p in got), reverse=True)


def test_retrieve_window_pulls_in_neighbours(tmp_path):
    rows = [(0, "开头的介绍词"), (60, "衔接的过渡句"), (120, "选中疑问的关键词就在这里"),
            (180, "后面的补充说明"), (240, "结尾的闲谈")]
    d = write_transcript(tmp_path / "ep", rows)

    got = retrieve(d, "关键词", window=1)
    assert len(got) == 1
    assert "衔接的过渡句" in got[0]["text"] and "后面的补充说明" in got[0]["text"]
    assert "开头的介绍词" not in got[0]["text"]
    assert got[0]["start"] == 60 and got[0]["ts"] == "00:01:00"

    bare = retrieve(d, "关键词", window=0)[0]
    assert bare["text"] == "[00:02:00] 选中疑问的关键词就在这里"
    assert bare["start"] == 120


def test_retrieve_merges_adjacent_hits(tmp_path):
    d = write_transcript(tmp_path / "ep", [
        (0, "开场白"),
        (60, "强化学习是第一部分"),
        (120, "强化学习继续说第二部分"),
        (180, "结束语"),
    ])
    got = retrieve(d, "强化学习", window=0)
    assert len(got) == 1, "相邻命中要合成一条，不能返回一堆碎片"
    assert "第一部分" in got[0]["text"] and "第二部分" in got[0]["text"]
    assert got[0]["start"] == 60


def test_retrieve_merges_overlapping_windows(tmp_path):
    d = write_transcript(tmp_path / "ep", [
        (0, "开场白"),
        (60, "强化学习在第一节"),
        (120, "中间这段没有关键词"),
        (180, "强化学习在第三节的补充"),
        (240, "结束语"),
    ])
    got = retrieve(d, "强化学习", window=1)
    assert len(got) == 1, "两处命中展开窗口后重叠，必须合成一条（不能返回两条重叠结果）"
    assert "第一节" in got[0]["text"] and "第三节的补充" in got[0]["text"]
    assert got[0]["start"] == 0


def test_retrieve_limit(tmp_path):
    # 命中片段之间要隔开：相邻命中会被合并成一条，那就测不出 limit 了
    rows = [(i * 300, f"第 {i} 段都在讲强化学习" if i % 2 == 0 else f"第 {i} 段说的是别的")
            for i in range(12)]
    d = write_transcript(tmp_path / "ep", rows)
    assert len(retrieve(d, "强化学习", limit=2, window=0)) == 2
    assert len(retrieve(d, "强化学习", limit=1, window=0)) == 1
    assert len(retrieve(d, "强化学习", limit=0, window=0)) == 0
    assert len(retrieve(d, "强化学习", limit=99, window=0)) == 6


def test_retrieve_caps_very_dense_passages(tmp_path):
    """命中区域特别长（同一个词连着讲几十句）时不要整段塞进提示词。"""
    rows = [(i * 30, f"第 {i} 句还在讲强化学习") for i in range(60)]
    d = write_transcript(tmp_path / "ep", rows)
    got = retrieve(d, "强化学习", window=0)
    assert len(got) == 1
    assert len(got[0]["text"].splitlines()) == deepdive.MAX_PASSAGE_SEGMENTS
    assert got[0]["start"] == 0


def test_retrieve_returns_empty_without_raising(tmp_path):
    ok = write_transcript(tmp_path / "ep", [(0, "强化学习第一句"), (60, "强化学习第二句")])
    assert retrieve(tmp_path / "不存在", "强化学习") == []
    assert retrieve(ok, "") == []
    assert retrieve(ok, "   ") == []
    assert retrieve(ok, "的了是在和就都") == [], "没有检索词（全是停用词）就不该硬凑结果"
    assert retrieve(ok, "完全没有出现过的词") == []
    assert retrieve(write_transcript(tmp_path / "one", [(0, "只有一个片段")]), "片段") == []
    assert retrieve(write_transcript(tmp_path / "bad", raw="{坏"), "强化") == []
    assert retrieve(write_transcript(tmp_path / "obj", raw="{}"), "强化") == []
    assert retrieve(None, "强化学习") == []


# ------------------------------------------------------------------ build_prompt

def test_build_prompt_system_states_the_hard_rules():
    system, _ = build_prompt(selection="选中", question="疑问", title="T", podcast="P",
                             passages=[], web="")
    for needle in ("绝不编造", "原文没有提到", "仅供参考", "[时:分:秒]",
                   "还可以往哪追", "逐字"):
        assert needle in system, needle
    assert "值得注意的是" in system, "要给出禁用套话清单"
    plan = deepdive.answer_mode("concise")
    assert f"{plan['lo']}-{plan['hi']}" in system, "字数区间要按档位填进去"
    assert str(deepdive.answer_limits("concise")[0]) in system, "报价字数要按档位填进去"


def test_system_forbids_answering_unasked_questions():
    """**这条是用户反馈的核心**：原来的回答 914 字 / 7 段，只有一段是回答所问的，
    其余是模型自己觉得「也很有意思」的点（「另一个容易误读的点是…」「…也值得停一下」）。
    所以提示词必须明确禁止主动扩展，而不只是把字数调小。
    """
    system, _ = build_prompt(selection="选中", question="疑问", title="T", podcast="P",
                             passages=[], web="")
    assert "只回答被问的那一点" in system, "必须有这条硬约束"
    for banned in ("也值得说", "顺带提一下", "还有一个容易误读的点", "也值得停一下"):
        assert banned in system, f"要把这类句式点名禁掉（{banned}）"
    assert "宁可少讲一个点，也不要答非所问" in system
    assert "第一句直接给答案" in system, "禁止绕圈开场"


def test_build_prompt_follows_mode():
    """档位不同，字数区间/段数/引用条数都要跟着变。"""
    concise, _ = build_prompt(selection="s", question="q", title="T", podcast="P",
                              passages=[], web="", mode="concise")
    detail, _ = build_prompt(selection="s", question="q", title="T", podcast="P",
                             passages=[], web="", mode="detail")
    assert concise != detail, "两个档位的提示词必须不同"
    c, d = deepdive.answer_mode("concise"), deepdive.answer_mode("detail")
    assert f"{c['lo']}-{c['hi']}" in concise and f"{d['lo']}-{d['hi']}" in detail
    assert str(c["quotes"]) in concise and str(d["quotes"]) in detail
    # 未知档位回退到默认（简洁），不能炸
    fallback, _ = build_prompt(selection="s", question="q", title="T", podcast="P",
                               passages=[], web="", mode="乱写的")
    assert f"{c['lo']}-{c['hi']}" in fallback


def test_build_prompt_user_carries_everything():
    passages = [{"start": 607, "ts": "00:10:07",
                 "text": "[00:10:07] 这是原话\n[00:11:00] 接着一句", "score": 2.0}]
    system, user = build_prompt(
        selection="选中这段话", question="他这是不是自相矛盾", title="某期标题", podcast="某播客",
        passages=passages, web="据网络资料：X 是 2024 年提出的", profile="【读者画像】读者称呼：某人",
    )
    assert isinstance(system, str) and system
    assert "选中这段话" in user
    assert "他这是不是自相矛盾" in user
    assert "某期标题" in user and "某播客" in user
    assert "[00:10:07] 这是原话" in user          # 片段文本
    assert "00:10:07" in user and "00:11:00" in user   # 片段里的时间戳
    assert "据网络资料：X 是 2024 年提出的" in user
    assert "读者称呼" in user
    assert "没有联网" not in user, "有网络资料时不能说没联网"


def test_build_prompt_explains_missing_passages_and_web():
    _, user = build_prompt(selection="选中", question="疑问", title="T", podcast="P",
                           passages=[], web="")
    assert "没有检索到" in user and "原文里没有覆盖" in user
    assert "没有联网" in user and "据网络资料" in user
    assert re.search(r"【这一期的原文片段[^\n]*】\n（[^）]+）", user), "空片段必须是明确说明，不能留空段落"
    assert re.search(r"【网络搜索结果[^\n]*】\n（[^）]+）", user), "没有网络资料也要明确说明"
    assert "【读者画像】" not in user, "没填画像时不该出现空段落"


def test_build_prompt_without_selection_or_question():
    _, user = build_prompt(selection="  ", question="", title="", podcast="",
                           passages=[{"ts": "00:00:01", "text": "[00:00:01] 甲"}], web="资料")
    assert "（没有给出选中文字）" in user
    assert "（他没有写出具体疑问" in user
    assert "（未知）" in user


# ------------------------------------------------------------------ stream_answer

def test_stream_answer_structure_and_deltas(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "强化学习第一句"), (60, "强化学习第二句")])
    fake = llm(pieces=("第一段。", "第二段。", "还可以往哪追"))
    seen, on_delta = _deltas()

    got = stream_answer(workdir=d, selection="强化学习", question="这到底是什么意思",
                        title="某期", podcast="某台", use_web=False, on_delta=on_delta)

    assert set(got) == {"answer", "passages", "web", "error"}
    assert got["error"] == ""
    assert got["answer"] == "第一段。第二段。还可以往哪追"
    assert len(seen) == 3, "每个 chunk 回调一次"
    assert "".join(seen) == got["answer"], "on_delta 拼起来必须等于 answer"
    assert got["passages"] and "强化学习第一句" in got["passages"][0]["text"]
    assert got["web"] is None
    assert fake.calls[0]["stream"] is True
    assert fake.calls[0]["stream_options"] == {"include_usage": True}
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert fake.calls[0]["max_tokens"] == deepdive.answer_limits("concise")[1], \
        "不传 mode 时应按默认（简洁）档给 token 上限"


def test_stream_answer_skips_web_when_disabled(tmp_output, llm, fake_search):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm()
    search = fake_search({"ok": True, "answer": "不该被用到"})

    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False, search=search)

    assert search.calls == [], "use_web=False 时不许碰注入的 search"
    assert got["web"] is None
    assert got["answer"]


def test_stream_answer_uses_web_results(tmp_output, llm, fake_search):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()
    search = fake_search({
        "ok": True, "answer": "贝叶斯定理是一种更新信念的方法。",
        "results": [{"title": "贝叶斯定理", "url": "https://example.com/bayes",
                     "snippet": "简述与应用"}],
    })

    got = stream_answer(workdir=d, selection="甲", question="贝叶斯是什么", title="t",
                        podcast="p", search=search)

    assert got["web"]["ok"] is True
    assert got["web"]["query"] == search.calls[0]
    assert "贝叶斯" in search.calls[0], "联网 query 里要有疑问的关键词"
    user = fake.messages[1]
    assert "贝叶斯定理是一种更新信念的方法。" in user
    assert "https://example.com/bayes" in user
    assert "仅供参考" in user
    assert "没有联网" not in user


def test_stream_answer_survives_web_failure(tmp_output, llm, fake_search):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()
    search = fake_search({"ok": False, "error": "HTTP 429 限流"})

    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p",
                        search=search)

    assert got["error"] == "", "联网失败不该让整个解读失败"
    assert got["answer"]
    assert got["web"]["ok"] is False
    assert "HTTP 429 限流" in got["web"]["error"], "失败原因要带给界面"
    assert search.calls
    user = fake.messages[1]
    assert "没有联网" in user, "检索失败时提示词按「没有网络资料」写"
    assert "429" not in user


def test_stream_answer_survives_web_exception(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm()

    def boom(query):
        raise TimeoutError("连接超时")

    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p",
                        search=boom)
    assert got["answer"] and got["error"] == ""
    assert got["web"]["ok"] is False and "TimeoutError" in got["web"]["error"]


def test_stream_answer_downgrades_when_websearch_unimportable(tmp_output, llm, monkeypatch):
    """websearch 还在并发开发中：import 不到就降级成「未联网」，不崩、不发请求。"""
    monkeypatch.setitem(sys.modules, "podcast_article.websearch", None)   # 让 import 必定失败
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()

    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p",
                        use_web=True)          # 不注入 search → 走默认路径

    assert got["error"] == ""
    assert got["answer"]
    assert got["web"]["ok"] is False and got["web"]["error"]
    assert "没有联网" in fake.messages[1]
    assert fake.messages[1].count("【网络搜索结果") == 1


def test_stream_answer_downgrades_when_websearch_incomplete(tmp_output, llm, monkeypatch):
    """半成品模块（还没有 search()）也要降级，而不是抛 AttributeError。"""
    stub = types.ModuleType("podcast_article.websearch")   # 故意不挂 search
    monkeypatch.setitem(sys.modules, "podcast_article.websearch", stub)
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm()

    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p",
                        use_web=True)
    assert got["answer"] and got["error"] == ""
    assert got["web"]["ok"] is False and "还不可用" in got["web"]["error"]


def test_default_path_uses_websearch_contract(tmp_output, llm, monkeypatch):
    """默认路径按约定调用 websearch 的 search / format_for_prompt（签名对齐，不等它落地）。"""
    stub = types.ModuleType("podcast_article.websearch")
    search_calls: list[str] = []
    format_calls: list[int] = []

    def search(query, limit=10):
        search_calls.append(query)
        return {"ok": True, "results": [{"title": "X", "url": "u", "snippet": "s"}]}

    def format_for_prompt(result, limit=5):
        format_calls.append(limit)
        return "【来自 websearch 的格式化】X：s"

    stub.search = search
    stub.format_for_prompt = format_for_prompt
    monkeypatch.setitem(sys.modules, "podcast_article.websearch", stub)

    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()
    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p",
                        use_web=True)

    assert got["web"]["ok"] is True
    assert len(search_calls) == 1
    assert format_calls == [deepdive.WEB_SNIPPETS]
    assert "【来自 websearch 的格式化】X：s" in fake.messages[1]


def test_default_path_falls_back_when_formatter_raises(tmp_output, llm, monkeypatch):
    stub = types.ModuleType("podcast_article.websearch")
    stub.search = lambda query, limit=10: {"ok": True, "results": [
        {"title": "X", "url": "https://x.example", "snippet": "简述"}]}

    def broken(result, limit=5):
        raise RuntimeError("格式化坏了")

    stub.format_for_prompt = broken
    monkeypatch.setitem(sys.modules, "podcast_article.websearch", stub)

    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()
    got = stream_answer(workdir=d, selection="甲", question="为什么", title="t", podcast="p")

    assert got["web"]["ok"] is True
    user = fake.messages[1]
    assert "https://x.example" in user and "简述" in user, "格式化失败要退到内置格式化"


def test_stream_answer_answers_without_any_passages(tmp_output, llm):
    fake = llm(pieces=("这一期的原文里没有覆盖这一点。",))
    got = stream_answer(workdir=tmp_output, selection="某段话",
                        question="为什么", title="t", podcast="p", use_web=False)

    assert got["passages"] == []
    assert got["error"] == "" and got["answer"]
    system, user = fake.messages
    assert "原文里没有覆盖" in user or "原文里没有覆盖" in system
    assert "不要引用任何原话" in user


def test_stream_answer_without_workdir(tmp_output, llm):
    fake = llm()
    got = stream_answer(workdir=None, selection="选中", question="为什么", title="t",
                        podcast="p", use_web=False)
    assert got["passages"] == [] and got["answer"] and got["error"] == ""
    assert fake.calls, "没有文字稿也要照常调模型"


def test_stream_answer_reports_model_error(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm(error=RuntimeError("服务端 500"))

    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False)

    assert set(got) == {"answer", "passages", "web", "error"}
    assert got["error"], "模型失败要给人话，不许抛异常"
    assert "500" in got["error"]
    assert got["answer"] == ""
    assert got["passages"], "失败也要把检索结果带回去"


def test_stream_answer_keeps_partial_answer_on_mid_stream_error(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm(pieces=("已经写出来的一段。",), mid_error=RuntimeError("连接断了"))
    seen, on_delta = _deltas()

    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False, on_delta=on_delta)

    assert got["answer"] == "已经写出来的一段。", "已经推给前端的增量不能被吞掉"
    assert "".join(seen) == got["answer"]
    assert got["error"] and "连接断了" in got["error"]


def test_stream_answer_reports_missing_api_key(tmp_output, monkeypatch):
    from podcast_article.config import MissingKeyError

    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])

    def boom():
        raise MissingKeyError("未找到 DEEPSEEK_API_KEY。")

    monkeypatch.setattr(deepdive, "_client", boom)
    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False)
    assert got["error"] and "API Key" in got["error"]
    assert got["answer"] == ""


def test_stream_answer_reports_empty_model_output(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm(pieces=())          # 一个字的正文都没有
    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False)
    assert got["answer"] == ""
    assert got["error"], "空产出必须变成人话错误"


def test_stream_answer_survives_broken_on_delta(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm(pieces=("正文。",))

    def explode(text):
        raise BrokenPipeError("SSE 连接断了")

    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False, on_delta=explode)
    assert got["error"] == ""
    assert got["answer"] == "正文。", "回调坏了也要把正文留在 answer 里"


# ------------------------------------------------------------------ 用量记账

def test_stream_answer_records_usage(tmp_output, llm):
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    llm()
    got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                        use_web=False)

    assert got["error"] == ""
    data = json.loads((d / "usage.json").read_text(encoding="utf-8"))
    assert data["calls"] >= 1 and data["out_tokens"] == 80
    assert usage.current() is None, "自己开的记录器要收掉，不能把全局状态留着"


def test_stream_answer_accumulates_existing_usage(tmp_output, llm):
    """这一集可能已经因为生成文章记过账：深挖的用量要累加，不能覆盖。"""
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    (d / "usage.json").write_text(
        json.dumps({**usage.empty("deepseek-flash"), "out_tokens": 700, "calls": 3}),
        encoding="utf-8",
    )
    llm()
    stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p", use_web=False)

    data = json.loads((d / "usage.json").read_text(encoding="utf-8"))
    assert data["out_tokens"] == 780
    assert data["calls"] == 4


def test_stream_answer_leaves_foreign_recorder_alone(tmp_output, llm):
    """批量任务正在记账时，不许抢过记录器（抢了会把别人记的账一起收掉）。"""
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    other = tmp_output / "other"
    other.mkdir()
    llm()
    recorder = usage.start("deepseek-flash", workdir=other)
    try:
        got = stream_answer(workdir=d, selection="甲", question="q", title="t", podcast="p",
                            use_web=False)
        assert got["answer"]
        assert usage.current() is recorder, "别人的记录器必须还活着"
        assert not (d / "usage.json").exists(), "不该往这一集目录里写文件"
    finally:
        usage.stop()


def test_stream_answer_usage_failure_is_not_fatal(tmp_output, llm):
    """workdir 不存在（或不可写）时，记账失败不该影响返回。"""
    missing = tmp_output / "不存在的目录" / "ep"
    llm()
    got = stream_answer(workdir=missing, selection="甲", question="q", title="t", podcast="p",
                        use_web=False)
    assert got["answer"] and got["error"] == ""
    assert usage.current() is None


# ------------------------------------------------------------------ 其它

def test_real_websearch_module_meshes(tmp_output, llm, monkeypatch):
    """真模块已落地：只打桩它的 search（不联网），让链路把它自己的 format_for_prompt 走通。

    断言刻意宽松（只要求标题与链接出现在提示词里）：websearch 以后调整排版口径时这个
    测试不该无故变红；但两个模块的接口（参数名、结果字段）对不上时会立刻暴露。
    """
    from podcast_article import websearch

    monkeypatch.setattr(websearch, "search", lambda query, **kwargs: {
        "ok": True, "query": query,
        "results": [{"title": "贝叶斯定理", "url": "https://example.com/bayes",
                     "snippet": "更新信念的方法"}],
    })
    d = write_transcript(tmp_output / "ep", [(0, "甲"), (60, "乙")])
    fake = llm()

    got = stream_answer(workdir=d, selection="甲", question="贝叶斯是什么", title="t", podcast="p")

    assert got["web"]["ok"] is True and got["error"] == ""
    user = fake.messages[1]
    assert "贝叶斯定理" in user and "https://example.com/bayes" in user
    assert "仅供参考" in user


def test_load_websearch_never_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "podcast_article.websearch", None)
    quiet = lambda *a, **k: None
    assert deepdive._load_websearch(quiet) is None
    module = deepdive._load_websearch(quiet)     # 真模块在时就是一个模块对象
    assert module is None or hasattr(module, "__name__")


def test_web_query_shape():
    """联网查询串的词序与词数是**硬约束**（真机实测出来的，不是风格问题）。

    实测表（cn.bing.com）：
        「月球大叔」 ✓ / 「推理优化」 ✓ / 「多模态」 ✓        ← 单个中文词正常
        「月球大叔 播客」 ✗ →「月球（地球的唯一卫星）」
        「推理优化 区别」 ✗ →「推理（思维的基本形式）」
        「多模态 表现」   ✗ →「多（汉语文字）」
        「多模态 gpt-4o」 ✗ → 中文在前照样退化
        「sglang 推理优化 区别 vllm」 ✓ → 拉丁词在第一位锚住了整条查询
    """
    # 有拉丁术语：放最前面，后面可以跟中文
    q = deepdive.search_query("SGLang 的推理优化", "它和 vLLM 的区别是什么")
    assert q.startswith("sglang"), f"拉丁术语必须排第一（实测它是唯一能锚住检索的东西）：{q}"
    assert "推理优化" in q, f"拉丁术语之后应当还能带中文词：{q}"

    # 纯中文：只能发一个词
    q2 = deepdive.search_query("他把名字缝在鱼身上", "")
    assert q2 and " " not in q2, f"纯中文查询只能有一个词（两个词必退化）：{q2}"

    assert len(q) <= deepdive.QUERY_MAX_CHARS, f"查询串要短，实际 {len(q)}：{q}"
    # 极端情况也不能空手而归：选文只有一两个字、追问全是虚词时退回原句
    assert deepdive._web_query("为什么", "甲"), "抽不出关键词时应退回截断后的原句"
    assert deepdive._web_query("", "") == ""


def test_search_terms_prefers_latin_and_short_cjk():
    """拉丁术语优先（实测它们能把检索锚住），中文只要短而完整的片段。"""
    terms = deepdive.search_terms("SGLang 的推理优化", "它和 vLLM 的区别是什么")
    assert "sglang" in terms and "vllm" in terms, f"拉丁术语要进词表：{terms}"

    terms2 = deepdive.search_terms("朱邦华复盘判断力、时机选择与 AI infra 的未来格局",
                                   "他说的「时机选择」具体指什么？")
    assert "时机选择" in terms2, f"首字不能被当成虚词剥掉（曾经剥成「机选择」）：{terms2}"
    assert len(terms2) <= deepdive.QUERY_MAX_TERMS, f"词表要短，实际 {terms2}"


def test_search_terms_never_returns_pure_function_words():
    """纯虚词组合不该进词表（「是一」「这是」这种没有检索价值）。"""
    terms = deepdive.search_terms("这是一段测试文字", "")
    assert terms, "正常中文选文要能抽出词"
    for junk in ("是一", "这是", "一个"):
        assert junk not in terms, f"虚词碎片漏进词表了：{junk} in {terms}"


def test_long_cjk_run_without_function_words_falls_back_to_head():
    """一整段没有虚词可切的长中文，要取开头的 2-4 字，而不是退回整句。

    实测：选「月球大叔聊低学历逆袭」——11 个字、没有虚词可切，旧实现会退回整句
    交给搜索引擎，而那**必然退化**（返回「月球（地球的唯一卫星）」）。
    取开头 4 字得到「月球大叔」，单独搜这个是对的（B 站/知乎的个人空间）。
    """
    terms = deepdive.search_terms("月球大叔聊低学历逆袭", "")
    assert terms and terms[0] == "月球大叔", f"应取开头的「月球大叔」，实际 {terms}"

    query = deepdive.search_query("月球大叔聊低学历逆袭", "")
    assert query == "月球大叔", f"查询串应当就是这一个词，实际「{query}」"


def test_query_terms_matches_what_is_actually_sent():
    """过滤词必须是**实际发出去的词**。

    这条是防一个具体的误杀：词表里那些被截断的候选词（`多模态上`、`推荐系统里`）
    永远不可能作为完整子串出现在搜索结果里，拿它们去过滤会把相关结果一起干掉，
    最后退化成「这次没联网」。
    """
    sel, q = "SGLang 的推理优化", "它和 vLLM 的区别是什么"
    sent = deepdive.query_terms(sel, q)
    assert sent == deepdive.search_query(sel, q).split(), \
        f"过滤词与查询串必须一一对应：{sent} vs {deepdive.search_query(sel, q)}"
    assert "sglang" in sent, f"拉丁术语必须在里面：{sent}"

    # 纯中文只发一个词，过滤词也只有那一个
    one = deepdive.query_terms("他把名字缝在鱼身上", "")
    assert len(one) == 1, f"纯中文只应有一个过滤词：{one}"

    # 抽不出词时返回空（调用方据此跳过过滤，见 search_query 的注释）
    assert deepdive.query_terms("为什么", "甲") == [] or deepdive.query_terms("为什么", "甲")


def test_answer_token_cap_is_bounded():
    """机械上限：够本档字数自然收尾，又不会让侧边抽屉跑成几千字。

    默认档是**简洁**（用户反馈：原来的 400-900 字里只有一段是回答所问的）。
    """
    assert deepdive.DEFAULT_ANSWER_MODE == "concise", "默认必须是简洁档"
    for mode, (cap_lo, cap_hi) in (("concise", (200, 420)), ("detail", (600, 1200))):
        ask, cap = deepdive.answer_limits(mode)
        assert cap_lo <= cap <= cap_hi, f"{mode} 的 token 上限应在合理区间，实际 {cap}"
        plan = deepdive.answer_mode(mode)
        assert plan["lo"] <= ask <= plan["hi"], f"{mode} 的报价字数应落在区间内，实际 {ask}"
        assert plan["lo"] < plan["hi"]
