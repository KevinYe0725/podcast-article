"""AI 助手的联网搜索（自己实现，不依赖模型的 built-in web_search）。

    from podcast_article import websearch
    r = websearch.search("DeepSeek V4 发布", limit=6)
    prompt_block = websearch.format_for_prompt(r)

为什么要自己实现
----------------
DeepSeek 官方文档明确把 `web_search` 列为「忽略」的内置工具（API 不提供），所以
联网这件事只能由本模块来做：抓搜索结果页 → 解析成 [{title, url, snippet}] → 拼成
给模型看的文本块。调用方是**流式回答**，所以本模块对外**绝不抛异常**：任何失败都
写进返回值的 `error` 里，让助手能照常回一句「本次没搜到」，而不是整个请求崩掉。

在这台机器上的实测结论（不要再重复试错）
----------------------------------------
- Bing **RSS 出口** `https://www.bing.com/search?q=…&format=rss&count=8`：200 / `text/xml` /
  约 4KB / 10 条，`<item>` 给的是最终 URL 和干净文本 → **主路径**（见 `_parse_bing_rss`）。
- Bing **HTML 页** `https://cn.bing.com/search?q=…`：200 / 约 98KB / 10 条，
  能解析但标题带 `<strong>` 高亮碎片、URL 偶尔是 `ck/a?u=a1…` 跳转链 → **兜底**。
  注意：给 Bing 加 `setlang`/`mkt` 会把中文长尾查询的结果整体打歪（实测
  `SGLang 朱邦华` → 摩托车车行、`月球大叔 播客` → Google Docs），所以不加。
- `https://search.brave.com/search?q=…&source=web`：**可用但不稳** —— 200 时约
  180~232KB、20 个结果块能正常解析；连续请求会 429（实测同一查询两次请求一次成功
  一次 429），偶尔还会返回一堵 consent/JS 墙（200 但没有结果块）→ 第二顺位兜底。
- `https://html.duckduckgo.com/html/`、`https://lite.duckduckgo.com/lite/`：403 被挡。
- `https://searx.be`、`https://www.mojeek.com`：JS 挑战 / Captcha。
- Tavily / Serper：root 可达但要用户自己的 key，做成可选 provider。

**Bing 对中文长尾专名（人名、栏目名）索引很差**：`朱邦华` 会退化成「朱」、`月球大叔`
会退化成「月球」，返回一堆百科条目。这类结果交给模型比不联网更糟，所以 `search()`
支持调用方传 `terms` 做相关性过滤（只认完整检索词，不拆 2-gram，见 `_matches`）。

设计取舍
--------
- **只用 requests + re**（已是声明依赖；仓库里没装 BeautifulSoup/lxml，也不该为一个
  解析器加依赖）。两个搜索页都是机器生成的规整 HTML，`re` 足够；真正需要抗的是
  「改版」而不是「畸形 HTML」，所以解析全部按**分块定位 + 块内多路回退**来写。
- 超时拆成 (连接 8s, 读取 timeout)：连接超时短一点，读取给调用方的 timeout，
  避免某个半死不活的站点把助手的流式回答整段挂住。
- 同一 query 10 分钟内复用缓存（模块级 dict + 锁）：助手经常把同一个问题拆成
  两三次查询，重复打搜索站不仅慢，还容易被风控（Brave 已经会回 429）。
- 日志里不打印 key；失败原因只写进返回值。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import html as html_lib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

import requests

# --------------------------------------------------------------- 常量

#: 免 key 的（抓网页）在前，需要 key 的在后；决定自动降级顺序：bing → brave。
PROVIDERS: tuple[str, ...] = ("bing", "brave", "tavily", "serper")

#: 设置页要展示/可写入的 .env 键，webapp 直接复用这个元组。
SEARCH_ENV: tuple[str, ...] = ("PA_SEARCH", "PA_SEARCH_PROVIDER", "TAVILY_API_KEY", "SERPER_API_KEY")

#: 需要用户自己配 key 的 provider
KEYED: tuple[str, ...] = ("tavily", "serper")

#: 免 key provider 的固定降级顺序
KEYLESS: tuple[str, ...] = ("bing", "brave")

LABELS: dict[str, str] = {
    "bing": "Bing（免 key）",
    "brave": "Brave（免 key）",
    "tavily": "Tavily（需 key）",
    "serper": "Serper（需 key）",
}

#: 每个 key 型 provider 对应的环境变量
KEY_ENV: dict[str, str] = {"tavily": "TAVILY_API_KEY", "serper": "SERPER_API_KEY"}

#: 用户配了 key 时的优先级（越靠前越优先）
KEYED_ORDER: tuple[str, ...] = ("tavily", "serper")

#: 关掉联网时的默认值（enabled=False 时 default 字段用它，界面显示稳定）
FALLBACK_PROVIDER = "bing"

CACHE_TTL = 600.0          # 同一 query 的缓存有效期（秒）
CONNECT_TIMEOUT = 8.0      # 连接超时（秒）
DEFAULT_TIMEOUT = 15.0     # 读取超时（秒）
_MIN_TITLE_LEN = 4         # 短于这个长度的标题基本是「视频」这类分组标签，要换一个
# 聚合结果的分组标签（不是结果标题）：实测 Brave 的视频块会先给一个 <h4>视频</h4>
_GROUP_LABEL_RE = re.compile(r"^(?:视频|图片|网页|新闻|购物|讨论|帖子|问答|地点|地图|航班)[:：]?$")

# 真实浏览器 UA + Accept-Language：Bing/Brave 对默认 UA（python-requests）会降级或挡掉，
# 中文优先的 Accept-Language 让 cn.bing.com 返回中文结果而不是英文站点的镜像。
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

BING_URL = "https://cn.bing.com/search"
# Bing 的 RSS 出口：同一站点、同一套结果，但给的是**最终 URL 和干净文本**（见 _parse_bing_rss）。
# 实测（本机 2026-09，`?q=…&format=rss&count=8`）：200，text/xml，3.5~4KB，10 条。
BING_RSS_URL = "https://www.bing.com/search"
BRAVE_URL = "https://search.brave.com/search"
TAVILY_URL = "https://api.tavily.com/search"
SERPER_URL = "https://google.serper.dev/search"

# 搜索接口超时/限流时最常出现的状态码（用于把「被限流」写成人话）
_RATE_LIMIT_CODES = (429, 503)

#: Bing RSS 一次要多少条：给大一点，后面的相关性过滤才有得挑（实测 count=8 → 10 条）
RSS_COUNT = 10
#: RSS 出口要 XML，Accept 跟 HTML 页分开写（实测两者都能返回 text/xml）
RSS_HEADERS = {**BASE_HEADERS, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}

# 明显的广告 / 追踪 / 站内跳转容器，出现在结果块里就直接丢掉。
# 依据：实测 Bing 结果页里广告位的 href 形如 `.../aclick?ld=...`、
# 页脚/推广位用 `go.microsoft.com/fwlink/?LinkID=...`；搜索引擎自己的域名
# （bing.com 的 ck/a 跳转、brave 站内搜索页等）对模型没价值。
_AD_URL_RE = re.compile(
    r"(?:^|[/.])(?:bing\.com/aclick|go\.microsoft\.com|duckduckgo\.com|"
    r"googleadservices\.com|google\.com/aclk|doubleclick\.net|"
    r"bing\.com/ck/a)\b",
    re.IGNORECASE,
)
# 搜索引擎自身的域名：作为**最终 URL** 出现时一律丢弃（跳转链应该被解开成真实目标）
_ENGINE_HOST_RE = re.compile(
    r"^https?://(?:[a-z0-9.-]*\.)?(?:bing\.com|brave\.com|microsoft\.com|google\.com|"
    r"duckduckgo\.com|msn\.com)(?::\d+)?(?:/|$)",
    re.IGNORECASE,
)

_BING_BLOCK_RE = re.compile(r'<li class="b_algo[^"]*"[^>]*>', re.IGNORECASE)
_BING_TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
#: Brave 的结果块开标签：class 里必须有独立词 snippet（`snippet-content-wrapper` 这类
#: 只在前后缀里含 snippet 的**不**算，所以两头用 \b 卡住），`data-pos` 由调用方再判一次。
_BRAVE_CONTAINER_RE = re.compile(r'<div\b[^>]*class="[^"]*\bsnippet\b[^"]*"[^>]*>', re.IGNORECASE)
_BRAVE_SECTION_RE = re.compile(r'id="results"', re.IGNORECASE)
_BRAVE_TITLE_RE = re.compile(
    r'<(?:div|h[1-6]|a)\b[^>]*class="[^"]*\b(?:search-)?snippet-title\b[^"]*"[^>]*>(.*?)</(?:div|h[1-6]|a)>',
    re.IGNORECASE | re.DOTALL,
)
# 退一档：class="title"（没有 search-snippet-title）的容器，标题可能是里面的 h3
_BRAVE_ANY_TITLE_RE = re.compile(
    r'<div\b[^>]*class="[^"]*\btitle\b[^"]*"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)
_ANCHOR_RE = re.compile(r"<a\b[^>]*\bhref=\"([^\"]*)\"[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<(?:script|style)\b.*?</(?:script|style)>", re.IGNORECASE | re.DOTALL)
_B_PARA_RE = re.compile(r'<p[^>]*\bclass="[^"]*\bb_[^"]*"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)
# 站点的展示域名/站点名行（"example.com"、"www.jxxy.net/ai"、"zhihu.com › question › …"、
# "YouTube"、"IT之家"）：这些不是标题也不是摘要，出现在候选里必须跳过
_SITE_LINE_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?(?:[/›»\s].*)?$"
    r"|^(?:youtube|wikipedia|zhihu|weibo|bilibili|twitter|x|facebook|instagram|reddit|"
    r"github|medium|linkedin|tiktok|douyin|youku|iqiyi|sohu|netease|sina|qq|csdn|juejin)"
    r"(?:\s*(?:com|cn|blog|视频|百科|知乎))*$"
    r"|^IT之家$",
    re.IGNORECASE,
)
_PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# 块级标签要换成换行而不是空格：`<h3>标题</h3><p>摘要</p>` 若都换成空格会拼成
# 「标题 摘要」一整行，按行取摘要时就分不开了（实测踩过）。
_BLOCK_TAG_RE = re.compile(r"</?(?:h[1-6]|p|div|li|tr|section|article|blockquote)\b[^>]*>",
                           re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\u00a0]+")# 剥标签会留下「假空格」：`动态：<strong>X</strong>` 会被剥成 `动态： X`。
# 这些空格在原文里并不存在，留在标题/摘要里会让模型看到断开的词（实测 Bing 因为
# <strong> 高亮关键词，标题普遍带这种空格）。
# 只收「中文标点旁」的空格——这是中文排版里本就不该有的那类；不碰中文和英文之间的
# 空格（`DeepSeek AI 发布` 是正常的），也不碰英文内部（`DeepSeek V4.1 Flash`）。
_FAKE_SPACE_RE = re.compile(
    r"[ ]+(?=[\u3000-\u303f\uff01-\uff65])"          # 中文标点前
    r"|(?<=[\u3000-\u303f\uff01-\uff65])[ ]+(?=[\u4e00-\u9fff])"   # 中文标点后紧跟汉字
)
_NL_RE = re.compile(r"\n{3,}")

# --------------------------------------------------------------- 内部状态

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

#: 可在测试里替换的 I/O 入口（默认就是 requests）
requests_get = requests.get
requests_post = requests.post


class _SearchError(Exception):
    """provider 级的失败：抓取失败、HTTP 错误码、页面结构识别不出来。"""


# --------------------------------------------------------------- 文本工具

def _text(raw: str) -> str:
    """HTML 片段 → 纯文本：剥 script/style/标签、还原实体、压空白。

    `<br>` 视作空格而不是换行：搜索结果里的 `<br>` 基本都用来断行展示，
    拼成一行反而更容易被模型当成一句连读的摘要。
    """
    s = _SCRIPT_RE.sub(" ", raw or "")
    s = _IMG_RE.sub(" ", s)              # 图标/站点图对文本没意义（alt 常是「全球 Web 图标」）
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_TAG_RE.sub("\n", s)       # 块级标签 → 换行（保留行结构，方便按行挑摘要）
    s = _TAG_RE.sub(" ", s)              # 其余行内标签 → 空格
    s = html_lib.unescape(s)
    s = s.replace("\r", "\n")
    s = _WS_RE.sub(" ", s)
    s = _FAKE_SPACE_RE.sub("", s)
    return _NL_RE.sub("\n\n", s).strip()


def _first_href(fragment: str) -> str:
    """片段里第一个 <a href> 的 href（原样，未解码实体）。"""
    m = _ANCHOR_RE.search(fragment or "")
    return m.group(1) if m else ""


def _longest_text(fragment: str) -> str:
    """最长的纯文本行——摘要的最后兜底。

    为什么按行取最长而不是直接取整块：块里混着站点名、面包屑、日期，整块拼接
    会得到「chooseai.net | 1 天前 正文…」这种噪声；分段取最长的一行通常就是正文。
    """
    lines = [ln.strip() for ln in _text(fragment).splitlines()]
    lines = [ln for ln in lines if ln]
    return max(lines, key=len) if lines else ""


def _is_junk_line(line: str) -> bool:
    """时间/日期/时长这类行：`08:30`、`1 天前`、`2026年5月5日`、`2026年4月24日`。

    视频聚合块里这种行又短又多，当标题/摘要都不合适（实测踩过：
    把 `15:25` 选成标题、把 `2026年4月24日` 选成摘要）。
    """
    s = line.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", s):
        return True
    if re.fullmatch(r"\d+\s*(?:秒|分钟|小时|天|周|个月|年)前", s):
        return True
    for fmt in ("%Y年%m月%d日", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y"):
        try:
            datetime.datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def _best_line(fragment: str, *, skip_prefix: str | None = None) -> str:
    """片段里最适合当摘要的那一行：最长的、且不是站点展示域名行的一行。

    站点行（`zhihu.com › question › 2066…`）经常比真摘要还长，所以要单独判掉；
    ``skip_prefix`` 用来排掉「开头就是标题」的那一行，避免把标题当摘要。
    """
    lines = [ln.strip() for ln in _text(fragment).splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if not _SITE_LINE_RE.match(ln) and not (skip_prefix and ln.startswith(skip_prefix))]
    return max(lines, key=len) if lines else ""


def _first_meaningful_line(fragment: str, *, min_len: int = 1, longest: bool = False) -> str:
    """片段里「像内容」的一行文字：不是站点域名行、不是时间日期行、够长。

    ``longest=True`` 取最长的一行而不是第一行——视频聚合块里第一行往往是站点名
    （`YouTube`），真正的标题更长，按长度挑更准。
    """
    lines = [ln.strip() for ln in _text(fragment).splitlines() if ln.strip()]
    lines = [ln for ln in lines
             if not _SITE_LINE_RE.match(ln) and not _is_junk_line(ln) and len(ln) >= min_len]
    if not lines:
        return ""
    return max(lines, key=len) if longest else lines[0]


def _is_usable_url(url: str) -> bool:
    """只保留 http(s) 且不是广告/追踪/搜索引擎自身的链接。"""
    if not url:
        return False
    if urlparse(url).scheme.lower() not in ("http", "https"):
        return False
    return not (_AD_URL_RE.search(url) or _ENGINE_HOST_RE.search(url))


def _clip(url: str, limit: int = 300) -> str:
    """URL 太长时截断（有些站点带一长串跟踪参数，塞进 prompt 只是浪费 token）。"""
    return url if len(url) <= limit else url[:limit]


# --------------------------------------------------------------- 跳转链归一化

def _decode_bing_redirect(url: str) -> str | None:
    """解开 Bing 的 `ck/a?...&u=a1<base64url>` 跳转链；解不出返回 None。

    实测 cn.bing.com 默认给的都是直接链接，但 Bing 在部分版位/地区会用
    `https://www.bing.com/ck/a?!&&p=...&u=a1aHR0cHM6Ly8...&ntb=1`：`u` 参数是把
    真实 URL 用 base64url 编码后再加 `a1` 前缀（老式 `?u=aHR0c...` 没有前缀，也一并支持）。

    两种编码都要能解——标准 base64 的 `+/`、URL 安全变体的 `-_`，以及被
    percent-encode 过的 `%2F`（Bing 有时会二次转义）。**解不出来就返回 None**，
    由调用方丢掉这一条：塞一条 bing.com 的跳转链给模型，等于给了个假来源。
    """
    if not url:
        return None
    raw = url.strip()
    if raw.startswith("//"):                       # 协议相对
        raw = "https:" + raw
    scheme = urlparse(raw).scheme.lower()
    if scheme not in ("http", "https"):            # 相对路径（/ck/a?...）也算
        if raw.startswith("/"):
            raw = "https://www.bing.com" + raw
        else:
            return None

    try:
        query = parse_qs(urlparse(raw).query, keep_blank_values=True)
    except ValueError:
        return None
    candidates: list[str] = []
    for key in ("u", "url", "r"):
        candidates.extend(query.get(key, []))
    # 少数版位把目标放在 `?u=a1...` 之外的 payload 参数里
    for key in ("u", "url"):
        m = re.search(rf"[?&]{key}=([^&]+)", raw)
        if m:
            candidates.append(m.group(1))
    candidates = [unquote(c) for c in candidates]

    for cand in candidates:
        for payload in (cand[2:] if cand.startswith("a1") else None, cand):
            if not payload:                        # 没有 a1 前缀时这一档就是 None，跳过
                continue
            # 逐级剥掉 Windows-1252 误码：某些字节被当成 latin-1 解码后又编码回来，
            # 会在 base64 里混进 Â/Ã，先按原样试、再按剥掉后的试。
            attempts = [payload]
            stripped = payload.replace("\u00c2", "").replace("\u00c3", "")
            if stripped != payload:
                attempts.append(stripped)
            for attempt in attempts:
                if not re.fullmatch(r"[A-Za-z0-9_\-=+/]+", attempt):
                    continue
                if len(attempt) < 8:
                    continue
                padded = attempt + "=" * (-len(attempt) % 4)
                for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                    try:
                        decoded = decoder(padded)
                    except (ValueError, TypeError):
                        continue
                    try:
                        text = decoded.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if text.startswith(("http://", "https://")):
                        return text.strip()
    return None


def _normalize_url(raw: str) -> str:
    """结果链接归一化：还原 HTML 实体 → 解开跳转链 → 过滤广告/非 http。

    返回空串表示这条结果应该被丢掉（**不是**塞个搜索引擎链接顶上）。
    """
    url = html_lib.unescape((raw or "").strip())
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):                       # 站内相对链接，没有来源价值
        return ""
    if "bing.com/ck/a" in url.lower():
        target = _decode_bing_redirect(url)
        if not target:
            return ""
        url = html_lib.unescape(target.strip())
    return url if _is_usable_url(url) else ""


# --------------------------------------------------------------- HTML 分块

def _div_block(page: str, tag_start: int) -> tuple[str, int]:
    """从 `<div ...>` 的起始下标处返回 (整块 HTML, 块结束下标)。

    自己数 `<div` / `</div>` 做配对，原因是 `<li class="b_algo">` 内部还有多层 div，
    用非贪婪正则 `.*?</div>` 会在一层就截断，丢掉摘要。数不到结尾就取到页面末尾
    （宁可多给一点内容，也别把最后一条结果整条丢掉）。
    """
    i = tag_start
    depth = 0
    while True:
        nxt_open = page.find("<div", i)
        nxt_close = page.find("</div>", i)
        if nxt_close == -1:
            return page[tag_start:], len(page)
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            i = nxt_open + 4
            continue
        depth -= 1
        i = nxt_close + 6
        if depth <= 0:
            return page[tag_start:i], i


def _bing_blocks(page: str) -> list[str]:
    """切出每条结果的 `<li class="b_algo">` 块。

    依据（实测 cn.bing.com 2026-09 的结果页）：每条自然结果都是一个
    `<li class="b_algo" data-id iid=SERP.xxxx>`，内部结构为

        <div class="b_tpcn"><a class="tilk" href=...>站点图标+域名</a></div>
        <h2 class=""><a href="<真实 URL>"><strong>关键词</strong>…</a></h2>
        <div class="b_caption"><p class="b_lineclamp2">摘要…</p></div>

    所以按 `<li class="b_algo` 切块最稳；块内再按 h2 → p 取字段。
    """
    starts = [m.start() for m in _BING_BLOCK_RE.finditer(page)]
    blocks: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(page)
        seg = page[start:end]
        cut = seg.rfind("</li>")                  # 去掉块尾的后续内容
        if cut != -1:
            seg = seg[: cut + len("</li>")]
        blocks.append(seg)
    return blocks


def _headings(page: str, start: int = 0) -> list[int]:
    """区块里所有标题标签（h1~h6）的位置，用于没有 data-pos 时的回退分块。

    用全部六级而不是只认 h2/h3：实测 Brave 的「视频」聚合块用 `<h4>视频</h4>`
    当分组标题，只认 h2/h3 会漏掉整块。
    """
    return [m.start() for m in re.finditer(r"<h[1-6][^>]*>", page[start:], re.IGNORECASE)]


def _div_start(page: str, idx: int) -> int:
    """返回 idx 处**所在**（或之前最近的）`<div>` 标签的起始下标；没有则 -1。

    **不能**写成 `page.rfind("<div", 0, idx + 1)`：`data-pos` 出现在开标签属性里时
    （`<div class="snippet" data-pos="1">`），`data-pos` 在标签内部，往回找 `<div`
    会越过这个标签本身、找到外层的 div（实测踩过：Bing 级的整页被当成一条结果）。
    正确做法是先退到最近的 `<`，那才是这个标签的起点；若不是 div（比如落在 <a> 上），
    再往前找一层——Brave 的结果块外层是 div，标题/链接在里层。
    """
    seen = 0
    while idx >= 0 and seen < 8:                  # 最多回退 8 层，防御性上限
        lt = page.rfind("<", 0, idx + 1)
        if lt == -1:
            return -1
        if re.match(r"<div\b", page[lt:lt + 8], re.IGNORECASE):
            return lt
        idx, seen = lt - 1, seen + 1
    return -1


def _brave_blocks(page: str) -> list[str]:
    """切出 Brave 的每条结果块，返回块 HTML 列表（含首尾）。

    依据（**实测存档**：本机 2026-09 用 `?q=…&source=web` 抓到 200 / 180~232KB 的响应）：
    每条结果是一个带 `data-pos` + `data-type` 的 snippet 容器：

        <div class="snippet svelte-jmfu5f" data-pos="1" data-type="web" data-keynav="true">
          <div class="result-body"><div class="result-wrapper"><div class="result-content">
            <a href="<目标 URL>">
              <div class="site-name-wrapper">…站点名 / <cite class="snippet-url">域名 › 面包屑</cite></div>
              <div class="title search-snippet-title line-clamp-1">标题</div>
            </a>
            <div class="generic-snippet"><div class="content">…摘要…</div></div>
          </div></div></div>
        </div>

    两个必须记住的实测事实：
      * **整页没有 `id="results"`**（两页都搜过：0 次）。真正的容器是
        `div.serp-layout > div.serp-columns-main`，类名带 svelte 哈希、不能依赖；
        所以这里不下钻容器，直接全局找结果块——顺带绕开「`id="results"` 不存在就
        一条都解析不出」这种失效。
      * 结果块**必然**是 `class` 里含独立词 `snippet` **且带 `data-pos`** 的 div
        （实测 20~21 个 / 页，`data-type` 为 web|cluster）。类名里含 "snippet" 的还有
        `title search-snippet-title`、`generic-snippet`、`snippet-content-wrapper`，
        以及页顶那个 `<div class="snippet noscript-hide" id="llm-snippet">`（AI 摘要框，
        **不带 data-pos**、不是搜索结果），所以判定必须同时要求 `data-pos`。

    切块做法：
      1) `_BRAVE_CONTAINER_RE` 匹配**整条开标签**，再要求标签里有 `data-pos`
         （这样 data-pos 出现在属性列表的哪个位置都不会漏——用「data-pos 前 200 字符
         里找 `<div`」那种写法会漏，实测两种页面的属性顺序不一样）；
      2) `_div_block` 做标签配对拿到整块；
      3) 块越过**下一条**容器起点时（说明外面还包着一层 div），截到下一条之前；
      4) 块必须含 `<a`；
      5) 一条都没切出来时才退回「标题标签所在的那个 div 块」。
    """
    # 注意：一律在**原始 page** 上用绝对偏移做 rfind / 配对。曾经的写法是先把
    # `id="results"` 之前的内容切掉再找，结果偏移量全部错位、块切成了空串。
    # `_BRAVE_SECTION_RE` 只是「万一哪天真有这层容器就从它之后开始」的可选优化。
    section = _BRAVE_SECTION_RE.search(page)
    base = section.end() if section else 0
    tag_starts: list[int] = []
    for m in _BRAVE_CONTAINER_RE.finditer(page, base):
        if "data-pos" not in m.group(0):              # AI 摘要框等：不是搜索结果
            continue
        tag_starts.append(m.start())

    out: list[str] = []
    for idx, tag_start in enumerate(tag_starts):
        block, end = _div_block(page, tag_start)
        # 用**块结束偏移量** end 判断，别用 len(block)：tag_start 不一定从 0 开始，
        # 用长度比会漏判「容器块里嵌着下一条结果」。
        if idx + 1 < len(tag_starts) and tag_starts[idx + 1] < end:
            block = page[tag_start:tag_starts[idx + 1]]   # 外层包裹块：截到下一条之前
        if "<a " not in block.lower():
            continue
        out.append(block)
    if out:
        return out

    # 回退：改版到没有 snippet/data-pos 标记时，按「标题标签所在的那个 div 块」猜。
    # 块的起点不能直接从标题往前找最近的 <div —— 标题通常嵌在 <a> 里，那样只会找到
    # 一个内层 div（实测：这种块里没有 <a，整页一条也解析不出来）。正确做法是先找到
    # 标题所在的 <a>，再找那个 <a> 所在的 div。
    # 注意 rfind 的 end 要用 idx + 1：<a> 的 `<` 正好落在 idx 上，用 0..idx 会把它漏掉。
    for heading in _headings(page, base):
        anchor_at = page.rfind("<a", 0, heading + 1)
        idx = anchor_at if anchor_at > base else heading
        block, _end = _div_block(page, _div_start(page, idx))
        if "<a " in block.lower():
            out.append(block)
    return out


# --------------------------------------------------------------- 解析：Bing

def _parse_bing(page: str, limit: int) -> list[dict]:
    """解析 Bing 结果页 → [{title, url, snippet}]；一条都没解析出来时抛 _SearchError。"""
    results: list[dict] = []
    seen: set[str] = set()
    blocks = _bing_blocks(page)

    for block in blocks:
        # 块内先找 <h2> 里的 <a href>：Bing 的结果标题永远在 h2 里，且 h2 里的
        # 那个链接就是真实目标（块首 b_tpcn 里的 tilk 只是站点图标链接，同一个 URL）。
        title_html, url = "", ""
        m = _BING_TITLE_RE.search(block)
        if m:
            title_html = m.group(1)
            url = _first_href(title_html)
        if not url:                               # 没 h2 的版位（视频/问答卡）退回整块第一个链接
            url = _first_href(block)
        url = _normalize_url(url)
        title = _text(title_html)
        if not title or not url or url in seen:
            continue
        seen.add(url)

        snippet = ""
        p = _B_PARA_RE.search(block) or _PARA_RE.search(block)
        if p:
            snippet = _text(p.group(1))
        if not snippet:                           # 没有 <p> 的版位：取 h2 之后最长的纯文本行
            tail = block[m.end():] if m else block
            snippet = _longest_text(tail[:4000])

        results.append({"title": title[:300], "url": _clip(url), "snippet": snippet[:600]})
        if len(results) >= limit:
            break

    if not results:
        raise _SearchError("无法从 Bing 页面解析出结果（页面结构可能已改版或触发了风控）")
    return results


# --------------------------------------------------------------- 解析：Brave

def _parse_brave(page: str, limit: int) -> list[dict]:
    """解析 Brave 结果页 → [{title, url, snippet}]；一条都没解析出来时抛 _SearchError。"""
    results: list[dict] = []
    seen: set[str] = set()

    for block in _brave_blocks(page):
        # 块结构（实测 search.brave.com，2026-09，见 _brave_blocks 的注释）：
        #   <a href="<目标>">…站点名/域名…<div class="title search-snippet-title">标题</div></a>
        #   <div class="generic-snippet"><div class="content">…摘要…</div></div>
        # 标题**不是** h2/h3（整页 0 个 <h2>/<h3>），而是那个 div；块尾的摘要则没有
        # 稳定类名。所以按「<a> 前后」切开：<a> 里最后一段文字是标题，<a> 之后最长
        # 的文字是摘要——比盯某个 class 更耐改版。
        anchor = _ANCHOR_RE.search(block)
        url = _normalize_url(_first_href(block))
        if not url:
            continue

        head = block[: anchor.end()] if anchor else block
        tail = block[anchor.end():] if anchor else ""
        # 先按标题类名找；视频/多元结果里标题可能不在 <a> 内，所以退一步在整块里找。
        title_m = (_BRAVE_TITLE_RE.search(head) or _BRAVE_TITLE_RE.search(block)
                   or _BRAVE_ANY_TITLE_RE.search(head))
        title = _text(title_m.group(1)) if title_m else ""
        if title_m is not None:
            # 视频/图片这类「聚合块」里，<a> 内那个 title 容器装的其实是分组标签
            # （实测 Brave 的 `data-type="cluster"` 块里是 `<span>视频</span>`，
            # 而块内**没有**第二个 title 容器，真标题在 <a> 之后）。所以标题过短时
            # 直接从 <a> 之后取第一行有意义的文字（跳过 "15:25" 这类时长/日期行）。
            others = [_text(t).strip() for t in _BRAVE_TITLE_RE.findall(block)]
            if len(title) < _MIN_TITLE_LEN:
                title = max([t for t in others if t] or [title], key=len)
                if len(title) < _MIN_TITLE_LEN:
                    title = (_first_meaningful_line(tail, min_len=_MIN_TITLE_LEN, longest=True)
                             or title)
        if not title:
            # 连类名都没有时（老结构：`<div class="title"><h3>…</h3></div>`），
            # 取 <a> 里第一行「不是站点域名行」的文字——标题永远在摘要之前，
            # 按「最长的一行」猜反而会挑到摘要。站点行单独判掉，否则
            # 「zhihu.com › question › 2066…」就会变成标题。
            title = _first_meaningful_line(head, min_len=2)
        title = title.strip()
        if not title or url in seen:
            continue
        seen.add(url)

        snippet = _best_line(tail) if tail else ""
        if not snippet and tail:                  # 回退：块尾任意 <p>
            p = _PARA_RE.search(tail)
            if p:
                snippet = _text(p.group(1))
        if not snippet:
            # 老式/别的版位结构：摘要和标题一起在 <a> **里面**，块尾什么都没有。
            # 就在 <a> 里取「最长且不是标题、不是站点行」的一行当摘要。
            snippet = _best_line(head, skip_prefix=title)
        # 标题猜错时（把摘要当成了标题）会变成 snippet == title，这时用剩下那行救回来：
        # 谁长谁当摘要，短的那个当标题。
        if snippet == title:
            rest = [ln.strip() for ln in _text(head).splitlines()
                    if ln.strip() and ln.strip() != title and not _SITE_LINE_RE.match(ln.strip())]
            if rest:
                snippet, title = max(rest, key=len), min(rest + [title], key=len)
        # 「视频/图片/网页」这类分组标题：拿摘要顶上比留个分组标签强
        # （实测 Brave 的视频聚合块会给 `<h4>视频</h4>`，那不是结果标题）。
        if title and _GROUP_LABEL_RE.match(title) and snippet and snippet != title:
            title, snippet = snippet, title
        if snippet.startswith(title):              # 摘要里常把标题重复一遍，去掉它
            snippet = snippet[len(title):].lstrip(" -—·|")
        results.append({"title": title[:300], "url": _clip(url), "snippet": snippet[:600]})
        if len(results) >= limit:
            break

    if not results:
        raise _SearchError("无法从 Brave 页面解析出结果（页面结构可能已改版或触发了限流）")
    return results


# --------------------------------------------------------------- 抓取 / 各 provider

def _fetch(method: str, url: str, *, params: dict | None = None, data: dict | None = None,
           headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
           provider: str = "") -> requests.Response:
    """统一的网络入口：任何异常都转成 _SearchError（带人话原因）。

    超时拆成 (连接 8s, 读取 timeout)：连接阶段短一点能更快地判掉「根本连不上」，
    读取阶段给调用方的 timeout，兼顾慢站与流式回答的体感。

    `provider` 只影响错误文案：搜索站被限流（429）和「页面改版」是两种完全不同的
    处理方式（前者等一会儿再来、后者要改解析器），所以 429 单独说清楚。
    """
    fn = requests_post if method.upper() == "POST" else requests_get
    try:
        resp = fn(url, params=params, data=data, headers=headers, timeout=(CONNECT_TIMEOUT, timeout))
    except requests.exceptions.Timeout as exc:
        raise _SearchError(f"请求超时（{timeout:g}s）：{exc}") from exc
    except requests.exceptions.SSLError as exc:
        raise _SearchError(f"TLS/证书错误：{exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise _SearchError(f"连接失败（网络不通或被挡）：{exc}") from exc
    except requests.exceptions.RequestException as exc:   # 其余 requests 异常
        raise _SearchError(f"请求出错：{exc}") from exc
    if resp.status_code in _RATE_LIMIT_CODES:
        who = f"{provider} " if provider else ""
        raise _SearchError(
            f"{who}被限流（HTTP {resp.status_code}）：请求太频繁，稍后再试"
            "（也可以把 PA_SEARCH_PROVIDER 换成另一个搜索源）"
        )
    if resp.status_code >= 400:
        raise _SearchError(f"HTTP {resp.status_code}（站点拒绝或触发了风控）")
    return resp


def _parse_bing_rss(text: str, limit: int) -> list[dict]:
    """解析 Bing 的 RSS 出口（`?format=rss`）→ [{title, url, snippet}]。

    为什么把它当主路径（本机实测）：
    - `https://www.bing.com/search?q=…&format=rss&count=8` → 200 / `text/xml` / 3.5~4KB / 10 条，
      `<item><title>/<link>/<description>` 都是**最终形态**：link 是真实目标 URL，
      不会出现 `bing.com/ck/a?u=a1<base64>` 那种跳转链，所以不会有「解不出就只能丢结果」
      的损耗；title/description 也比 HTML 页干净（没有 `<strong>` 高亮碎片，
      所以不会出现 `DeepSeek V4 .1 Flash` 那种被高亮标签劈开的标题）。
    - 同一查询下 RSS 与 HTML 的**结果 URL 顺序完全一致**（本机同一条查询对比过），
      但 RSS 的体积是 HTML 的 1/25，解析和出错面都小得多。

    非 XML 响应（风控页 / HTML 错误页 / 空响应）必须能识别出来：这里只认
    「能 XML 解析 + 有 `<item>` + 至少解析出一条」，否则抛 _SearchError，由调用方
    退回 HTML 解析（见 _search_bing）。
    """
    if not text or "<" not in text:
        raise _SearchError("RSS 响应为空或不是 XML")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise _SearchError(f"RSS 不是合法 XML（可能被风控页/HTML 替代）：{exc}") from exc

    results: list[dict] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        def field(tag: str) -> str:
            return (item.findtext(tag) or "").strip()
        url = _normalize_url(field("link"))
        title = _text(field("title"))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        results.append({"title": title[:300], "url": _clip(url),
                        "snippet": _text(field("description"))[:600]})
        if len(results) >= limit:
            break
    if not results:
        raise _SearchError("RSS 里没有可用的结果项")
    return results


def _bing_rss(query: str, limit: int, timeout: float) -> list[dict]:
    """取 Bing 的 RSS 出口。count 给大一点：多要几条，后面的相关性过滤才有得挑。"""
    resp = _fetch("GET", BING_RSS_URL,
                  params={"q": query, "format": "rss", "count": max(limit, RSS_COUNT)},
                  headers=dict(RSS_HEADERS), timeout=timeout, provider="bing")
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ("xml" not in ctype and "rss" not in ctype) and (resp.text or "").lstrip()[:5] != "<?xml":
        raise _SearchError(f"返回的不是 XML（Content-Type={ctype or '未知'}），退回 HTML 解析")
    return _parse_bing_rss(resp.text or "", limit)


def _bing_html(query: str, limit: int, timeout: float) -> list[dict]:
    """Bing 的 HTML 页解析：只在 RSS 不可用时使用（兜底）。

    注意：这里**不带** `setlang`/`mkt`。实测给 Bing 加这些语言参数会把中文长尾查询
    的结果整体打歪（`SGLang 朱邦华` → 摩托车车行；`月球大叔 播客` → Google Docs），
    不加反而正常。
    """
    resp = _fetch("GET", BING_URL, params={"q": query, "ensearch": "0"},
                  headers=dict(BASE_HEADERS), timeout=timeout, provider="bing")
    return _parse_bing(resp.text or "", limit)


def _search_bing(query: str, limit: int, timeout: float, terms: list[str] | None = None) -> list[dict]:
    """Bing：RSS 优先，失败/空则退回 HTML 解析（两条路都走不通才抛错）。"""
    try:
        results = _bing_rss(query, limit, timeout)
    except _SearchError as rss_err:
        try:
            results = _bing_html(query, limit, timeout)
        except _SearchError as html_err:
            raise _SearchError(f"RSS 与 HTML 都失败（RSS：{rss_err}；HTML：{html_err}）") from html_err
        return results
    return results


def _search_brave(query: str, limit: int, timeout: float, terms: list[str] | None = None) -> list[dict]:
    resp = _fetch("GET", BRAVE_URL, params={"q": query, "source": "web"},
                  headers=dict(BASE_HEADERS), timeout=timeout, provider="brave")
    return _parse_brave(resp.text or "", limit)


def _json_of(resp: requests.Response) -> dict:
    """取响应的 JSON 对象；不是对象或解析不了就给 _SearchError。"""
    try:
        data = resp.json()
    except ValueError as exc:                     # requests 的 JSONDecodeError
        raise _SearchError(f"返回的不是 JSON（可能被网关拦截）：{exc}") from exc
    if not isinstance(data, dict):
        raise _SearchError("返回的 JSON 结构不是对象")
    return data


def _search_tavily(query: str, limit: int, timeout: float,
                   terms: list[str] | None = None) -> list[dict]:
    """Tavily：POST /search，Authorization: Bearer <key>，结果在 results[].title/url/content。

    文档：https://docs.tavily.com/documentation/api-reference/endpoint/search
    """
    key = _key_of("tavily")
    if not key:
        raise _SearchError("未配置 TAVILY_API_KEY")
    resp = _fetch(
        "POST", TAVILY_URL,
        data=json.dumps({"query": query, "max_results": limit, "search_depth": "basic"}).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": UA},
        timeout=timeout,
    )
    data = _json_of(resp)
    out: list[dict] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = _normalize_url(str(item.get("url") or ""))
        title = _text(str(item.get("title") or ""))
        if not url or not title:
            continue
        out.append({"title": title[:300], "url": _clip(url),
                    "snippet": _text(str(item.get("content") or ""))[:600]})
        if len(out) >= limit:
            break
    if not out:
        raise _SearchError("Tavily 返回 0 条结果")
    return out


def _search_serper(query: str, limit: int, timeout: float,
                   terms: list[str] | None = None) -> list[dict]:
    """Serper：POST https://google.serper.dev/search，X-API-KEY 头，结果在 organic[].title/link/snippet。"""
    key = _key_of("serper")
    if not key:
        raise _SearchError("未配置 SERPER_API_KEY")
    resp = _fetch(
        "POST", SERPER_URL,
        data=json.dumps({"q": query, "num": limit}).encode("utf-8"),
        headers={"X-API-KEY": key, "Content-Type": "application/json", "User-Agent": UA},
        timeout=timeout,
    )
    data = _json_of(resp)
    out: list[dict] = []
    for item in data.get("organic") or []:
        if not isinstance(item, dict):
            continue
        url = _normalize_url(str(item.get("link") or ""))
        title = _text(str(item.get("title") or ""))
        if not url or not title:
            continue
        out.append({"title": title[:300], "url": _clip(url),
                    "snippet": _text(str(item.get("snippet") or ""))[:500]})
        if len(out) >= limit:
            break
    if not out:
        raise _SearchError("Serper 返回 0 条自然结果")
    return out


