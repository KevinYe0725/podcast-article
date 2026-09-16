"""播客订阅存储 + 轮询：add/check/discover/remove/update/due/snapshot。

所有测试都跑在临时 feeds.json 上，并且把 `feeds._fetch` 换成假解析器，
断言里带一个「从未真的联网」的调用计数 —— 一旦有人绕过 _fetch 就会失败。
"""
from __future__ import annotations

import importlib
import time

import feedparser
import pytest

from podcast_article import feeds

# ------------------------------------------------------------------ 测试装置

RSS_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
    "<channel>\n"
    "<title>测试电台</title>\n"
    "<link>https://example.com/podcast</link>\n"
)


def _pub_date(day: int) -> str:
    """2024-03-<day> 00:00:00 GMT（RFC-822，feedparser 能解析）。"""
    return f"Mon, {day:02d} Mar 2024 00:00:00 GMT"


def rss(items: list[dict], title: str = "测试电台") -> str:
    """造一段真实可解析的最小 RSS，items 里每项对应 feed 里的一集（顺序 = feed 顺序）。

    item 支持 guid/link/title/day/enclosure。默认按标题生成稳定的 guid/link，
    这样「同一集在不同顺序的 feed 里」仍然是同一集（真实站点也是这个行为）；
    需要造出真正没有标识的 entry 时传 guid=None / link=None。
    """
    body = [RSS_HEAD.replace("测试电台", title, 1)]
    for i, item in enumerate(items, start=1):
        title_i = item.get("title", f"第 {i} 集")
        key = f"ep{i}" if not item.get("title") else "".join(
            ch if ch.isalnum() else "-" for ch in title_i
        ).strip("-") or f"ep{i}"
        guid = item.get("guid", f"guid-{key}")
        link = item.get("link", f"https://example.com/{key}")
        day = item.get("day", i)
        audio = item.get("enclosure", f"https://example.com/{key}.mp3")
        parts = [f"<title>{title_i}</title>"]
        if guid is not None:
            parts.append(f"<guid>{guid}</guid>")
        if link is not None:
            parts.append(f"<link>{link}</link>")
        if day is not None:
            parts.append(f"<pubDate>{_pub_date(day)}</pubDate>")
        if audio:
            parts.append(f'<enclosure url="{audio}" type="audio/mpeg" length="1"/>')
        body.append("<item>" + "".join(parts) + "</item>\n")
    body.append("</channel>\n</rss>\n")
    return "".join(body)


class FakeFeed:
    """一个可替换内容的假 feed：obj["content"] 换成新 RSS 就等于 feed 更新了。"""

    def __init__(self, content: str):
        self.content = content
        self.calls: list[tuple[str, float]] = []
        self.error: Exception | None = None

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        setattr(self, key, value)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """feeds.json 落到 tmp_path，并给一条空订阅表。"""
    monkeypatch.setenv("PA_FEEDS_FILE", str(tmp_path / "feeds.json"))
    monkeypatch.setattr(feeds, "FEEDS_PATH", tmp_path / "feeds.json")


@pytest.fixture()
def fake_feed(monkeypatch):
    """把网络出口收敛到假解析器上（挂一个空 feed）。"""
    holder = FakeFeed(rss([]))

    def fake_fetch(url, timeout=25.0):
        holder.calls.append((url, timeout))
        if holder.error is not None:
            raise holder.error
        return feedparser.parse(holder.content)

    monkeypatch.setattr(feeds, "_fetch", fake_fetch)
    return holder


FEED_URL = "https://example.com/feed.xml"


def _assert_never_online(holder: FakeFeed) -> None:
    """假解析器至少被调用过一次 —— 说明代码走的是被替换掉的 _fetch，没有真的联网。"""
    assert holder.calls, "没有走 _fetch，测试可能真的联网了"


# --------------------------------------------------------------- _fetch 本体
# 下面这组测试专门覆盖**真实的** _fetch（上面的 fixture 都把它整个换掉了，
# 于是它的实现细节没人验证 —— 实测就是这么漏掉一个致命 bug 的）。


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise feeds.requests.HTTPError(f"{self._status} Client Error")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


