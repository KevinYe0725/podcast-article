"""时间戳识别与换算（podcast_article/timestamps.py）。

真实问题：模型引用一段话时写成**时间区间** —— `[00:06:36-00:06:49]`。
只认单个 `[00:06:36]` 时，整篇都是区间的文章（实测《The Diary Of A CEO》那篇有 20+ 处）
一个可点的语音链接都没有。区间按起点跳转：引文就是从那一秒开始的。
"""
from __future__ import annotations

from podcast_article import timestamps as T


# ------------------------------------------------------------------ 换算


def test_to_seconds():
    assert T.to_seconds("00:06:36") == 396
    assert T.to_seconds("01:45:58") == 6358
    assert T.to_seconds("00:00:00") == 0


def test_two_part_is_minutes_and_seconds():
    """模型偶尔漏写小时（实测出现过 `[09:24]`）：按 分:秒 解释。"""
    assert T.to_seconds("09:24") == 564
    assert T.to_seconds("45:00") == 2700


# ------------------------------------------------------------------ 匹配


def test_single_timestamp():
    m = T.TS_RE.search("看这里 [00:10:07]。")
    assert m and m.group(1) == "00:10:07", "单个时间点要能匹配"
    assert m.group(2) is None, "单个时间点没有区间终点"


def test_range_timestamp_variants():
    """连字符、破折号、波浪号、「至 / 到」都要认（模型写法并不统一）。"""
    for text, start, end in [
        ("[00:06:36-00:06:49]", "00:06:36", "00:06:49"),
        ("[00:06:36–00:06:49]", "00:06:36", "00:06:49"),      # en dash
        ("[00:06:36—00:06:49]", "00:06:36", "00:06:49"),      # em dash
        ("[00:06:36~00:06:49]", "00:06:36", "00:06:49"),
        ("[00:06:36～00:06:49]", "00:06:36", "00:06:49"),      # 全角波浪号
        ("[00:06:36 至 00:06:49]", "00:06:36", "00:06:49"),
        ("[00:06:36到00:06:49]", "00:06:36", "00:06:49"),
        ("[00:06:36-00:06:49]", "00:06:36", "00:06:49"),
    ]:
        m = T.TS_RE.search(text)
        assert m, f"{text} 应该被识别成时间戳"
        assert (m.group(1), m.group(2)) == (start, end), f"{text} 解析成 {m.groups()}"


def test_range_with_short_second_part():
    """区间终点也允许是 分:秒 形态：`[09:24-10:02]`。"""
    m = T.TS_RE.search("[09:24-10:02]")
    assert m and m.group(1) == "09:24" and m.group(2) == "10:02"


def test_full_width_brackets():
    assert T.TS_RE.search("［00:01:00］"), "全角方括号也要认"
    assert T.TS_RE.search("【00:02:00】"), "中文方头括号也要认"


def test_does_not_match_plain_text():
    for text in ["第一节", "00:10:07 没有括号", "[见附录]", "【重点】", "[1]"]:
        assert not T.TS_RE.search(text), f"{text!r} 不该被当成时间戳"


# ------------------------------------------------------------------ 渲染


def test_linkify_single_uses_that_second():
    html = T.linkify("> 原话 [00:10:07]")
    assert '<span class="ts" data-sec="607"' in html
    assert "[00:10:07]</span>" in html, "显示的仍然是原文（含方括号）"
    assert 'role="button"' in html and 'tabindex="0"' in html, "要能键盘操作"


def test_linkify_range_jumps_to_start():
    html = T.linkify("> 一段话 [00:20:00-00:20:15]")
    assert 'data-sec="1200"' in html, "区间要跳到起点（20:00 = 1200 秒）"
    assert 'data-sec="1215"' not in html, "不能跳到区间终点"


def test_linkify_range_mentions_start_in_title():
    html = T.linkify("[00:20:00-00:20:15]")
    assert "00:20:00" in html.split('title="')[1].split('"')[0], "提示里要说清跳的是区间起点"


def test_linkify_leaves_other_text_alone():
    html = T.linkify("正文没有时间戳 [见附录] 也没有别的。")
    assert "span" not in html


def test_linkify_multiple_in_one_paragraph():
    html = T.linkify("先 [00:01:00] 再 [00:02:00-00:02:30]。")
    assert html.count('class="ts"') == 2
    assert 'data-sec="60"' in html and 'data-sec="120"' in html


# ------------------------------------------------------------------ 导出


def test_mark_wraps_without_playback():
    """导出的 HTML 单文件里没有播放器，只需要样式包一层。"""
    html = T.mark("> 原话 [00:20:00-00:20:15]")
    assert '<span class="ts">[00:20:00-00:20:15]</span>' in html
    assert "data-sec" not in html


# ------------------------------------------------------------------ 文字稿行首


def test_line_ts_matches_transcript_lines():
    assert T.LINE_TS_RE.match("[00:10:07] 这是一句").group(1) == "00:10:07"
    assert T.LINE_TS_RE.match("[00:20:00-00:20:15] 区间行").group(1) == "00:20:00"
    assert T.LINE_TS_RE.match("没有时间戳") is None


def test_only_ts_matches_standalone_lines():
    """整行只有时间戳（排版收尾要把这种孤儿行并回引文）。"""
    assert T.ONLY_TS_RE.match("[00:10:15]")
    assert T.ONLY_TS_RE.match("  [00:06:36-00:06:49]  ")
    assert T.ONLY_TS_RE.match("> 引文 [00:10:15]") is None, "带引文的行不算孤儿"
    assert T.ONLY_TS_RE.match("[00:10:15] 后面还有字") is None


# ------------------------------------------------------------------ 起点秒数


def test_start_of():
    assert T.start_of("> 引文 [00:06:36-00:06:49]") == 396
    assert T.start_of("没有任何时间戳") is None