#: 统一签名 (query, limit, timeout, terms) -> list[dict]；key 型 provider 不用 terms
#: （它们按 key 直接查 API，参数里没有给语言/词表的位置），所以只是接受并忽略。
_SEARCHERS = {
    "bing": _search_bing,
    "brave": _search_brave,
    "tavily": _search_tavily,
    "serper": _search_serper,
}


# --------------------------------------------------------------- key / 配置

def _env_keys() -> set[str]:
    """当前进程可见的 key 型环境变量集合（含 .env 里写的，config 已在 import 时注入）。"""
    present: set[str] = set()
    for name in KEY_ENV.values():
        if (os.environ.get(name) or "").strip():
            present.add(name)
    # 直接读文件兜底：webapp 的「保存设置」是写 .env，而 .env 只在 config import 时
    # 注入一次 os.environ；不读文件会出现「刚填了 key 但 available() 还说没配」。
    try:
        from .settings import read_env
        env = read_env()
    except Exception:                             # pragma: no cover - 极端环境
        env = {}
    for name in KEY_ENV.values():
        if str(env.get(name) or "").strip():
            present.add(name)
    return present


def _key_of(provider: str) -> str:
    """取某个 key 型 provider 的 key（环境变量优先，其次 .env）；没有则空串。"""
    name = KEY_ENV.get(provider)
    if not name:
        return ""
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        from .settings import read_env
        return str(read_env().get(name) or "").strip()
    except Exception:                             # pragma: no cover
        return ""


