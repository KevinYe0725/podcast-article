"""从「一坨粘贴文本」里抠出真正的链接。

分享按钮复制出来的内容几乎都带着说明文字：

    【赫拉利警示：AI正在悄然接管人类世界。】https://www.bilibili.com/video/BV1aphc6NEb6?vd_source=…

B 站 / YouTube / 小宇宙的分享文案、聊天记录里的「1. 标题 https://…」都是这个样子。
之前只有「整行就是链接（或本地路径）」才认，于是带标题粘一次就会被判成「无法识别的链接」。

这里只做一件事：把链接本身挑出来，并修掉粘在它末尾的标点。判断「这是什么平台的链接」
仍然交给 `sources.resolve`。
"""
from __future__ import annotations

import re

# 链接本体：从 http(s):// 开始，到第一个空白为止 —— 末尾的标点再单独修
_URL_RE = re.compile(r"https?://[^\s]+", re.I)

# 一行里连着写了好几个链接时的分隔符（`https://a.com/2，https://a.com/3`）。
# 只有分隔符后面紧跟 http(s):// 时才拆：链接里本来就可能有逗号（`?ids=1,2`）。
_GLUED_RE = re.compile(r"[\s,，、;；]+(?=https?://)", re.I)

# 粘在链接末尾、但不属于链接的字符（中英文标点、引号、句末句号）
_TAIL_JUNK = "。，、；：！？…,.;:!?\"'“”‘’"

# 成对的右括号：只有链接里没有对应的左括号时，它才算「粘上来的噪音」
_CLOSERS = {")": "(", "）": "（", "]": "[", "】": "【", "》": "《", "」": "「", "』": "『"}


def clean_url(url: str) -> str:
    """去掉粘在链接尾部的标点：`https://a.com/x。` → `https://a.com/x`。

    成对括号里的右括号要留着：`…/wiki/Foo_(bar)` 是链接的一部分。
    """
    s = (url or "").strip()
    while s:
        last = s[-1]
        opener = _CLOSERS.get(last)
        if opener:
            if s.count(opener) >= s.count(last):
                break          # 有配对的左括号，这个右括号属于链接
            s = s[:-1]
            continue
        if last in _TAIL_JUNK:
            s = s[:-1]
            continue
        break
    return s


def extract_links(text: str) -> list[str]:
    """抠出文本里所有 http(s) 链接：保持出现顺序，去重，末尾标点已修掉。"""
    out: list[str] = []
    for m in _URL_RE.finditer(str(text or "")):
        # 一行里用逗号/顿号/空格连着写好几个链接时，先拆开再逐个修末尾
        for piece in _GLUED_RE.split(m.group(0)):
            url = clean_url(piece)
            if url and url not in out:
                out.append(url)
    return out


def first_link(text: str) -> str:
    """「只跑一条」的场景用这个：抠出第一条链接；一条都没有就原样返回。

    没有链接时原样返回，是为了不破坏「本地文件路径」这条路 —— `/Users/me/x.m4a`
    不是链接，但它是合法输入，还要交给 `sources.resolve` 去判断。
    """
    found = extract_links(text)
    if found:
        return found[0]
    return str(text or "").strip()