def test_fetch_uses_requests_with_timeout_and_parses_bytes(monkeypatch):
    """_fetch 必须：带超时地请求、把响应体交给 feedparser。

    回归测试：曾经写成 feedparser.parse(url, request_timeout=...) —— feedparser
    根本没有 request_timeout 这个参数，一调用就 TypeError，导致**订阅功能整体不可用**；
    而当时所有测试都把 _fetch 整个替换掉了，所以谁都没发现。
    """
    seen: dict = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["timeout"] = timeout
        return _FakeResponse(rss([{"title": "第一集", "guid": "g1"}]).encode("utf-8"))

    monkeypatch.setattr(feeds.requests, "get", fake_get)
    parsed = feeds._fetch(FEED_URL, timeout=7.0)

    assert seen["url"] == FEED_URL, f"应当直接请求传入的 url，实际 {seen}"
    assert seen["timeout"] is not None, "必须设置超时，否则会永久挂住"
    # (连接超时, 读取超时) 元组；读取超时用调用方给的值
    assert isinstance(seen["timeout"], tuple) and seen["timeout"][1] == 7.0, \
        f"超时应当带上调用方给的值，实际 {seen['timeout']}"
    assert "User-Agent" in seen["headers"], "应当带 User-Agent，否则部分站点会拒绝"
    assert [e["title"] for e in parsed.entries] == ["第一集"], \
        f"响应体应当被解析成 feed，实际 {parsed.entries}"


def test_fetch_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        feeds.requests, "get",
        lambda url, headers=None, timeout=None: _FakeResponse(b"nope", status=404),
    )
    with pytest.raises(feeds.requests.HTTPError):
        feeds._fetch(FEED_URL)


def test_add_propagates_wrapped_error_when_fetch_fails(monkeypatch):
    """真实 _fetch 出错时，add() 要给出可读的 ValueError（而不是把 HTTPError 抛到界面）。"""
    def boom(url, headers=None, timeout=None):
        raise feeds.requests.ConnectionError("connection reset by peer")

    monkeypatch.setattr(feeds.requests, "get", boom)
    with pytest.raises(ValueError) as exc:
        feeds.add(FEED_URL)
    assert "connection reset" in str(exc.value).lower() or "抓取" in str(exc.value), \
        f"错误信息里应能看出是抓取失败，实际 {exc.value}"


def _both_feeds(holder: FakeFeed, a_id: str):
    """两个 feed 用不同内容（A 有更新、B 也更新），验证 check() 会逐个抓取。"""

    def fake_fetch(url, timeout=25.0):
        holder.calls.append((url, timeout))
        if a_id and url.endswith("a.xml"):
            return feedparser.parse(
                rss([{"title": "A 台新集", "day": 4}, {"title": "A 台老集", "day": 1}])
            )
        return feedparser.parse(
            rss([{"title": "B 台新集", "day": 2}, {"title": "B 台老集", "day": 1}])
        )

    return fake_fetch


# ------------------------------------------------------------------ _entry_id 优先级

def test_entry_id_priority_on_real_parsed_entries():
    """guid / link 缺哪一层就退到下一层（用真实解析出来的 entry 断言）。"""
    parsed = feedparser.parse(
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>t</title>"
        "<item><title>有 guid 也有 link</title><guid>g-1</guid>"
        "<link>https://example.com/g</link></item>"
        "<item><title>只有 link</title><link>https://example.com/l</link></item>"
        "<item><title>只有标题</title></item>"
        "</channel></rss>"
    )
    assert feeds._entry_id(parsed.entries[0]) == "g-1"                    # id/guid 优先
    assert feeds._entry_id(parsed.entries[1]) == "https://example.com/l"  # 其次 link
    assert feeds._entry_id(parsed.entries[2]) == "title:只有标题"          # 最后只剩标题


def test_entry_id_uses_title_plus_published_then_prefixes():
    """title + published 是倒数第二档；连日期都没有时才用 title: 前缀。"""
    base = {"title": "第 1 集", "published": "Mon, 04 Mar 2024 00:00:00 GMT"}
    assert feeds._entry_id(base) == "第 1 集|Mon, 04 Mar 2024 00:00:00 GMT"
    assert feeds._entry_id({"title": "第 1 集"}) == "title:第 1 集"
    assert feeds._entry_id({}) == ""


def test_entry_id_atom_id_wins_over_link():
    entry = {"id": "tag:example.com,2024:1", "guid": None, "link": "https://example.com/1"}
    assert feeds._entry_id(entry) == "tag:example.com,2024:1"