def _config() -> tuple[bool, str | None]:
    """(是否启用联网, 用户指定的 provider)。

    `PA_SEARCH=0/false/off/no/不/关` 表示彻底关掉；**非法值（如 maybe）按开启处理**——
    用户显然是想开，猜错方向比报错友好（非法值当成关闭会让人以为功能坏了）。
    """
    raw = (os.environ.get("PA_SEARCH") or "").strip().lower()
    enabled = raw not in {"0", "false", "off", "no", "disable", "disabled", "关闭", "否", "不"}
    forced = (os.environ.get("PA_SEARCH_PROVIDER") or "").strip().lower()
    return enabled, (forced if forced in PROVIDERS else None)


def available() -> dict:
    """当前可用的 provider 概览（设置页直接渲染这个）。

        {
          "providers": {name: {"label": str, "needs_key": bool, "configured": bool}},
          "default": "<现在实际会用的>",
          "enabled": bool        # PA_SEARCH=0 时为 False
        }

    configured 的口径：免 key 的永远 True；key 型的看环境变量 / .env 里有没有 key。
    default 的选取：PA_SEARCH_PROVIDER 指定则用它（没配 key 就退回免 key 的，
    否则用户会在界面上看到一个永远失败的默认值）；没指定则优先用户配了 key 的
    （tavily > serper），都没有就用 bing。
    """
    enabled, forced = _config()
    present = _env_keys()

    providers: dict[str, dict] = {}
    for name in PROVIDERS:
        needs_key = name in KEYED
        env_name = KEY_ENV.get(name, "")
        configured = True if not needs_key else env_name in present
        providers[name] = {"label": LABELS[name], "needs_key": needs_key,
                           "configured": configured, "env": env_name}

    if forced:
        if providers[forced]["configured"]:
            default = forced
        else:
            # 用户指定的 provider 没配 key：退回免 key 的，**不要**顺手换成另一个
            # key 型 provider —— 他明确表达了偏好，界面上的默认值应尽量贴近它。
            default = FALLBACK_PROVIDER
    else:
        default = ""
        for name in KEYED_ORDER:
            if providers[name]["configured"]:
                default = name
                break
        if not default:
            default = FALLBACK_PROVIDER          # bing：实测最稳的免 key 通道

    return {"providers": providers, "default": default, "enabled": enabled,
            "env": list(SEARCH_ENV)}


