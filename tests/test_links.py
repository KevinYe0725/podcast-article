"""从粘贴文本里抠链接（podcast_article/links.py）。

真实场景：用户从 B 站 / 小宇宙 / YouTube 点「分享」，粘出来的是一整句文案 ——
标题、说明、序号、句末句号全都在，链接只是其中一段。以前只有「整行就是链接」
才认，于是这种粘贴会被判成「无法识别的链接」。
"""
from __future__ import annotations

from podcast_article import links


BILI = ("【赫拉利警示：AI正在悄然接管人类世界。】"
        "https://www.bilibili.com/video/BV1aphc6NEb6"
        "?vd_source=ef96ebb3b1001d1943c031ad435f1d46")


# ------------------------------------------------------------- 用户报的那一例


def test_extracts_link_after_bracketed_title():
    """【标题】https://… —— 标题里还带句号，链接要完整抠出来。"""
    assert links.first_link(BILI) == (
        "https://www.bilibili.com/video/BV1aphc6NEb6"
        "?vd_source=ef96ebb3b1001d1943c031ad435f1d46"
    )


def test_keeps_query_string_intact():
    url = links.first_link(BILI)
    assert "vd_source=ef96ebb3b1001d1943c031ad435f1d46" in url, "查询参数不能被截断"
    assert url.startswith("https://www.bilibili.com/video/BV1aphc6NEb6?")


def test_link_with_title_before_and_after():
    got = links.first_link("推荐这个 https://a.com/ep1 讲得挺好")
    assert got == "https://a.com/ep1", f"前后都有说明文字时也要抠干净：{got!r}"


# ------------------------------------------------------------------ 末尾标点


def test_strips_trailing_chinese_punctuation():
    assert links.first_link("看这个 https://a.com/1。 挺有意思") == "https://a.com/1"
    assert links.first_link("【标题】https://a.com/2）") == "https://a.com/2"
    assert links.first_link("（见 https://a.com/3，）") == "https://a.com/3"


def test_strips_trailing_ascii_punctuation():
    assert links.first_link("see https://a.com/4, then") == "https://a.com/4"
    assert links.first_link("https://a.com/5!") == "https://a.com/5"


def test_keeps_balanced_brackets_in_url():
    """维基那种 /wiki/Foo_(bar)：右括号是链接的一部分，不能剪掉。"""
    assert links.first_link("https://en.wikipedia.org/wiki/Foo_(bar)") == \
        "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert links.clean_url("https://a.com/wiki/Foo_(bar)。") == "https://a.com/wiki/Foo_(bar)"


def test_keeps_comma_inside_query():
    """`?ids=1,2` 里的逗号是链接的一部分（只有在逗号后面紧跟 http 时才当分隔符）。"""
    assert links.first_link("https://a.com/?ids=1,2,3&x=9") == "https://a.com/?ids=1,2,3&x=9"


# ------------------------------------------------------------------ 多条链接


def test_extracts_all_links_in_order():
    got = links.extract_links(f"{BILI}\n【另一期】https://a.com/2")
    assert got == [links.first_link(BILI), "https://a.com/2"]


def test_splits_glued_links():
    """一行里用逗号/顿号连着写好几条，也要拆开（旧行为，不能退化）。"""
    got = links.extract_links("https://a.com/1, https://a.com/2，https://a.com/3、https://a.com/4")
    assert got == [f"https://a.com/{n}" for n in range(1, 5)], f"连写的链接应拆开：{got}"


def test_dedupes_while_keeping_order():
    got = links.extract_links("https://a.com/1 和 https://a.com/2 再看 https://a.com/1")
    assert got == ["https://a.com/1", "https://a.com/2"], f"重复链接只留一次：{got}"


# ------------------------------------------------------------ 不该被当链接的


def test_ignores_non_http_urls():
    assert links.extract_links("ftp://a.com/x\nwww.a.com/2\n随便一句话") == []


def test_japanese_and_chinese_text_around_link():
    got = links.first_link("【ポッドキャスト】https://a.com/9 をどうぞ")
    assert got == "https://a.com/9"


# ------------------------------------------------------------------ 本地路径


def test_first_link_returns_text_when_no_link():
    """没有链接时原样返回：本地文件路径还要靠它交给 resolve 去判断。"""
    assert links.first_link("/Users/me/录音.m4a") == "/Users/me/录音.m4a"
    assert links.first_link("  ~/Downloads/某播客.mp3  ") == "~/Downloads/某播客.mp3"
    assert links.first_link("") == ""
    assert links.first_link(None) == ""


def test_clean_url_handles_empty():
    assert links.clean_url("") == "" and links.clean_url(None) == ""
    assert links.clean_url("。。。") == ""
