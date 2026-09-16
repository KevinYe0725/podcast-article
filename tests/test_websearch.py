"""联网搜索（podcast_article.websearch）：provider 选择、HTML 解析、降级、缓存、格式化。

**全部打桩，绝不联网**：所有 HTTP 都走 `monkeypatch` 换掉的 `websearch.requests_get` /
`websearch.requests_post`，断言的也是「发出了什么请求」。页面的解析用真实的
Bing 结构片段（从 cn.bing.com 抓下来再裁掉噪声），见 BING_PAGE 的注释。
"""
from __future__ import annotations

import base64
import json

import pytest

from podcast_article import websearch as ws

# ------------------------------------------------------------------ 夹具

BING_TARGET = "https://www.chooseai.net/news/6933/"

# ---------------- Bing 的 RSS 出口（**主路径**）----------------
# 这一段是从实测响应里裁出来的真结构（本机 2026-09，
# `https://www.bing.com/search?q=…&format=rss&count=8` → 200 / text/xml / 约 4KB / 10 条）：
# channel 里带 title/link/description/image/copyright，然后是一串 <item>，
# item 用 title/link/description 三个子元素。只保留前两条 item，内容原样。
BING_RSS = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>必应：SGLang 朱邦华</title><link>http://www.bing.com:80/search?q=SGLang+%e6%9c%b1%e9%82%a6%e5%8d%8e</link><description>搜索结果</description><image><url>http://www.bing.com:80/s/a/rsslogo.gif</url><title>SGLang 朱邦华</title><link>http://www.bing.com:80/search?q=SGLang+%e6%9c%b1%e9%82%a6%e5%8d%8e</link></image><copyright>版权所有 © 2026 Microsoft</copyright><item><title>GitHub - sgl-project/sglang: SGLang is a high-performance serving ...</title><link>https://github.com/sgl-project/sglang</link><description>SGLang is a high-performance serving framework for large language models and multimodal models.</description></item><item><title>SGLang &amp; 朱邦华：中文文档站</title><link>https://docs.sglang.com.cn/zh/</link><description>朱邦华团队维护的 SGLang 中文文档，含 2 万字符的推理加速说明。</description></item></channel></rss>"""

# 走 HTML 兜底时要用的 RSS：非 XML（风控页）—— `_bing_only` 默认喂这个
BING_RSS_NOT_XML = ("<!DOCTYPE html><html><head><title>找不到页面</title></head>"
                    "<body>我们无法找到你要找的页面。请检查地址是否正确。</body></html>")

# 相关性过滤用的 RSS：「月球大叔／播客」这种长尾词在 Bing 上退化成了「月球」，
# 返回的全是百科条目（标题/摘要里既没有「月球大叔」也没有「播客」）。
BING_RSS_IRRELEVANT = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>必应：月球大叔 播客</title><link>http://www.bing.com:80/search?q=x</link><description>搜索结果</description><item><title>月球（地球唯一的天然卫星）_百度百科</title><link>https://baike.baidu.com/item/%E6%9C%88%E7%90%83/30767</link><description>月球（英文名：Moon，拉丁文：Luna）又称月亮、太阴，是围绕地球旋转的唯一的天然卫星。</description></item><item><title>月球 | NASA中文</title><link>https://www.nasachina.cn/tag/%e6%9c%88%e7%90%83</link><description>画面中展示了多张处于不同程度月食状态下的地球月球图像。</description></item><item><title>中国探月与深空探测网</title><link>http://www.clep.org.cn/</link><description>关于发放月球科研样品的公告 月球样品又有新发现。</description></item></channel></rss>"""

# 部分相关：第一条真的讲「月球大叔」这个播客，后两条又是月球百科。
BING_RSS_PARTIAL = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>必应：月球大叔</title><link>http://www.bing.com:80/search?q=x</link><description>搜索结果</description><item><title>月球大叔 | 小宇宙 - 听播客，上小宇宙</title><link>https://www.xiaoyuzhoufm.com/podcast/67b18c9596e13cadcb525ad5</link><description>在硅谷采访100个有意思的人，这里是月球大叔。</description></item><item><title>月球（地球唯一的天然卫星）_百度百科</title><link>https://baike.baidu.com/item/%E6%9C%88%E7%90%83/30767</link><description>月球又称月亮、太阴，是围绕地球旋转的唯一的天然卫星。</description></item><item><title>月球 | NASA中文</title><link>https://www.nasachina.cn/tag/%e6%9c%88%e7%90%83</link><description>关于月球的图像与科普。</description></item></channel></rss>"""

# 真实结构片段：来自实测 cn.bing.com/search?q=... 的结果页（2026-09，约 98KB），
# 这里保留了**每个<b_algo>块里真正被解析用到的那几个节点**（b_tpcn 站点图标链、
# h2>a 标题、b_caption>p.b_lineclamp2 摘要），其余（内联 css link、favicon 的
# rms_iac 图片、h="ID=SERP,..." 属性）原样保留了一部分，用来验证解析器不会把
# 这些噪声当成标题或摘要。
BING_PAGE = """<!DOCTYPE html><html><head><title>DeepSeek V4 发布 - 搜索</title></head><body>
<ol id="b_results">
<li class="b_algo" data-id iid=SERP.5330><link rel="stylesheet" href="/rp/vlzlb9l5G2c23fVqamzc_M4u4oI.gz.css" type="text/css"/><div class="b_tpcn"><a class="tilk" aria-label="jxxy.net" RedirectUrl="" tabindex="-1" target="_blank" href="https://www.jxxy.net/ai/ai-daily/global-ai-news-20260915/" h="ID=SERP,5143.1"><div class="tpic"><div class="rms_iac" data-src="https://ts1.tc.mm.bing.net/th/id/ODLS.A2450?w=32&amp;h=32&amp;pid=1.2"></div></div></div><div class="tptxt"><div class="tptt">jxxy.net</div><div class="tpmeta"><div class="b_attribution" tabindex="-1"><cite>https://www.jxxy.net &#8250; ai</cite></div></div></div></a></div><h2 class=""><a target="_blank" target="_blank" href="https://www.jxxy.net/ai/ai-daily/global-ai-news-20260915/" h="ID=SERP,5143.2">2026年9月15日全球 AI 动态：<strong>DeepSeek</strong> AI 发布 DeepSeek ...</a></h2><div class="b_caption"><p class="b_lineclamp2" data-rslinkclamp-iid="">今日全球 AI 要闻速览：DeepSeek AI 发布 DeepSeek-V4.1-Flash，Nathan Lambert 评 Nvidia 收购 ...</p></div></li>
<li class="b_algo" data-id iid=SERP.5331><div class="b_tpcn"><a class="tilk" aria-label="chooseai.net" tabindex="-1" target="_blank" href="https://www.chooseai.net/news/6933/" h="ID=SERP,5144.1"><div class="tpic"><div class="rms_iac" data-src="https://ts1.tc.mm.bing.net/th/id/ODLS.A2450BEC-5595?w=32&amp;h=32&amp;pid=1.2"></div></div></div><div class="tptxt"><div class="tptt">chooseai.net</div><div class="tpmeta"><div class="b_attribution" tabindex="-1"><cite>https://www.chooseai.net &#8250; news</cite></div></div></div></a></div><h2 class=""><a target="_blank" target="_blank" href="https://www.chooseai.net/news/6933/" h="ID=SERP,5144.2"><strong>DeepSeek V4</strong>.1 Flash 版本测评：官方19项跑分实测</a></h2><div class="b_caption"><p class="b_lineclamp2">1 天前&ensp;&#0183;&ensp;DeepSeek V4.1 Flash 是深度求索于 2026 年 9 月 10 日正式发布的 V4.1 系列首款模型。</p></div></li>
<li class="b_algo" data-id iid=SERP.5332><div class="b_tpcn"><a class="tilk" href="https://juejin.cn/post/7685253104890101812" target="_blank"><div class="tptxt"><div class="tptt">juejin.cn</div></div></a></div><h2 class=""><a target="_blank" href="https://juejin.cn/post/7685253104890101812" h="ID=SERP,5145.2">DeepSeek V4.1 Flash 免费用实录：权重都开源了</a></h2><div class="b_caption"><p class="b_lineclamp2">一是 DeepSeek 发布了 V4.1 Flash，模型权重直接放上了 Hugging Face，MIT 协议。</p></div></li>
<li class="b_algo" data-id iid=SERP.5333><div class="b_tpcn"><a class="tilk" href="http://go.microsoft.com/fwlink/?LinkID=617350" target="_blank">Microsoft 推广位</a></div><h2 class=""><a target="_blank" href="http://go.microsoft.com/fwlink/?LinkID=617350" h="ID=SERP,5146.2">立刻下载 Edge 浏览器</a></h2><div class="b_caption"><p>广告：这是推广位，不是自然结果。</p></div></li>
<li class="b_algo" data-id iid=SERP.5334><div class="b_tpcn"><a class="tilk" href="https://www.bing.com/aclick?ld=e8XYZ" target="_blank">赞助商</a></div><h2 class=""><a target="_blank" href="https://www.bing.com/aclick?ld=e8XYZ" h="ID=SERP,5147.2">赞助商链接</a></h2><div class="b_caption"><p>广告</p></div></li>
<li class="b_algo" data-id iid=SERP.5335><h2 class=""><a target="_blank" href="/search?q=related" h="ID=SERP,5148.2">站内相关搜索</a></h2><div class="b_caption"><p>相对链接，不是外部来源。</p></div></li>
<li class="b_algo" data-id iid=SERP.5336><div class="b_tpcn"><a class="tilk" href="https://example.org/no-title"></a></div><h2 class=""><a target="_blank" href="https://example.org/no-title"></a></h2><div class="b_caption"><p>空标题的卡片，应当被丢掉。</p></div></li>
<li class="b_algo" data-id iid=SERP.5337><h2 class=""><a target="_blank" href="https://www.ufcn.cn/article/2476200.html"><strong>DeepSeek V4</strong>.1 Flash 发布：原生多模态视觉理解</a></h2><div class="b_caption"><div class="b_lineclamp2">深度求索今日正式发布了全新模型结构系列中的最小尺寸成员——DeepSeekV4.1Flash。</div></div></li>
</ol></body></html>
"""