# --------------------------------------------------------------- 缓存

def _cache_key(query: str, provider: str, limit: int) -> str:
    """缓存键：query 归一化（去空白 + 小写）+ provider + limit 的摘要。

    带 limit 是因为搜索条数不同会调不同页数；小写归一让 "AI" / "ai" 复用同一份结果。
    """
    digest = hashlib.sha256(f"{provider}\x00{limit}".encode("utf-8")).hexdigest()[:8]
    return f"{query.strip().lower()}\x00{digest}"


def _cache_get(key: str) -> dict | None:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if not hit:
            return None
        ts, payload = hit
        if now - ts > CACHE_TTL:
            _cache.pop(key, None)
            return None
        # 深拷贝：调用方（或模型）改了返回的 list 不该污染缓存
        return json.loads(json.dumps(payload, ensure_ascii=False))


def _cache_put(key: str, payload: dict) -> None:
    with _cache_lock:
        # 顺手清掉过期项，长跑进程里缓存不会无限涨
        now = time.time()
        for k in [k for k, (ts, _) in _cache.items() if now - ts > CACHE_TTL]:
            _cache.pop(k, None)
        _cache[key] = (now, json.loads(json.dumps(payload, ensure_ascii=False)))


def clear_cache() -> None:
    """清空搜索缓存（测试用；换 provider / 换 key 后强制重查也可以调）。"""
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------- 对外接口