def test_title_only_entry_is_seen_once_and_not_queued_twice(fake_feed, monkeypatch):
    """连 guid/link 都没有的 entry 退化成 title: 标识，仍然只报一次。"""
    content = (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        "<title>测试电台</title>"
        "<item><title>没有任何标识的一集</title></item>"
        "<item><title>有标识的一集</title><guid>guid-已见</guid>"
        '<enclosure url="https://example.com/1.mp3" type="audio/mpeg"/></item>'
        "</channel></rss>"
    )
    monkeypatch.setattr(
        feeds, "_fetch", lambda url, timeout=25.0: feedparser.parse(content)
    )
    feed = feeds.add(FEED_URL)
    assert feed["seen"] == ["title:没有任何标识的一集", "guid-已见"]
    assert feeds.check(feed["id"]) == []
    assert feeds.check(feed["id"]) == []


# ------------------------------------------------------------------ add / check

def test_add_records_title_and_marks_all_episodes_seen(fake_feed):
    fake_feed["content"] = rss([{"title": "第 3 集"}, {"title": "第 2 集"}, {"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    _assert_never_online(fake_feed)

    assert feed["id"].startswith("f")
    assert feed["title"] == "测试电台"          # 订阅时抓到的节目名
    assert feed["url"] == FEED_URL
    assert feed["auto"] is True
    assert feed["backfill"] == 0
    assert feed["last_checked"] is None
    assert feed["last_error"] == ""
    assert feed["added_at"] <= time.time()
    assert len(feed["seen"]) == 3
    assert feeds.get(feed["id"])["seen"] == feed["seen"]

    # 紧接着 check()：feed 里没有变化，不该报任何新单集
    assert feeds.check() == []
    _assert_never_online(fake_feed)


def test_add_with_explicit_title_keeps_it(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL, title="我给的名字", auto=False)
    assert feed["title"] == "我给的名字"
    assert feed["auto"] is False


def test_check_reports_new_episode_with_pick(fake_feed):
    fake_feed["content"] = rss([{"title": "老集"}, {"title": "更老的一集"}])
    feed = feeds.add(FEED_URL)
    assert feeds.check() == []

    # feed 里多了一集（最新），pick 应该是 1
    fake_feed["content"] = rss(
        [{"title": "新集", "day": 5}, {"title": "老集", "day": 4}, {"title": "更老的一集", "day": 3}]
    )
    new = feeds.check()
    assert len(new) == 1
    item = new[0]
    assert item["feed_id"] == feed["id"]
    assert item["feed_title"] == "测试电台"
    assert item["feed_url"] == FEED_URL
    assert item["title"] == "新集"
    assert item["pick"] == 1
    assert item["episode_url"] == "https://example.com/新集"
    assert item["audio_url"] == "https://example.com/新集.mp3"
    assert item["pub_date"] == "2024-03-05T00:00:00Z"

    # 已标记 seen：再抓一次不该重复报
    assert feeds.check() == []
    assert feeds.check(feed["id"]) == []


def test_check_returns_oldest_first_and_pick_desc_within_feed(fake_feed):
    """一个 feed 里冒出的多集：按时间旧→新返回（补跑历史用），pick 是抓取那刻的位置。"""
    fake_feed["content"] = rss([{"title": "老集", "day": 1}])
    feed = feeds.add(FEED_URL)

    fake_feed["content"] = rss(
        [
            {"title": "最新", "day": 6},
            {"title": "中间", "day": 5},
            {"title": "老集", "day": 1},
        ]
    )
    new = feeds.check(feed["id"])
    assert [x["title"] for x in new] == ["中间", "最新"]
    assert [x["pick"] for x in new] == [2, 1]
    assert [x["pub_date"] for x in new] == ["2024-03-05T00:00:00Z", "2024-03-06T00:00:00Z"]


def test_backfill_two_queues_latest_two_oldest_first(fake_feed):
    fake_feed["content"] = rss(
        [
            {"title": "第 4 集", "day": 4},
            {"title": "第 3 集", "day": 3},
            {"title": "第 2 集", "day": 2},
            {"title": "第 1 集", "day": 1},
        ]
    )
    feed = feeds.add(FEED_URL, backfill=2)
    assert feed["backfill"] == 2
    # 最新两集（pick 1、2）被从 seen 里拿掉；更老的仍然算已处理
    assert len(feed["seen"]) == 2

    new = feeds.check(feed["id"])
    assert [x["title"] for x in new] == ["第 3 集", "第 4 集"]       # 旧 → 新
    assert [x["pick"] for x in new] == [2, 1]
    assert feeds.check(feed["id"]) == []                            # 不重复入队


def test_backfill_more_than_feed_marks_nothing_seen(fake_feed):
    fake_feed["content"] = rss([{"title": "第 2 集", "day": 2}, {"title": "第 1 集", "day": 1}])
    feed = feeds.add(FEED_URL, backfill=9)
    assert feed["seen"] == []
    assert [x["title"] for x in feeds.check(feed["id"])] == ["第 1 集", "第 2 集"]


def test_check_multiple_feeds_merges_by_date(fake_feed, monkeypatch):
    fake_feed["content"] = rss([{"title": "A 台老集", "day": 1}])
    a = feeds.add("https://example.com/a.xml")
    fake_feed["content"] = rss([{"title": "B 台老集", "day": 1}])
    b = feeds.add("https://example.com/b.xml")

    # 两个 feed 各冒出一集，按 pub_date 从旧到新合并返回
    monkeypatch.setattr(feeds, "_fetch", _both_feeds(fake_feed, a["id"]))
    both = feeds.check()

    assert [(x["title"], x["feed_id"]) for x in both] == [
        ("B 台新集", b["id"]),
        ("A 台新集", a["id"]),
    ]
    assert [x["pick"] for x in both] == [1, 1]
    assert feeds.check() == []                                    # 都已标记 seen


# ------------------------------------------------------------------ 失败容错

def test_check_swallows_failure_and_records_error(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    assert feeds.get(feed["id"])["last_checked"] is None

    fake_feed["error"] = RuntimeError("网络不可达")
    assert feeds.check() == []                                   # 不抛异常
    stored = feeds.get(feed["id"])
    assert "网络不可达" in stored["last_error"]
    assert stored["last_checked"] is None                        # 失败不更新时间
    assert stored["seen"] == feed["seen"]                        # 也不动 seen

    # 恢复后照常工作，并且 last_error 被清掉
    fake_feed["error"] = None
    fake_feed["content"] = rss([{"title": "新集", "day": 9}, {"title": "第 1 集", "day": 1}])
    new = feeds.check()
    assert [x["title"] for x in new] == ["新集"]
    stored = feeds.get(feed["id"])
    assert stored["last_error"] == ""
    assert stored["last_checked"] is not None


def test_check_skips_malformed_feed_without_killing_others(fake_feed, monkeypatch):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    good = feeds.add("https://example.com/good.xml")
    bad = feeds.add("https://example.com/bad.xml")

    def per_url_fetch(url, timeout=25.0):
        fake_feed["calls"].append((url, timeout))
        fake_feed["timeout"] = timeout
        if url.endswith("bad.xml"):
            return feedparser.parse('<?xml version="1.0"?><rss><channel><title>坏掉了')
        return feedparser.parse(
            rss([{"title": "新集", "day": 3}, {"title": "第 1 集", "day": 1}])
        )

    monkeypatch.setattr(feeds, "_fetch", per_url_fetch)  # 依然是零联网的假解析器
    new = feeds.check(timeout=7.0)
    assert [x["feed_id"] for x in new] == [good["id"]]            # 坏 feed 被跳过
    assert "RSS 解析失败" in feeds.get(bad["id"])["last_error"]
    assert feeds.get(bad["id"])["last_checked"] is None
    assert "RSS 解析失败" not in feeds.get(good["id"])["last_error"]
    assert fake_feed["timeout"] == 7.0                            # 超时参数透传到 _fetch


def test_check_unknown_feed_raises(fake_feed):
    with pytest.raises(ValueError, match="订阅不存在"):
        feeds.check("fnope")


def test_check_forwards_timeout_to_fetch(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feeds.add(FEED_URL, timeout=3.5)
    assert fake_feed.calls[-1] == (FEED_URL, 3.5)
    feeds.check(timeout=9.0)
    assert fake_feed.calls[-1] == (FEED_URL, 9.0)


def test_add_and_discover_reject_bad_backfill_and_limit(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    with pytest.raises(ValueError, match="不能为负数"):
        feeds.add(FEED_URL, backfill=-1)
    with pytest.raises(ValueError, match="必须是整数"):
        feeds.add(FEED_URL, backfill="两集")
    assert feeds.load()["feeds"] == []                         # 参数不合法就不落盘
    assert feeds.discover(FEED_URL, limit=0)["episodes"] == []
    assert len(feeds.discover(FEED_URL, limit="乱写")["episodes"]) == 1  # 退化成默认 5


# ------------------------------------------------------------------ 环境变量覆盖

def test_path_can_be_overridden_by_env(tmp_path, monkeypatch):
    """FEEDS_PATH 可用 PA_FEEDS_FILE 覆盖（测试才敢跑在临时文件上）。"""
    target = tmp_path / "sub" / "feeds.json"
    monkeypatch.setenv("PA_FEEDS_FILE", str(target))
    reloaded = importlib.reload(feeds)
    try:
        assert reloaded.FEEDS_PATH == target
        assert reloaded.load() == {"feeds": []}
    finally:
        monkeypatch.undo()
        importlib.reload(feeds)


# ------------------------------------------------------------------ discover

def test_discover_previews_before_subscribing(fake_feed):
    fake_feed["content"] = rss(
        [{"title": f"第 {i} 集", "day": 9 - i} for i in range(1, 8)]
    )
    out = feeds.discover(FEED_URL)
    _assert_never_online(fake_feed)
    assert out["title"] == "测试电台"
    assert out["count"] == 7                                      # feed 里单集总数
    assert [e["index"] for e in out["episodes"]] == [1, 2, 3, 4, 5]  # index 是 1-based pick
    assert out["episodes"][0]["title"] == "第 1 集"
    assert out["episodes"][0]["pub_date"] == "2024-03-08T00:00:00Z"

    limited = feeds.discover(FEED_URL, limit=2)
    assert [e["index"] for e in limited["episodes"]] == [1, 2]
    assert limited["count"] == 7

    assert feeds.load()["feeds"] == []                            # 预览不落盘


def test_discover_rejects_non_feed(fake_feed):
    fake_feed["content"] = "这不是一个 feed"
    with pytest.raises(ValueError, match="RSS 解析失败"):
        feeds.discover(FEED_URL)
    with pytest.raises(ValueError, match="RSS 解析失败"):
        feeds.add(FEED_URL)
    assert feeds.load()["feeds"] == []


def test_discover_rejects_empty_feed(fake_feed):
    fake_feed["content"] = rss([])
    with pytest.raises(ValueError, match="没有任何单集"):
        feeds.discover(FEED_URL)


def test_add_reports_network_error(fake_feed):
    fake_feed["error"] = ValueError("RSS 解析失败：Connection refused")
    with pytest.raises(ValueError, match="Connection refused"):
        feeds.add(FEED_URL)
    assert feeds.load()["feeds"] == []


# ------------------------------------------------------------------ remove / update

def test_add_duplicate_url_raises(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feeds.add(FEED_URL)
    with pytest.raises(ValueError, match="已经订阅过这个节目：测试电台"):
        feeds.add(FEED_URL)                                       # 完全相同的链接
    with pytest.raises(ValueError, match="已经订阅过"):
        feeds.add("https://example.com/feed.xml/")                # 归一化后视为同一个
    assert len(feeds.load()["feeds"]) == 1


def test_add_rejects_empty_url(fake_feed):
    with pytest.raises(ValueError, match="不能为空"):
        feeds.add("   ")
    assert fake_feed.calls == []                                  # 空链接不该发起请求


def test_remove_unknown_id_raises(fake_feed):
    with pytest.raises(ValueError, match="订阅不存在"):
        feeds.remove("fnope")

    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    feeds.remove(feed["id"])
    assert feeds.get(feed["id"]) is None
    assert feeds.load()["feeds"] == []
    with pytest.raises(ValueError, match="订阅不存在"):
        feeds.remove(feed["id"])


def test_update_only_whitelisted_fields(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    before = feeds.get(feed["id"])

    updated = feeds.update(
        feed["id"],
        auto=False,
        backfill=3,
        title="改过的名字",
        url="https://evil.example.com/feed.xml",
        id="fdeadbeef",
        seen=[],
        last_error="乱写的",
    )
    assert updated["auto"] is False
    assert updated["backfill"] == 3
    assert updated["title"] == "改过的名字"
    assert updated["url"] == FEED_URL                             # 白名单之外一律忽略
    assert updated["id"] == feed["id"]
    assert updated["seen"] == before["seen"]
    assert updated["last_error"] == ""

    with pytest.raises(ValueError, match="订阅不存在"):
        feeds.update("fnope", auto=True)

    with pytest.raises(ValueError, match="必须是整数"):
        feeds.update(feed["id"], backfill="两集")


# ------------------------------------------------------------------ due

def test_due_uses_interval_and_injected_now(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    first = feeds.add(FEED_URL)
    fake_feed["content"] = rss([{"title": "另一台的第 1 集"}])
    second = feeds.add("https://example.com/b.xml")

    # 从未检查过 → 都 due（用注入的 now，不 sleep）
    assert feeds.due() == [first["id"], second["id"]]
    assert feeds.due(now=1_000.0) == [first["id"], second["id"]]

    # 直接把 last_checked 写成可控的时间戳（check() 写的是真实当前时间，不便断言边界）
    t0 = 1_000_000.0
    data = feeds.load()
    for feed in data["feeds"]:
        feed["last_checked"] = t0
    feeds._save(data)

    assert feeds.due(interval_minutes=60, now=t0 + 59 * 60) == []            # 59 分钟，未到期
    assert feeds.due(interval_minutes=60, now=t0 + 3600) == [first["id"], second["id"]]  # 恰好到期
    assert feeds.due(interval_minutes=60, now=t0 + 61 * 60) == [first["id"], second["id"]]

    # 单独把 second 设成 3 小时没检查，只有它 due
    data = feeds.load()
    data["feeds"][1]["last_checked"] = t0 - 3 * 3600
    feeds._save(data)
    assert feeds.due(interval_minutes=60, now=t0) == [second["id"]]


def test_due_zero_interval_returns_everything(fake_feed):
    """interval=0：刚检查过也算 due（轮询想要「每次都查」时用）。"""
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    data = feeds.load()
    data["feeds"][0]["last_checked"] = 5_000.0
    feeds._save(data)
    assert feeds.due(0, now=5_000.0) == [feed["id"]]
    assert feeds.due(0, now=4_999.0) == []


def test_due_ignores_missing_last_checked(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    assert feeds.due(interval_minutes=1, now=time.time()) == [feed["id"]]


# ------------------------------------------------------------------ snapshot

def test_snapshot_hides_seen_details(fake_feed):
    fake_feed["content"] = rss([{"title": "第 3 集"}, {"title": "第 2 集"}, {"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)

    snap = feeds.snapshot()
    assert snap["total_new"] == 0
    assert len(snap["feeds"]) == 1
    item = snap["feeds"][0]
    assert "seen" not in item                                     # 不返回 seen 明细
    assert item["seen_count"] == 3
    assert item["new_count"] == 0
    assert item["id"] == feed["id"]
    assert item["url"] == FEED_URL
    assert item["title"] == "测试电台"
    assert item["auto"] is True
    assert item["backfill"] == 0
    assert item["last_error"] == ""


def test_snapshot_after_new_episodes_keeps_seen_count(fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feed = feeds.add(FEED_URL)
    fake_feed["content"] = rss([{"title": "第 3 集", "day": 3}, {"title": "第 2 集", "day": 2},
                                {"title": "第 1 集", "day": 1}])
    assert len(feeds.check(feed["id"])) == 2
    item = feeds.snapshot()["feeds"][0]
    assert "seen" not in item
    assert item["seen_count"] == 3                                # 老 1 集 + 新 2 集
    assert item["last_checked"] is not None


def test_load_tolerates_broken_store(tmp_path):
    (tmp_path / "feeds.json").write_text("{ 这不是 json", encoding="utf-8")
    assert feeds.load() == {"feeds": []}
    assert feeds.snapshot() == {"feeds": [], "total_new": 0}
    assert feeds.due() == []


def test_save_is_atomic_and_leaves_no_tmp_file(tmp_path, fake_feed):
    fake_feed["content"] = rss([{"title": "第 1 集"}])
    feeds.add(FEED_URL)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["feeds.json"]                                # 临时文件没有残留
    assert feeds.FEEDS_PATH.read_text(encoding="utf-8").startswith("{\n  \"feeds\"")