# Brave 页面结构（v2）。**这一份是从 search.brave.com 的真实响应里裁出来的**
# （2026-09，`?q=DeepSeek V4 发布&source=web`，实测返回 200 / 约 232KB / 20 个
# `data-pos` 块），只做了两件不影响结构的处理：把 favicon 的 base64 图片 URL 截短、
# 只留前两条结果。真实结构要点：
#   * 每条结果是 `<div class="snippet svelte-xxx" data-pos="N" data-type="web">`；
#   * 整页**没有** <h2>/<h3>，标题是 `<div class="title search-snippet-title">`；
#   * `<a href>` 包住站点名+域名+标题，摘要（class 只有 content/generic-snippet，
#     svelte 哈希会变）在 `<a>` **之后**；
#   * 站点展示域名是 `<cite class="snippet-url">zhihu.com › question › 2066…</cite>`，
#     它常常比摘要还长，必须判掉。
BRAVE_PAGE = """<!DOCTYPE html><html><body>
<div id="results" data-pos="0">
<div class="snippet svelte-jmfu5f" data-pos="1" data-type="web" data-keynav="true"><div class="result-body svelte-1rq4ngz"><div class="result-wrapper svelte-1rq4ngz"><div class="result-content svelte-1rq4ngz"><a href="https://www.zhihu.com/question/2066521165246489298" target="_self" class="svelte-14r20fy l1"><div class="site-name-wrapper svelte-on1hvy"><div class="favicon-wrapper svelte-on1hvy"><img src="https://imgs.search.brave.com/VNnFTqCr..." alt="🌐" class="favicon size-m svelte-w2a9kc"/></div> <div class="site-name-content svelte-on1hvy"><div class="desktop-small-semibold t-secondary text-ellipsis">Zhihu</div> <div class="url-wrapper svelte-on1hvy"><cite class="snippet-url desktop-small-regular t-tertiary svelte-on1hvy">zhihu.com <span class="text-ellipsis">› question  › 2066521165246489298</span></cite></div></div></div><!----> <div class="title search-snippet-title line-clamp-1 svelte-14r20fy" title="DeepSeek V4 flash 正式版发布，有哪些亮点值得关注？ - 知乎">DeepSeek V4 flash 正式版发布，有哪些亮点值得关注？ - 知乎</div></a><!----> <div class="generic-snippet svelte-1cwdgg3"><div class="content desktop-default-regular t-primary line-clamp-dynamic svelte-1cwdgg3"><span class="t-secondary">2026年7月30日 -</span> 时间: <strong>2026-07-31</strong>DeepSeek-V4-Flash 更新DeepSeek-V4-Flash 正式版 API 上线公测Agent 能力大幅增强，基…</div></div></div></div></div></div>
<div class="snippet svelte-jmfu5f" data-pos="2" data-type="web" data-keynav="true"><div class="result-body svelte-1rq4ngz"><div class="result-wrapper svelte-1rq4ngz"><div class="result-content svelte-1rq4ngz"><a href="https://zhuanlan.zhihu.com/p/2062944543549416649" target="_self" class="svelte-14r20fy l1"><div class="site-name-wrapper svelte-on1hvy"><div class="site-name-content svelte-on1hvy"><div class="desktop-small-semibold t-secondary text-ellipsis">Zhihu</div> <div class="url-wrapper svelte-on1hvy"><cite class="snippet-url desktop-small-regular t-tertiary svelte-on1hvy">zhuanlan.zhihu.com <span class="text-ellipsis">› p  › 2062944543549416649</span></cite></div></div></div><!----> <div class="title search-snippet-title line-clamp-1 svelte-14r20fy" title="DeepSeek V4「正式版」即将发布！大家有什么期待？ - 知乎">DeepSeek V4「正式版」即将发布！大家有什么期待？ - 知乎</div></a><!----> <div class="generic-snippet svelte-1cwdgg3"><div class="content desktop-default-regular t-primary line-clamp-dynamic svelte-1cwdgg3"><span class="t-secondary">2026年7月21日 -</span> <strong>6月29日，DeepSeek 团队正式宣布 V4 正式版将于7月中旬上线</strong>。此后，腾讯云也发布公告确认 V4 正式版“原厂直供”模型计划于7月中上线。</div> <div class="thumbnail-wrapper svelte-1cwdgg3"><a href="https://zhuanlan.zhihu.com/p/2062944543549416649" class="thumbnail svelte-1yrspal general" target="_self" tabindex="-1"><img src="https://imgs.search.brave.com/mmVB5niB..." alt="" width="112" height="112" class="svelte-1yrspal"/></a></div></div></div></div></div></div>
<div class="snippet svelte-jmfu5f" data-pos="3" data-type="web"><div class="result-wrapper"><div class="result-content"><a href="https://www.bing.com/aclick?ld=ads"><div class="title search-snippet-title">Brave 上的广告位</div></a><div class="generic-snippet"><div class="content">广告，应被过滤。</div></div></div></div></div>
</div>
</body></html>
"""