def _chain(provider: str, forced: str | None) -> list[str]:
    """要依次尝试的 provider 列表：指定的那个排第一，免 key 的按 bing → brave 兜底。

    `forced`（PA_SEARCH_PROVIDER 配的那个）不再从兜底序列里剔除，而是**排到最前**：
    「优先用你选的」和「你选的挂了就直接失败」是两回事，后者会白白丢掉能用的兜底。
    指定了 key 型 provider 时不自动降级到免 key 站点（key 无效/余额不足换站点也白搭，
    而且会绕开用户明确的选择）。
    """
    order = [provider]
    if provider in KEYLESS:
        for name in KEYLESS:
            if name not in order:
                order.append(name)
    if forced in KEYLESS:
        order = sorted(order, key=lambda n: (n != forced,))       # forced 排最前
    return order


def _relevance_terms(terms: list[str] | None, limit: int = 8) -> list[str]:
    """清洗调用方给的检索词：去空白、去重（保序）、丢掉过短的。

    丢掉长度 <2 的词是因为单个字符（`的`、`v`）几乎必然在任意文本里命中，等于没过滤。
    """
    out: list[str] = []
    for term in terms or []:
        t = str(term or "").strip()
        if len(t) < 2 or t.lower() in {x.lower() for x in out}:
            continue
        out.append(t)
    return out[:limit]