# Brave 真实页面（**第二次实测存档**：`?q=月球大叔 播客&source=web` → 200 / 约 180KB /
# 21 个结果块）。这一份是整块裁出来的（只截短 favicon 的 base64 图片 URL），
# 用来锁住「结果块是 `<div class="snippet svelte-*" data-pos data-type>`」这条判定。
# 与 BRAVE_PAGE 的区别：真实页面里**整页没有 `id="results"`**，而且类名里含 "snippet"
# 的元素有一大堆（`title search-snippet-title`、`generic-snippet`、
# `snippet-content-wrapper`、页顶的 `<div class="snippet noscript-hide" id="llm-snippet">`），
# 只有带 `data-pos` 的那个才是搜索结果。
BRAVE_REAL_BLOCK = ('<div class="snippet svelte-jmfu5f" data-pos="0" data-type="web" data-keynav="true">'
                    '<div class="result-body svelte-1rq4ngz"><div class="result-wrapper svelte-1rq4ngz">'
                    '<div class="result-content svelte-1rq4ngz"><a href="https://www.xiaoyuzhoufm.com/podcast/67b18c9596e13cadcb525ad6" target="_self" class="svelte-14r20fy l1">'
                    '<div class="site-name-wrapper svelte-on1hvy"><div class="favicon-wrapper svelte-on1hvy">'
                    '<img src="https://imgs.search.brave.com/TRIM..." alt="🌐" class="favicon size-m svelte-w2a9kc"/></div> '
                    '<div class="site-name-content svelte-on1hvy"><div class="desktop-small-semibold t-secondary text-ellipsis">Xiao Yu Zhou</div> '
                    '<div class="url-wrapper svelte-on1hvy"><cite class="snippet-url desktop-small-regular t-tertiary svelte-on1hvy">xiaoyuzhoufm.com '
                    '<span class="text-ellipsis">› podcast  › 67b18c9596e13cadcb525ad6</span></cite></div></div></div><!----> '
                    '<div class="title search-snippet-title line-clamp-1 svelte-14r20fy" title="月球大叔 | 小宇宙 - 听播客，上小宇宙">月球大叔 | 小宇宙 - 听播客，上小宇宙</div></a><!----> '
                    '<div class="generic-snippet svelte-1cwdgg3"><div class="content desktop-default-regular t-primary line-clamp-dynamic svelte-1cwdgg3">'
                    '<span class="t-secondary">2026年6月10日 -</span> <!---->在硅谷采访100个有意思的人，这里是月球大叔。今天我们请到了江鋆晨。'
                    '</div></div></div></div></div></div>')

BRAVE_REAL_PAGE = ('<!DOCTYPE html><html><body><div class="serp-layout svelte-iym1n4">'
                   '<div class="serp-columns-main svelte-k5i0f5">'
                   '<div class="snippet noscript-hide svelte-jmfu5f" id="llm-snippet">'
                   '<div class="title search-snippet-title">页顶的 AI 摘要框（不是结果）</div>'
                   '<div class="content">这是品牌/摘要区块，不带 data-pos。</div></div>'
                   + BRAVE_REAL_BLOCK +
                   '</div></div></body></html>')

# 旧/简化版结构：结果块用 data-pos 标记、标题用 h3。留一份做兼容性回归——
# 万一 Brave 的标题层级改回 h2/h3，或别的地区版本还给 h3，解析不能整页失败。
BRAVE_LEGACY_PAGE = """<!DOCTYPE html><html><body>
<div id="results" data-pos="0">
  <div class="snippet" data-pos="1">
    <a href="https://example.com/brave-one">
      <div class="url">example.com</div>
      <div class="title"><h3>Brave 第一条结果</h3></div>
      <div class="snippet-description">这是 Brave 第一条结果的摘要，含 <b>关键词</b> 高亮。</div>
    </a>
  </div>
  <div class="snippet" data-pos="2">
    <a href="https://example.com/brave-two">
      <div class="url">example.com</div>
      <div class="title"><h3>Brave 第二条结果</h3></div>
      <div class="snippet-description">第二条摘要。</div>
    </a>
  </div>
</div>
</body></html>
"""


class FakeResponse:
    """最小的 requests.Response 替身：status_code / text / headers / json()。"""

    def __init__(self, text: str = "", *, status: int = 200, payload=None,
                 content_type: str = "text/html; charset=utf-8"):
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _clean():
    """每个用例前后清空搜索缓存，互不干扰。"""
    ws.clear_cache()
    yield
    ws.clear_cache()


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch, tmp_path):
    """默认没有任何 key，也不会读到用户真实的 .env（settings.ENV_PATH 指到空文件）。"""
    from podcast_article import settings as st
    monkeypatch.setattr(st, "ENV_PATH", tmp_path / "no-such.env")
    for name in ws.KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PA_SEARCH", raising=False)
    monkeypatch.delenv("PA_SEARCH_PROVIDER", raising=False)


def _stub(monkeypatch, handler):
    """把 websearch 的请求入口换成 handler(method, url, **kwargs)，并记录调用。"""
    calls: list[dict] = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return handler(method, url, **kwargs)

    monkeypatch.setattr(ws, "requests_get", lambda url, **kw: fake("GET", url, **kw))
    monkeypatch.setattr(ws, "requests_post", lambda url, **kw: fake("POST", url, **kw))
    return calls


def _is_bing_rss(call: dict) -> bool:
    """这次请求是不是在打 Bing 的 RSS 出口（`format=rss`）。"""
    params = call.get("params") or {}
    return "bing.com" in str(call.get("url")) and params.get("format") == "rss"


def _bing_only(monkeypatch, html=BING_PAGE, rss=BING_RSS_NOT_XML):
    """Bing 返回 RSS（默认给「非 XML」，于是自动退到 HTML）+ HTML；其他站点一律连接失败。

    这样 `_bing_only(...)` 走的是「RSS 不可用 → HTML 兜底」的真实链路，
    既覆盖了兜底逻辑，又保证测试不依赖真实网络。
    """
    def handler(method, url, **kwargs):
        if "bing.com" in str(url):
            if (kwargs.get("params") or {}).get("format") == "rss":
                return FakeResponse(rss, content_type="text/xml; charset=utf-8")
            return FakeResponse(html)
        raise ws.requests.ConnectionError(f"stub: 不该请求 {url}")
    return _stub(monkeypatch, handler)


def _bing_rss_only(monkeypatch, rss=BING_RSS):
    """只让 Bing 的 RSS 出口返回结果；HTML 出口一律失败（验证主路径优先）。"""
    def handler(method, url, **kwargs):
        if "bing.com" in str(url):
            if (kwargs.get("params") or {}).get("format") == "rss":
                return FakeResponse(rss, content_type="text/xml; charset=utf-8")
            raise ws.requests.ConnectionError("stub: RSS 成功了不该再打 HTML")
        raise ws.requests.ConnectionError(f"stub: 不该请求 {url}")
    return _stub(monkeypatch, handler)


# ------------------------------------------------------------------ available()

def test_available_defaults_to_bing():
    a = ws.available()
    assert a["default"] == "bing", "没有任何 key 时必须默认走免 key 的 bing"
    assert a["enabled"] is True
    assert set(a["providers"]) == set(ws.PROVIDERS)
    for name in ("bing", "brave"):
        assert a["providers"][name]["configured"] is True, "免 key 的永远算已配置"
        assert a["providers"][name]["needs_key"] is False
    for name in ("tavily", "serper"):
        assert a["providers"][name]["needs_key"] is True
        assert a["providers"][name]["configured"] is False
    assert list(ws.SEARCH_ENV)[:2] == ["PA_SEARCH", "PA_SEARCH_PROVIDER"], \
        "SEARCH_ENV 要给设置页复用，前两项是开关与 provider"


def test_available_prefers_configured_tavily(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    a = ws.available()
    assert a["providers"]["tavily"]["configured"] is True
    assert a["default"] == "tavily", "配了 key 的 provider 优先于免 key 的"


def test_available_tavily_beats_serper(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-key")
    assert ws.available()["default"] == "serper"
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    assert ws.available()["default"] == "tavily", "两个都配了时 tavily > serper"


def test_available_respects_forced_provider(monkeypatch):
    monkeypatch.setenv("PA_SEARCH_PROVIDER", "brave")
    assert ws.available()["default"] == "brave", "PA_SEARCH_PROVIDER 指定就跟着变"


def test_available_falls_back_when_forced_provider_has_no_key(monkeypatch):
    monkeypatch.setenv("PA_SEARCH_PROVIDER", "serper")     # 没配 SERPER_API_KEY
    a = ws.available()
    assert a["default"] == "bing", \
        "指定了没配 key 的 provider 要退回免 key 的，否则界面上的默认值永远失败"
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-key")
    assert ws.available()["default"] == "bing", \
        "被迫回退时也只退到免 key 的，不能偷偷换成用户没指定的 tavily"


def test_available_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PA_SEARCH", "0")
    a = ws.available()
    assert a["enabled"] is False
    assert a["providers"]["bing"]["configured"] is True, "关掉联网不影响「已配置」的展示"

    r = ws.search("任意", limit=3)
    assert r["ok"] is False and r["results"] == []
    assert "PA_SEARCH" in r["error"] or "关闭" in r["error"], \
        f"关掉联网时要说清原因，实际 {r['error']}"


def test_available_illegal_env_value_treated_as_enabled(monkeypatch):
    monkeypatch.setenv("PA_SEARCH", "maybe")
    a = ws.available()
    assert a["enabled"] is True, "非法值按开启处理（用户显然是想要联网，猜错方向更糟）"
    r = ws.search("任意", limit=1, provider="bing")
    assert "PA_SEARCH" not in r["error"], f"非法值不该被当成关闭，实际 {r['error']}"


# ------------------------------------------------------------------ Bing 解析

def test_parse_bing_real_structure(monkeypatch):
    """HTML 解析（现在是**兜底**路径）：RSS 不可用时才走它，结构断言保持原样。"""
    calls = _bing_only(monkeypatch)              # 默认让 RSS 返回一段非 XML
    r = ws.search("DeepSeek V4 发布", limit=10)

    assert r["ok"] is True, r["error"]
    assert r["provider"] == "bing"
    assert r["query"] == "DeepSeek V4 发布"
    # 第一次是 RSS（失败），第二次才是 HTML 页
    assert [c["url"] for c in calls] == [ws.BING_RSS_URL, ws.BING_URL]
    assert [c["method"] for c in calls] == ["GET", "GET"]
    assert calls[1]["params"]["q"] == "DeepSeek V4 发布"
    assert "setlang" not in calls[1]["params"], \
        "给 Bing 带 setlang/mkt 会把中文长尾查询的结果打歪（实测），不能加"

    first = r["results"][0]
    # 标题取 <h2> 里的 <a> 文本，实体与标签都清掉；`<strong>` 高亮在剥标签时会留下
    # 一个空格（`动态： DeepSeek`），这是真实输出，不额外处理：机器上实测 Bing 的
    # 标题普遍带高亮，硬收空格会误伤 "DeepSeek V4.1 Flash" 里的真空格。
    assert first["title"] == "2026年9月15日全球 AI 动态： DeepSeek AI 发布 DeepSeek ...", \
        f"实际 {first['title']!r}"
    assert "<" not in first["title"], "标签必须被剥掉"
    assert first["url"] == "https://www.jxxy.net/ai/ai-daily/global-ai-news-20260915/"
    assert "今日全球 AI 要闻速览" in first["snippet"]

    second = next(x for x in r["results"] if x["url"] == BING_TARGET)
    # 原始 HTML 是 `<strong>DeepSeek V4</strong>.1 Flash 版本测评…`，剥标签留下 "V4 .1"
    assert second["title"].startswith("DeepSeek V4 .1 Flash 版本测评"), second["title"]
    # &ensp; / &#0183; 这类实体要还原成空白，不能留在摘要里
    assert "&ensp;" not in second["snippet"] and "&#0183;" not in second["snippet"]
    assert second["snippet"].startswith("1 天前"), f"实际 {second['snippet']!r}"

    # 没有 <p> 的那条（ufcn.cn）退回块内最长的纯文本，而不是空摘要
    sixth = next(x for x in r["results"] if x["url"] == "https://www.ufcn.cn/article/2476200.html")
    assert "原生多模态" in sixth["title"]
    assert "DeepSeekV4.1Flash" in sixth["snippet"], f"实际 {sixth['snippet']!r}"


def test_parse_bing_respects_limit(monkeypatch):
    # 解析器级别的 limit（HTML 兜底路径；RSS 的 limit 在 test_bing_rss_respects_limit）
    for limit in (1, 2, 3):
        assert len(ws._parse_bing(BING_PAGE, limit)) == limit, f"limit={limit}"
    _bing_only(monkeypatch)
    for limit in (1, 2, 3):
        ws.clear_cache()
        r = ws.search("q", limit=limit)
        assert len(r["results"]) == limit, f"limit={limit} 时应只留 {limit} 条"


def test_bing_filters_ads_and_non_http(monkeypatch):
    """广告位（aclick / go.microsoft.com）、站内相对链接、空标题都必须被丢掉。"""
    _bing_only(monkeypatch)
    r = ws.search("q", limit=10)
    urls = [x["url"] for x in r["results"]]
    assert all(u.startswith("https://") for u in urls), urls
    assert not any("aclick" in u or "go.microsoft.com" in u for u in urls), urls
    assert not any("bing.com" in u for u in urls), "站内相对/跳转链接不能混进结果"
    assert not any(x["title"] == "" for x in r["results"])
    assert "example.org/no-title" not in " ".join(urls), "空标题的卡片要丢掉"
    assert len(urls) == 4, f"8 个块里应只剩 4 条自然结果，实际 {urls}"


def test_bing_filters_all_results_gives_error_not_crash(monkeypatch):
    """整页全是广告时不能返回 ok=True 的空结果，也不能抛异常。"""
    page = ('<ol><li class="b_algo"><h2><a href="https://www.bing.com/aclick?ld=x">广告</a></h2>'
            '<p>广告</p></li></ol>')
    def handler(method, url, **kwargs):
        if "bing.com" in str(url):
            if (kwargs.get("params") or {}).get("format") == "rss":
                return FakeResponse(BING_RSS_NOT_XML, content_type="text/xml")
            return FakeResponse(page)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=3, provider="bing")
    assert r["ok"] is False and r["results"] == []
    assert r["error"], "全都过滤掉时要给出原因"


# ------------------------------------------------------------------ 跳转链归一化

def _bing_redirect(target: str, prefix: str = "a1") -> str:
    """造一条 Bing 风格的跳转链：u=a1<base64url(target)>。"""
    b64 = base64.urlsafe_b64encode(target.encode("utf-8")).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=9f3c&u={prefix}{b64}&ntb=1"


@pytest.mark.parametrize("prefix", ["a1", ""])
def test_bing_redirect_decoded(prefix):
    """`u=a1<base64url>` 与老式 `u=<base64>` 都要解出真实 URL。"""
    target = "https://example.com/real/page?x=1&y=中文"
    assert ws._decode_bing_redirect(_bing_redirect(target, prefix)) == target
    assert ws._normalize_url(_bing_redirect(target, prefix)) == target


def test_bing_redirect_percent_encoded():
    """Bing 有时把 u 参数再 percent-encode 一层；也要能解。"""
    target = "https://example.com/a/b"
    b64 = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    url = f"https://www.bing.com/ck/a?u=a1{b64.replace('=', '%3D')}&ntb=1"
    assert ws._decode_bing_redirect(url) == target


def test_bing_redirect_undecodable_is_dropped(monkeypatch):
    """解不出来的跳转链要**丢掉这一条**，而不是塞个 bing.com 链接进去。"""
    assert ws._decode_bing_redirect("https://www.bing.com/ck/a?u=!!!not-base64!!!") is None
    assert ws._normalize_url("https://www.bing.com/ck/a?u=!!!nope!!!") == ""

    good = "https://example.com/good"
    page = (
        '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=!!!nope!!!">坏链接</a></h2>'
        "<p>解析不出的跳转链</p></li>"
        f'<li class="b_algo"><h2><a href="{_bing_redirect(good)}">好链接</a></h2>'
        "<p>能解析</p></li>"
    )
    def handler(method, url, **kwargs):
        if "bing.com" in str(url):
            if (kwargs.get("params") or {}).get("format") == "rss":
                return FakeResponse(BING_RSS_NOT_XML, content_type="text/xml")
            return FakeResponse(page)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=5, provider="bing")
    assert r["ok"] is True
    assert [x["url"] for x in r["results"]] == [good], "只应留下解得出真实地址的那条"


# ------------------------------------------------------------------ Brave 解析