def _matches(term: str, item: dict) -> bool:
    """结果是否与某个检索词相关。

    判定 = 该词作为**大小写不敏感的完整子串**出现在「标题 + 摘要」里。

    为什么必须用**完整词**、不能拆 2-gram 或单字（本机实测的反例）：
      * 查询 `月球大叔 播客`，Bing 退化成了「月球」——返回《月球（地球唯一的天然卫星）
        _百度百科》。若用 2-gram，`月球大叔` 的碎片 `月球` 会命中这条垃圾结果，
        于是「月球百科」被当成与「月球大叔」相关的东西喂给模型。
      * 同理 `朱邦华` 不能拆成 `朱`/`邦`/`华`：`朱（汉字）_百度百科` 会被判成相关。
    所以判定只认调用方给的**完整检索词**（例如 `月球大叔`、`播客`）。
    """
    haystack = f"{item.get('title') or ''} {item.get('snippet') or ''}".casefold()
    return term.casefold() in haystack


def _filter_relevant(results: list[dict], terms: list[str]) -> list[dict]:
    """保留至少命中一个 `terms` 的结果（terms 为空时原样返回，保持向后兼容）。"""
    if not terms:
        return results
    return [r for r in results if any(_matches(t, r) for t in terms)]


def _irrelevant_error(terms: list[str], query: str) -> str:
    """全被过滤掉时的说明：告诉模型「这个词搜不到」，而不是让它以为「没有相关信息」。"""
    shown = "「" + "、".join(terms[:2]) + "」" if terms else f"「{query}」"
    return f"搜索服务没有返回与{shown}相关的结果（可能不支持这个词）"