def test_parse_brave_real_structure(monkeypatch):
    """真实 Brave 结构：标题在 .search-snippet-title，摘要在 <a> 之后，广告被过滤。"""
    def handler(method, url, **kwargs):
        if "brave.com" in url:
            return FakeResponse(BRAVE_PAGE)
        raise ws.requests.ConnectionError("stub: 无网络")
    calls = _stub(monkeypatch, handler)

    r = ws.search("q", limit=5, provider="brave")
    assert r["ok"] is True, r["error"]
    assert r["provider"] == "brave"
    assert calls[0]["url"] == ws.BRAVE_URL

    # 广告块（bing.com/aclick）必须在过滤阶段丢掉
    assert [x["url"] for x in r["results"]] == [
        "https://www.zhihu.com/question/2066521165246489298",
        "https://zhuanlan.zhihu.com/p/2062944543549416649",
    ], r["results"]

    first = r["results"][0]
    assert first["title"] == "DeepSeek V4 flash 正式版发布，有哪些亮点值得关注？ - 知乎"
    assert "zhihu.com" not in first["title"], "标题里不能混进站点展示域名"
    assert first["snippet"].startswith("2026年7月30日"), f"实际 {first['snippet']!r}"
    assert "DeepSeek-V4-Flash 正式版 API 上线公测" in first["snippet"]

    second = r["results"][1]
    assert second["title"].startswith("DeepSeek V4「正式版」即将发布")
    assert "腾讯云也发布公告确认" in second["snippet"], second["snippet"]


def test_parse_brave_legacy_h3_structure(monkeypatch):
    """标题层级改回 h3（或别的地区版本）时也要能解析——不能整页失败。"""
    def handler(method, url, **kwargs):
        if "brave.com" in url:
            return FakeResponse(BRAVE_LEGACY_PAGE)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=5, provider="brave")
    assert r["ok"] is True, r["error"]
    assert [x["url"] for x in r["results"]] == ["https://example.com/brave-one",
                                                "https://example.com/brave-two"]
    assert r["results"][0]["title"] == "Brave 第一条结果"
    assert "含 关键词 高亮" in r["results"][0]["snippet"], r["results"][0]["snippet"]


def test_brave_snippet_does_not_repeat_title(monkeypatch):
    """摘要开头重复标题时要去掉标题那一段（真实 Brave 摘要常常这样）。"""
    page = ('<div id="results"><div class="snippet" data-pos="1">'
            '<a href="https://example.com/x"><h3>同一个标题</h3></a>'
            '<div class="generic-snippet"><div class="content">'
            '<p>同一个标题 - 这里才是摘要</p></div></div></div></div>')
    def handler(method, url, **kwargs):
        if "brave.com" in url:
            return FakeResponse(page)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=2, provider="brave")
    assert r["results"][0]["snippet"] == "这里才是摘要", r["results"][0]["snippet"]


def test_brave_falls_back_to_title_divs_without_data_pos(monkeypatch):
    """Brave 改版去掉 data-pos 时，退回「含 h2/h3 的 div 块」也要能解析。"""
    page = ('<div id="results">'
            '<div class="snippet"><a href="https://example.com/n1"><h3>无 data-pos 的标题</h3>'
            '<div class="descr">这是摘要一。</div></a></div>'
            '<div class="snippet"><a href="https://example.com/n2"><h2>第二个标题</h2>'
            '<p>这是摘要二。</p></a></div>'
            '</div>')
    def handler(method, url, **kwargs):
        if "brave.com" in url:
            return FakeResponse(page)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=5, provider="brave")
    assert r["ok"] is True, r["error"]
    assert [x["url"] for x in r["results"]] == ["https://example.com/n1",
                                                "https://example.com/n2"]
    assert r["results"][0]["title"] == "无 data-pos 的标题"
    assert r["results"][0]["snippet"] == "这是摘要一。"
    assert r["results"][1]["title"] == "第二个标题"
    assert r["results"][1]["snippet"] == "这是摘要二。"


def test_offline_never_touches_the_network(monkeypatch):
    """兜底断言：`PA_SEARCH=0` 时**一次请求都不许发**。

    这条同时是「测试不联网」的自检：哪天有人忘了打桩，这里会以「不该请求」炸出来，
    而不是悄悄去打真实搜索站。
    """
    monkeypatch.setenv("PA_SEARCH", "0")
    calls = _stub(monkeypatch, lambda method, url, **kw: pytest.fail(f"不该请求 {url}"))
    r = ws.search("这个查询绝不该真的发出去", limit=1)
    assert r["ok"] is False and r["error"] and calls == []


def test_parse_brave_group_label_replaced_by_snippet(monkeypatch):
    """视频聚合块给的分组标签（`<h4>视频</h4>`）不能当标题——用摘要顶上。

    依据：实测 Brave 的 `data-type="cluster"` 块里，块首是
    `<header><span class="desktop-heading-h4">视频</span></header>`，
    真标题在 <a> 之后。锁住这个行为，避免 regression 成「标题=视频」。
    """
    page = ('<div id="results"><div class="snippet" data-pos="1" data-type="cluster">'
            '<header><span class="desktop-heading-h4 t-secondary">视频</span></header>'
            '<a href="https://www.youtube.com/watch?v=abc"><div class="thumbnail"></div></a>'
            '<div class="video-cluster-grid"><a href="https://www.youtube.com/watch?v=abc">'
            '<h3>真正的视频标题</h3></a><div class="meta">08:30 2026年5月5日</div>'
            '<div class="desc">这是视频的描述文字，比分组标签长得多。</div>'
            '</div></div></div>')
    def handler(method, url, **kwargs):
        if "brave.com" in url:
            return FakeResponse(page)
        raise ws.requests.ConnectionError("stub: 无网络")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=3, provider="brave")
    assert r["ok"] is True, r["error"]
    assert r["results"][0]["title"] != "视频", "分组标签不能当标题"
    assert len(r["results"][0]["title"]) > 4, r["results"][0]["title"]


def test_result_shape_is_stable(monkeypatch):
    """返回值里哪些字段是「永远在」的——webapp / 助手直接读，不能少。"""
    _bing_only(monkeypatch)
    r = ws.search("q", limit=2)
    for field in ("query", "provider", "results", "ok", "error"):
        assert field in r, f"返回结果必须永远带 {field}"
    assert isinstance(r["results"], list) and r["results"]
    for item in r["results"]:
        assert set(item) == {"title", "url", "snippet"}, item
        assert all(isinstance(v, str) for v in item.values())


def test_search_never_raises_for_any_provider_name(monkeypatch):
    """provider 传错名字 / 传 None 都不能炸（调用方是流式回答）。"""
    def handler(method, url, **kwargs):
        raise ws.requests.ConnectionError("boom")
    _stub(monkeypatch, handler)
    for name in ("nope", "", "  ", None, "BING"):
        r = ws.search("q", limit=2, provider=name)
        assert isinstance(r, dict) and r["ok"] in (True, False)


# ------------------------------------------------------------------ Bing RSS（主路径）

def test_parse_bing_rss_real_sample():
    """RSS 解析：喂真实 RSS 样本，断言 title / link / description 三字段。

    样本取自实测响应（见 BING_RSS 的注释）。RSS 的价值在于 link 是**最终 URL**，
    所以这里特意断言「不含 bing.com/ck/a 跳转链」。
    """
    results = ws._parse_bing_rss(BING_RSS, 10)
    assert len(results) == 2, results
    first = results[0]
    assert first["title"] == "GitHub - sgl-project/sglang: SGLang is a high-performance serving ..."
    assert first["url"] == "https://github.com/sgl-project/sglang"
    assert first["snippet"].startswith("SGLang is a high-performance serving framework")
    # 实体还原：样本里的 `&amp;` 应当还原成 `&`
    assert results[1]["title"] == "SGLang & 朱邦华：中文文档站", results[1]["title"]
    assert results[1]["url"] == "https://docs.sglang.com.cn/zh/"
    assert "朱邦华团队维护的 SGLang 中文文档" in results[1]["snippet"]
    assert not any("bing.com" in r["url"] for r in results), "RSS 给的是最终 URL，不该有跳转链"


def test_bing_rss_is_the_primary_path(monkeypatch):
    """RSS 拿到东西就**不该**再去抓 HTML 页（主路径优先）。"""
    calls = _bing_rss_only(monkeypatch)
    r = ws.search("SGLang 朱邦华", limit=2, terms=["sglang"])

    assert r["ok"] is True, r["error"]
    assert r["provider"] == "bing"
    # 两条 item 都含 sglang（标题/摘要），limit=2 都留下；第二条标题里带实体 &amp;
    assert [x["url"] for x in r["results"]] == ["https://github.com/sgl-project/sglang",
                                                "https://docs.sglang.com.cn/zh/"]
    assert len(calls) == 1 and calls[0]["url"] == ws.BING_RSS_URL
    assert calls[0]["params"]["format"] == "rss"
    assert calls[0]["params"]["count"] >= 2, "要多要几条，后面的相关性过滤才有得挑"


def test_bing_rss_respects_limit(monkeypatch):
    _bing_rss_only(monkeypatch)
    for limit in (1, 2):
        ws.clear_cache()
        assert len(ws.search("q", limit=limit, terms=["sglang"])["results"]) == limit


def test_parse_bing_rss_rejects_non_xml_and_empty():
    """非 XML / 空 / 没有 item 都必须抛 _SearchError（由调用方退回 HTML）。"""
    for bad in ("", "   ", BING_RSS_NOT_XML, "<html><body>x</body></html>",
                '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'):
        with pytest.raises(ws._SearchError):
            ws._parse_bing_rss(bad, 5)


def test_bing_falls_back_to_html_when_rss_is_not_xml(monkeypatch):
    """RSS 返回非 XML（风控页/HTML）时自动退回 HTML 解析并标注成功。"""
    def handler(method, url, **kwargs):
        if "bing.com" not in str(url):
            raise ws.requests.ConnectionError("stub: 不该请求这里")
        if (kwargs.get("params") or {}).get("format") == "rss":
            return FakeResponse(BING_RSS_NOT_XML, content_type="text/html; charset=utf-8")
        return FakeResponse(BING_PAGE)
    calls = _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)
    assert r["ok"] is True, r["error"]
    assert r["provider"] == "bing"
    assert len(r["results"]) == 3
    assert [c["url"] for c in calls] == [ws.BING_RSS_URL, ws.BING_URL], \
        "RSS 不可用后必须退到 HTML 页"


@pytest.mark.parametrize("rss_response", [
    lambda: FakeResponse(BING_RSS_NOT_XML, content_type="text/html"),    # 风控页
    lambda: FakeResponse("", status=500),                                # 服务端错误
    lambda: FakeResponse('<?xml version="1.0"?><rss><channel/></rss>',   # 合法 XML 但没结果
                         content_type="text/xml"),
])
def test_bing_fallback_matrix(monkeypatch, rss_response):
    """RSS 的每一种失败（非 XML / HTTP 错 / 无结果）都能退到 HTML，而不是直接失败。"""
    def handler(method, url, **kwargs):
        if "bing.com" not in str(url):
            raise ws.requests.ConnectionError("stub: 不该请求这里")
        if (kwargs.get("params") or {}).get("format") == "rss":
            return rss_response()
        return FakeResponse(BING_PAGE)
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=2)
    assert r["ok"] is True and len(r["results"]) == 2, r["error"]


def test_bing_reports_both_rss_and_html_failures(monkeypatch):
    """两条路都挂时，error 里既要能看到 RSS 的原因也要能看到 HTML 的原因。"""
    def handler(method, url, **kwargs):
        if "bing.com" not in str(url):
            raise ws.requests.ConnectionError("stub: 不该请求这里")
        return FakeResponse("<!DOCTYPE html><html><body>验证码</body></html>")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=2, provider="bing")
    assert r["ok"] is False and r["results"] == []
    assert "RSS" in r["error"] and "HTML" in r["error"], r["error"]


# ------------------------------------------------------------------ 相关性过滤

def test_relevance_filter_drops_unrelated_results(monkeypatch):
    """查询词与结果无关时：ok=False + 说明「没有相关结果」，而不是把垃圾喂给模型。

    样本是实测的「月球大叔 播客」：Bing 把查询退化成了「月球」，返回的全是百科条目。
    """
    _bing_rss_only(monkeypatch, BING_RSS_IRRELEVANT)
    r = ws.search("月球大叔 播客", limit=3, terms=["月球大叔", "播客"])

    assert r["ok"] is False
    assert r["results"] == []
    assert "没有返回与" in r["error"], r["error"]
    assert "月球大叔" in r["error"] and "播客" in r["error"], \
        f"要写清是哪些词没搜到，实际 {r['error']}"


def test_relevance_filter_keeps_only_matching_results(monkeypatch):
    """部分相关时：只保留命中的那些，条数变少但 ok 仍为真。"""
    _bing_rss_only(monkeypatch, BING_RSS_PARTIAL)
    r = ws.search("月球大叔", limit=5, terms=["月球大叔"])

    assert r["ok"] is True, r["error"]
    assert [x["url"] for x in r["results"]] == [
        "https://www.xiaoyuzhoufm.com/podcast/67b18c9596e13cadcb525ad5"], r["results"]


def test_relevance_filter_does_not_use_ngram_fragments(monkeypatch):
    """**关键反例**：结果里有「月球」，但检索词是「月球大叔」——必须被判为不相关。

    用 2-gram 的话 `月球大叔` → `月球` 会命中「月球_百度百科」，垃圾结果会被当成
    与「月球大叔」相关的东西（这正是真机实测到的误判）。判定必须用完整检索词。
    """
    _bing_rss_only(monkeypatch, BING_RSS_IRRELEVANT)     # 三条里全都含「月球」
    r = ws.search("月球大叔", limit=3, terms=["月球大叔"])

    assert r["ok"] is False, f"含「月球」不等于相关，实际 {r['results']}"
    assert r["results"] == []
    assert "月球大叔" in r["error"]


def test_relevance_filter_is_case_insensitive_and_uses_snippet(monkeypatch):
    """大小写不敏感，且标题或摘要任一命中都算相关。"""
    rss = ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
           '<item><title>某个标题</title><link>https://a.example/1</link>'
           '<description>正文里提到了 SGLANG 这个框架。</description></item>'
           '<item><title>无关内容</title><link>https://a.example/2</link>'
           '<description>完全无关。</description></item></channel></rss>')
    _bing_rss_only(monkeypatch, rss)

    r = ws.search("sglang", limit=5, terms=["SGLang"])
    assert r["ok"] is True
    assert [x["url"] for x in r["results"]] == ["https://a.example/1"], \
        "摘要命中也要算相关，且大小写不敏感"


def test_no_terms_means_no_filtering(monkeypatch):
    """不传 terms = 不过滤，现有调用方的行为完全不变（兼容性）。"""
    _bing_rss_only(monkeypatch, BING_RSS_IRRELEVANT)
    r = ws.search("月球大叔 播客", limit=3)
    assert r["ok"] is True and len(r["results"]) == 3

    # 显式传空列表 / 全是单字噪声词，同样等于不过滤
    ws.clear_cache()
    assert ws.search("月球大叔 播客", limit=3, terms=[])["ok"] is True
    ws.clear_cache()
    assert ws.search("月球大叔 播客", limit=3, terms=["月", ""])["ok"] is True


def test_relevance_terms_cleaning():
    assert ws._relevance_terms(None) == []
    assert ws._relevance_terms(["月球大叔", "月球大叔", " 播客 "]) == ["月球大叔", "播客"], \
        "去重保序 + 去空白"
    assert ws._relevance_terms(["a", "ab", ""]) == ["ab"], "长度 <2 的词过滤掉（单字几乎必然命中）"


def test_relevance_filter_falls_through_to_next_provider(monkeypatch):
    """bing 的结果全被过滤掉时，要按「provider 失败」继续试 brave，而不是直接放弃。"""
    def handler(method, url, **kwargs):
        if "bing.com" in str(url):
            if (kwargs.get("params") or {}).get("format") == "rss":
                return FakeResponse(BING_RSS_IRRELEVANT, content_type="text/xml")
            raise ws.requests.ConnectionError("stub: RSS 有返回就不该打 HTML")
        if "brave.com" in str(url):
            return FakeResponse(BRAVE_PAGE)
        raise ws.requests.ConnectionError("stub: 不该请求这里")
    _stub(monkeypatch, handler)

    # terms 用「知乎」：bing 的样本里没有它（全被过滤）→ 换 brave，brave 样本标题里有
    r = ws.search("知乎", limit=3, terms=["知乎"])
    assert r["ok"] is True, r["error"]
    assert r["provider"] == "brave", "bing 没有相关结果时应继续降级"
    assert r["results"], r["results"]