def _failure(query: str, provider: str, attempted: list[str], errors: list[str]) -> dict:
    """全挂了的返回值：仍然 ok=False + 人话 error，绝不抛异常。"""
    detail = "；".join(e for e in errors if e) or "未知原因"
    return {"query": query, "provider": provider, "results": [], "ok": False,
            "error": f"本次未能联网（已尝试 {'、'.join(attempted)}）：{detail}"}


def search(query: str, *, limit: int = 6, provider: str | None = None,
           timeout: float = DEFAULT_TIMEOUT, terms: list[str] | None = None) -> dict:
    """联网搜索，返回给助手用的结构化结果。

        {
          "query": 原样回显的查询词,
          "provider": 实际用上的 provider（降级后是降级后的那个）,
          "results": [{"title": str, "url": str, "snippet": str}, ...],
          "ok": bool,
          "error": "" | "人话错误信息"
        }

    `terms` 是「这次实际用于检索的词」，用于**相关性过滤**（可选，不传就不过滤，
    现有调用方不受影响）：搜索服务对中文长尾专名（人名、栏目名）索引很差，会把查询
    退化成只匹配其中一个字，返回一堆完全无关的百科条目。那些结果交给模型比不联网更糟，
    所以只要结果在「标题 + 摘要」里**一个完整检索词都没命中**，就丢掉这条；
    全被丢光时返回 ok=False + 「没有相关结果」的人话说明。

    **绝不抛异常**：调用方是流式回答，这里抛出去用户什么也看不到。所以连接失败、
    超时、风控、页面改版、key 没配、被限流……全部转成 ok=False + error 里的中文说明。
    一条结果都没有时 results=[] 且 ok=False（空结果对模型没有价值，等价于没搜到）。
    """
    query = (query or "").strip()
    limit = max(1, min(int(limit or 0) or 1, 20))
    enabled, forced = _config()
    rel_terms = _relevance_terms(terms)

    if not query:
        return {"query": query, "provider": "", "results": [], "ok": False,
                "error": "查询词为空"}
    if not enabled:
        return {"query": query, "provider": "", "results": [], "ok": False,
                "error": "联网搜索已关闭（PA_SEARCH=0）"}

    want = (provider or "").strip().lower()
    avail = available()
    if want not in PROVIDERS:
        want = forced if (forced and avail["providers"][forced]["configured"]) else avail["default"]

    attempted: list[str] = []
    errors: list[str] = []
    # 只要出现过「搜到东西但全被相关性过滤掉」，最终就该说「没有相关结果」而不是
    # 「未能联网」——两者对用户含义完全不同（是断网了，还是该换个词再搜）。
    filtered_out = False
    for name in _chain(want, forced):
        attempted.append(name)
        key = _cache_key(query, name, limit)
        cached = _cache_get(key)
        if cached is not None:
            return cached                       # 缓存命中：连「已尝试」都照旧回显
        try:
            raw = _SEARCHERS[name](query, limit, timeout, rel_terms)
        except _SearchError as exc:
            errors.append(f"{name}：{exc}")
            continue
        except Exception as exc:                # pragma: no cover - 兜底：任何意外都别抛
            errors.append(f"{name}：意外错误 {type(exc).__name__}: {exc}")
            continue
        if not raw:
            errors.append(f"{name}：没有返回任何结果")
            continue

        results = _filter_relevant(raw, rel_terms)
        if not results:
            # 搜到了东西，但没一条和检索词相关 —— 这正是「Bing 对中文长尾专名索引差」
            # 的典型表现，不能当成成功结果给模型。
            filtered_out = True
            errors.append(f"{name}：{len(raw)} 条结果都不含检索词 "
                          f"{'、'.join(rel_terms) or query}（被相关性过滤掉）")
            continue
        payload = {"query": query, "provider": name, "results": results, "ok": True, "error": ""}
        _cache_put(key, payload)
        return payload

    if filtered_out and rel_terms:
        # 网络是通的、只是没搜到相关的东西：给一句更准的话，别让用户以为断网了。
        return {"query": query, "provider": want, "results": [], "ok": False,
                "error": _irrelevant_error(rel_terms, query)}
    return _failure(query, want, attempted, errors)