def test_irrelevant_error_wording():
    err = ws._irrelevant_error(["月球大叔", "播客", "第三个"], "月球大叔 播客")
    assert err == "搜索服务没有返回与「月球大叔、播客」相关的结果（可能不支持这个词）", err
    assert "月球大叔 播客" in ws._irrelevant_error([], "月球大叔 播客")


def test_format_for_prompt_shows_irrelevant_reason(monkeypatch):
    """ok=False 时提示词里必须写明「本次未能联网（原因：没有相关结果）」。"""
    _bing_rss_only(monkeypatch, BING_RSS_IRRELEVANT)
    r = ws.search("月球大叔 播客", limit=3, terms=["月球大叔", "播客"])
    text = ws.format_for_prompt(r)
    assert text.strip(), "失败时不能返回空串"
    assert "本次未能联网" in text and "月球大叔" in text
    assert "不要编造" in text


def test_no_language_params_on_bing(monkeypatch):
    """Bing 的请求不能带 setlang/mkt：实测它们会把中文长尾查询结果打歪。"""
    calls = _bing_only(monkeypatch)
    ws.search("SGLang 朱邦华", limit=2)
    assert len(calls) == 2                       # RSS + HTML 兜底
    for c in calls:
        assert "setlang" not in c["params"], c["params"]
        assert "mkt" not in c["params"], c["params"]
        assert c["params"]["q"] == "SGLang 朱邦华"


# ------------------------------------------------------------------ 降级与失败

def test_parse_brave_real_container_class(monkeypatch):
    """真实存档页面：没有 id="results"、类名里一堆含 snippet 的干扰元素。

    锁两件事：(1) 结果块靠 `class 含独立词 snippet + 有 data-pos` 认出来；
    (2) 页顶那个 `<div class="snippet noscript-hide" id="llm-snippet">`（AI 摘要框、
    不含 data-pos）不能被当成搜索结果。
    """
    def handler(method, url, **kwargs):
        if "brave.com" in str(url):
            return FakeResponse(BRAVE_REAL_PAGE)
        raise ws.requests.ConnectionError("stub: bing 先挂了")
    _stub(monkeypatch, handler)

    r = ws.search("月球大叔 播客", limit=5, provider="brave")
    assert r["ok"] is True, r["error"]
    assert [x["url"] for x in r["results"]] == [
        "https://www.xiaoyuzhoufm.com/podcast/67b18c9596e13cadcb525ad6"], r["results"]
    first = r["results"][0]
    assert first["title"] == "月球大叔 | 小宇宙 - 听播客，上小宇宙"
    assert first["snippet"].startswith("2026年6月10日"), first["snippet"]
    assert "AI 摘要框" not in " ".join(x["title"] for x in r["results"]), \
        "页顶不带 data-pos 的 AI 摘要框不是搜索结果"


def test_brave_blocks_requires_data_pos():
    """只有「class 含独立词 snippet 且有 data-pos」的 div 才算结果块。"""
    page = ('<div class="snippet noscript-hide" id="llm-snippet">AI 摘要</div>'
            '<div class="snippet-content-wrapper">包装层</div>'
            '<div class="title search-snippet-title">标题</div>'
            '<div class="snippet" data-pos="1"><a href="https://a.example/1">真结果</a></div>')
    blocks = ws._brave_blocks(page)
    assert len(blocks) == 1, blocks
    assert "真结果" in blocks[0]


def test_brave_rate_limit_is_reported_clearly(monkeypatch):
    """Brave 429 时 error 必须明确写「被限流（429），稍后再试」。"""
    def handler(method, url, **kwargs):
        if "brave.com" in str(url):
            return FakeResponse("", status=429)
        raise ws.requests.ConnectionError("stub: bing 先挂了")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)
    assert r["ok"] is False and r["results"] == []
    assert "429" in r["error"], r["error"]
    assert "限流" in r["error"], f"要明说被限流，实际 {r['error']}"
    assert "稍后再试" in r["error"], r["error"]
    assert "brave" in r["error"], "要分清是哪个源被限流"


def test_brave_consent_wall_without_results_is_an_error(monkeypatch):
    """200 但没有结果块（consent/JS 墙）要报「解析不出结果」，不能 ok=True 空结果。"""
    wall = ("<!DOCTYPE html><html><body><div class='challenge'>"
            "Enable JavaScript and cookies to continue</div></body></html>")
    def handler(method, url, **kwargs):
        if "brave.com" in str(url):
            return FakeResponse(wall)
        raise ws.requests.ConnectionError("stub: bing 先挂了")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)
    assert r["ok"] is False and r["results"] == []


def test_falls_back_to_brave_when_bing_fails(monkeypatch):
    def handler(method, url, **kwargs):
        # Bing 的两条路（RSS 出口 + HTML 页）都挂掉，才轮到 Brave
        if "bing.com" in str(url):
            raise ws.requests.ConnectionError("connection reset by peer")
        if "brave.com" in str(url):
            return FakeResponse(BRAVE_PAGE)
        raise ws.requests.ConnectionError("stub: 不该请求这里")
    calls = _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)
    assert r["ok"] is True, r["error"]
    assert r["provider"] == "brave", "bing 挂了要自动退到 brave，并把实际用上的写进 provider"
    assert len(r["results"]) == 2
    assert [c["url"] for c in calls] == [ws.BING_RSS_URL, ws.BING_URL, ws.BRAVE_URL], \
        f"应当先 bing（RSS→HTML）再 brave，实际 {[c['url'] for c in calls]}"


def test_both_keyless_fail_returns_error_without_raising(monkeypatch):
    def handler(method, url, **kwargs):
        raise ws.requests.ConnectionError("connection reset by peer")
    calls = _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)          # 绝不能抛异常
    assert isinstance(r, dict)
    assert r["ok"] is False
    assert r["results"] == []
    assert r["error"], "失败必须在结果里说明原因（调用方是流式回答）"
    assert "bing" in r["error"] and "brave" in r["error"], r["error"]
    # Bing 会先试 RSS 再试 HTML（两条都记录），然后才是 Brave
    assert len(calls) == 3, f"bing(RSS+HTML) + brave 共 3 次，实际 {len(calls)}"
    assert calls[2]["url"] == ws.BRAVE_URL


def test_specified_keyless_provider_falls_back(monkeypatch):
    """显式指定 bing 时，bing 失败仍应退到 brave（指定的 provider 失败不能直接放弃）。"""
    def handler(method, url, **kwargs):
        if "bing.com" in url:
            raise ws.requests.Timeout("read timeout")
        if "brave.com" in url:
            return FakeResponse(BRAVE_PAGE)
        raise ws.requests.ConnectionError("stub")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=2, provider="bing")
    assert r["provider"] == "brave" and r["ok"] is True


def test_keyed_provider_failure_does_not_guess_other_sites(monkeypatch):
    """key 型 provider 失败（key 无效）不降级到免 key 的——那是用户明确选的路。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-bad")
    def handler(method, url, **kwargs):
        if "tavily" in url:
            return FakeResponse("", status=401)
        raise ws.requests.ConnectionError("stub: 不该请求免 key 站点")
    calls = _stub(monkeypatch, handler)

    r = ws.search("q", limit=3, provider="tavily")
    assert r["ok"] is False and r["results"] == []
    assert "401" in r["error"], r["error"]
    assert len(calls) == 1, "不该替用户去抓网页"


def test_provider_without_key_is_reported(monkeypatch):
    calls = _stub(monkeypatch, lambda *a, **k: FakeResponse(BING_PAGE))
    r = ws.search("q", limit=2, provider="serper")
    assert r["ok"] is False
    assert "SERPER_API_KEY" in r["error"], r["error"]
    assert calls == [], "没 key 时不该发出任何请求"


def test_http_error_and_garbage_html_are_reported(monkeypatch):
    def handler(method, url, **kwargs):
        if "bing.com" in url:
            return FakeResponse("<html><body>验证码</body></html>")
        if "brave.com" in url:
            return FakeResponse("", status=429)
        raise ws.requests.ConnectionError("stub")
    _stub(monkeypatch, handler)

    r = ws.search("q", limit=3)
    assert r["ok"] is False and r["results"] == []
    assert "429" in r["error"], r["error"]
    assert "解析" in r["error"], f"页面改版也要能说清，实际 {r['error']}"


def test_timeout_is_split_and_headers_are_browser_like(monkeypatch):
    calls = _bing_only(monkeypatch)
    ws.search("q", limit=2, timeout=7.5)
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, tuple) and timeout[0] == ws.CONNECT_TIMEOUT and timeout[1] == 7.5, \
        f"超时要拆成 (连接, 读取) 且读取用调用方的值，实际 {timeout}"
    headers = calls[0]["headers"]
    assert "User-Agent" in headers and "Mozilla" in headers["User-Agent"], \
        "默认 UA 会被搜索站降级/挡住"
    assert "Accept-Language" in headers and headers["Accept-Language"].startswith("zh"), \
        "要带 Accept-Language，否则 cn.bing.com 可能返回英文结果"


def test_empty_query_and_bad_limit_are_safe(monkeypatch):
    calls = _bing_rss_only(monkeypatch)
    r = ws.search("   ", limit=3)
    assert r["ok"] is False and "查询词为空" in r["error"] and calls == []

    r2 = ws.search("q", limit=0)                 # limit 非法值不能把请求打成 limit=0
    assert r2["ok"] is True and len(r2["results"]) >= 1
    assert calls[0]["params"]["q"] == "q"


# ------------------------------------------------------------------ key 型 provider

def test_tavily_uses_bearer_key_and_parses_json(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    payload = {"results": [
        {"title": "T1", "url": "https://a.example/1", "content": "摘要一"},
        {"title": "T2", "url": "https://a.example/2", "content": "摘要二"},
        {"title": "T3", "url": "https://a.example/3", "content": "摘要三"},
        {"title": "", "url": "https://a.example/4", "content": "空标题要丢"},
    ]}
    calls = _stub(monkeypatch, lambda *a, **k: FakeResponse("", payload=payload))

    r = ws.search("q", limit=2, provider="tavily")
    assert r["ok"] is True and r["provider"] == "tavily"
    assert [x["title"] for x in r["results"]] == ["T1", "T2"], "要受 limit 限制"
    assert calls[0]["url"] == ws.TAVILY_URL and calls[0]["method"] == "POST"
    assert calls[0]["headers"]["Authorization"] == "Bearer tvly-secret", \
        "key 只能用 Bearer 头传，不能写进 URL（会进日志）"
    assert "tvly-secret" not in json.dumps(calls[0].get("params") or {}, ensure_ascii=False)
    assert json.loads(calls[0]["data"].decode("utf-8"))["query"] == "q"


def test_serper_uses_api_key_header_and_parses_organic(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "serper-secret")
    payload = {"organic": [
        {"title": "S1", "link": "https://b.example/1", "snippet": "摘要一"},
        {"title": "S2", "link": "https://b.example/2", "snippet": "摘要二"},
    ], "ads": [{"title": "广告", "link": "https://ad.example"}]}
    calls = _stub(monkeypatch, lambda *a, **k: FakeResponse("", payload=payload))

    r = ws.search("q", limit=5, provider="serper")
    assert r["ok"] is True and r["provider"] == "serper"
    assert [x["title"] for x in r["results"]] == ["S1", "S2"], "只取 organic，不取 ads"
    assert calls[0]["url"] == ws.SERPER_URL
    assert calls[0]["headers"]["X-API-KEY"] == "serper-secret"


def test_garbage_json_from_keyed_provider_is_safe(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret")
    _stub(monkeypatch, lambda *a, **k: FakeResponse("<html>502 Bad Gateway</html>"))
    r = ws.search("q", limit=3, provider="tavily")
    assert r["ok"] is False and r["results"] == [] and r["error"]


# ------------------------------------------------------------------ 缓存

def test_cache_hits_once_within_ttl(monkeypatch):
    calls = _bing_rss_only(monkeypatch)
    a = ws.search("同一个问题", limit=3)
    b = ws.search("同一个问题", limit=3)
    assert a == b
    assert len(calls) == 1, f"10 分钟内同一个 query 只该发一次请求，实际 {len(calls)}"

    # 大小写/空白归一：也算同一个 query（`AI` 与 `ai`）
    ws.search("同一个问题 ", limit=3)
    ws.search("同 一个 问题", limit=3)
    assert len(calls) == 2, f"相差空格的算不同查询，实际 {len(calls)}"
    assert len([c for c in calls if c["params"]["q"] == "同一个问题"]) == 1


def test_cache_cleared_by_clear_cache(monkeypatch):
    calls = _bing_rss_only(monkeypatch)
    ws.search("q", limit=3)
    ws.clear_cache()
    ws.search("q", limit=3)
    assert len(calls) == 2, "clear_cache() 后必须重新请求"


def test_failed_search_is_not_cached(monkeypatch):
    """失败的查询不进缓存：下一轮网络恢复了就该能搜到。"""
    state = {"n": 0}
    def handler(method, url, **kwargs):
        state["n"] += 1
        if "bing.com" not in str(url):
            raise ws.requests.ConnectionError("stub: 不该请求这里")
        if state["n"] == 1:                      # 第一次：RSS 与 HTML 都挂
            raise ws.requests.ConnectionError("temporary")
        if (kwargs.get("params") or {}).get("format") == "rss":
            return FakeResponse(BING_RSS, content_type="text/xml; charset=utf-8")
        raise ws.requests.ConnectionError("stub: RSS 好使了就不该再打 HTML")
    _stub(monkeypatch, handler)

    first = ws.search("q", limit=3)
    assert first["ok"] is False
    second = ws.search("q", limit=3)
    assert second["ok"] is True and second["provider"] == "bing"


def test_cache_is_not_shared_across_limit(monkeypatch):
    calls = _bing_rss_only(monkeypatch)
    ws.search("q", limit=1)
    ws.search("q", limit=3)
    assert len(calls) == 2, "条数不同的两次查询不能互相复用"


def test_cached_result_is_a_copy(monkeypatch):
    """调用方改了返回的 list 不该污染缓存。"""
    _bing_rss_only(monkeypatch)          # 这份 RSS 样本有 2 条 item
    a = ws.search("q", limit=2)
    a["results"].clear()
    b = ws.search("q", limit=2)
    assert len(b["results"]) == 2


# ------------------------------------------------------------------ format_for_prompt

def test_format_for_prompt_ok():
    result = {"query": "DeepSeek V4", "provider": "bing", "ok": True, "error": "", "results": [
        {"title": "标题一", "url": "https://a.example/1", "snippet": "摘要一"},
        {"title": "标题二", "url": "https://a.example/2", "snippet": "摘要二"},
    ]}
    text = ws.format_for_prompt(result)
    assert "1. " in text and "2. " in text, "每条要有序号"
    for bit in ("标题一", "https://a.example/1", "摘要一", "标题二", "https://a.example/2"):
        assert bit in text, f"缺少 {bit}：\n{text}"
    assert "仅供参考" in text, "必须明确标注这是网络搜索结果，仅供参考"
    assert "过时" in text or "不准确" in text
    assert "bing" in text, "要写清来源 provider"


def test_format_for_prompt_respects_limit():
    result = {"query": "q", "provider": "bing", "ok": True, "error": "",
              "results": [{"title": f"T{i}", "url": f"https://a.example/{i}", "snippet": "s"}
                          for i in range(6)]}
    text = ws.format_for_prompt(result, limit=2)
    assert "T0" in text and "T1" in text and "T2" not in text


def test_format_for_prompt_failure_mentions_it():
    text = ws.format_for_prompt({"query": "q", "provider": "bing", "ok": False,
                                 "error": "连接失败（网络不通或被挡）", "results": []})
    assert text.strip(), "失败时不能返回空串，否则模型会以为「搜了但没有相关信息」"
    assert "未能联网" in text
    assert "连接失败" in text, "要把原因带给模型"
    assert "不要编造" in text


def test_format_for_prompt_never_raises():
    assert ws.format_for_prompt({}) == ""
    assert ws.format_for_prompt({"query": "q"})     # 字段缺失也不能抛
    assert ws.format_for_prompt({"query": "q", "ok": True, "results": None})