def format_for_prompt(result: dict, *, limit: int = 6) -> str:
    """把 search() 的返回值格式化成给模型看的文本块。

    要求：每条带序号 + 标题 + URL + 摘要；顶部明确写清来源与可信度边界；失败时也要
    给出一句话（而不是空串）——空串会让模型以为「搜了但确实没有相关信息」，从而
    编造「据我了解」；写明「未能联网」它才知道该说不知道。
    """
    query = str((result or {}).get("query") or "").strip()
    if not result or not isinstance(result, dict):
        return "" if not query else f"（查询「{query}」没有可用的搜索结果）"

    head = f"【网络搜索结果】查询词：{query}" if query else "【网络搜索结果】"
    if not result.get("ok"):
        error = str(result.get("error") or "").strip() or "原因未知"
        return (f"{head}\n本次未能联网（原因：{error}）。"
                "请不要编造搜索结果或引用具体链接；如果确有必要，只说明这是你已有的知识。")

    results = [r for r in (result.get("results") or []) if isinstance(r, dict)][: max(1, limit)]
    if not results:
        return (f"{head}\n这次搜索没有返回可用的结果。"
                "请不要编造搜索结果或引用具体链接。")

    provider = str(result.get("provider") or "")
    lines = [f"以下是为回答该问题检索到的网络搜索结果（来源：{provider}，共 {len(results)} 条），"
             "仅供参考，可能与播客内容无关或已过时；如需引用请以原文链接为准。",
             head + "："]
    for i, item in enumerate(results, 1):
        title = _text(str(item.get("title") or "")) or "(无标题)"
        url = str(item.get("url") or "")
        snippet = _text(str(item.get("snippet") or ""))
        lines.append(f"{i}. 标题：{title}\n   URL：{url}\n   摘要：{snippet or '（无摘要）'}")
    lines.append("注意：以上结果来自网络检索，可能不准确、不完整或已过时，"
                 "请结合播客原文判断，不要把它当作事实的唯一依据。")
    return "\n".join(lines)
