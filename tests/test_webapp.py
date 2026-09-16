"""Web 接口：历史库、分类、归类、删除、音频 Range、时间戳链接化。

全部跑在临时输出目录上（见 conftest.py），不会碰真实数据。
"""
import json


def test_library_lists_episode_with_preview(client):
    data = client.get("/api/library").get_json()
    assert [it["dir"] for it in data["items"]] == ["20240101-测试台-测试单集"]
    item = data["items"][0]
    assert item["has_article"] is True
    assert item["has_audio"] is True
    assert item["size"] > 0
    assert item["preview"]["deck"] == "一句话引语"
    assert item["preview"]["takeaways"] == ["一条可检验的判断", "一条可试的做法"]


def test_categories_crud_and_assign(client):
    created = client.post("/api/categories", json={"name": "AI 技术"}).get_json()["category"]
    assert client.post("/api/categories", json={"name": "AI 技术"}).status_code == 400

    resp = client.post("/api/assign", json={"dir": "20240101-测试台-测试单集", "category_id": created["id"]})
    assert resp.status_code == 200 and resp.get_json()["categories"][0]["count"] == 1

    renamed = client.patch(f"/api/categories/{created['id']}", json={"name": "AI 与工程"})
    assert renamed.get_json()["category"]["name"] == "AI 与工程"

    assert client.delete(f"/api/categories/{created['id']}").status_code == 200
    assert client.get("/api/categories").get_json()["categories"] == []


def test_assign_unknown_category_rejected(client):
    assert client.post("/api/assign", json={"dir": "x", "category_id": "cnope"}).status_code == 400


def test_article_html_linkifies_timestamps(client):
    html = client.get("/api/file/20240101-测试台-测试单集/article.md").get_json()["html"]
    assert '<span class="ts" data-sec="607"' in html
    assert "[00:10:07]" in html
    # 时间戳必须是 <span> 而不是 <a>：任何 <a>（哪怕没有 href）都可能触发一次导航，
    # 而阅读页把地址栏当路由（#/a/<目录名>）—— 点一下时间戳，刚打开的文章会被判成
    # 「离开了这一篇」整页收起来。UI 测试与 jsdom 都抓到过这个坑。
    assert 'href="#"' not in html
    assert '<a class="ts"' not in html


def test_file_whitelist_blocks_other_names(client):
    assert client.get("/api/file/20240101-测试台-测试单集/meta.json").status_code == 200
    assert client.get("/api/file/20240101-测试台-测试单集/audio.m4a").status_code == 400


def test_file_path_traversal_rejected(client):
    assert client.get("/api/file/..%2F..%2Fetc/passwd").status_code in (400, 404)


def test_audio_stream_supports_range(client):
    full = client.get("/api/audio/20240101-测试台-测试单集")
    assert full.status_code == 200 and full.headers["Accept-Ranges"] == "bytes"
    part = client.get("/api/audio/20240101-测试台-测试单集", headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206
    assert part.headers["Content-Range"].startswith("bytes 0-1023/")
    assert len(part.data) == 1024


def test_audio_missing_returns_404(client):
    assert client.get("/api/audio/不存在的目录").status_code == 404


def test_delete_article_only_keeps_audio(client, episode):
    resp = client.delete("/api/episode/20240101-测试台-测试单集", json={"scope": "article"})
    assert resp.status_code == 200 and resp.get_json()["freed"] > 0
    assert not (episode / "article.md").exists()
    assert (episode / "audio.m4a").exists()
    assert (episode / "transcript.txt").exists()


def test_delete_whole_record_clears_category(client, episode):
    cat = client.post("/api/categories", json={"name": "临时"}).get_json()["category"]
    client.post("/api/assign", json={"dir": "20240101-测试台-测试单集", "category_id": cat["id"]})

    resp = client.delete("/api/episode/20240101-测试台-测试单集", json={"scope": "all"})
    assert resp.status_code == 200 and resp.get_json()["freed"] > 0
    assert not episode.exists()
    assert client.get("/api/categories").get_json()["assignments"] == {}


def test_delete_unknown_dir_404(client):
    assert client.delete("/api/episode/不存在", json={"scope": "all"}).status_code == 404


def test_settings_roundtrip_and_secret_write(client, monkeypatch, tmp_path):
    from podcast_article import settings as st

    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-old\n", encoding="utf-8")
    monkeypatch.setattr(st, "ENV_PATH", env)

    resp = client.post("/api/settings", json={
        "profile": {"name": "Kevin", "interests": "AI"},
        "generation": {"length_mode": "concise"},
        "secrets": {"DEEPSEEK_API_KEY": "sk-new"},
    })
    body = resp.get_json()
    assert body["profile"]["name"] == "Kevin"
    assert body["generation"]["length_mode"] == "concise"
    assert body["env_changed"] == ["DEEPSEEK_API_KEY"]
    assert "sk-new" in env.read_text(encoding="utf-8")

    got = client.get("/api/settings").get_json()
    assert got["profile"]["name"] == "Kevin"
    assert "sk-new" not in json.dumps(got["secrets"])       # 明文绝不回传


def test_mcp_preset_endpoint(client, monkeypatch, tmp_path):
    from podcast_article import mcp_config as mc

    monkeypatch.setattr(mc, "CONFIG_PATH", tmp_path / "mcp_servers.json")
    resp = client.post("/api/mcp/servers/preset", json={"preset": "podcast-article"})
    assert resp.status_code == 200
    assert resp.get_json()["server"]["name"] == "podcast-article"
    assert client.post("/api/mcp/servers/preset", json={"preset": "nope"}).status_code == 400


def test_jobs_current_idle(client):
    assert client.get("/api/jobs/current").get_json()["status"] == "idle"


# ================================================================ 检索 / 状态 / 用量 / 导出
#
# 新增接口的用例。两条硬规矩：
# 1. webapp 在 import 时把 queue.QUEUE_PATH / feeds.FEEDS_PATH 定到了仓库根目录，
#    所以凡是碰队列或订阅的用例都必须用 stores fixture 把它们指到 tmp_path —— 绝不
#    能写出仓库根目录的 queue.json / feeds.json（settings.json 同理）。
# 2. 网络出口（feeds._fetch）一律打桩，绝不真的联网；会走到 Pipeline 的一律
#    monkeypatch 掉 webapp._new_job，绝不真的启动下载任务。

import io
import zipfile

import pytest

DIR = "20240101-测试台-测试单集"
FEED_URL = "https://example.com/feed.xml"


def _pub_date(day: int) -> str:
    """2024-03-<day> 00:00:00 GMT（RFC-822，feedparser 能解析）。"""
    return f"Mon, {day:02d} Mar 2024 00:00:00 GMT"


def _rss(titles: list[str], title: str = "测试电台") -> str:
    """造一段最小可解析的 RSS。

    titles 的顺序就是 feed 顺序（1 = 最新）；guid/link 由标题生成，所以「同一集」
    在 feed 内容变化前后标识稳定 —— 新增一集不会让老的单集被当成新单集。
    """
    items = []
    for i, ep_title in enumerate(titles, 1):
        key = "".join(ch if ch.isalnum() else "-" for ch in ep_title).strip("-") or f"ep{i}"
        items.append(
            f"<item><title>{ep_title}</title><guid>guid-{key}</guid>"
            f"<link>https://example.com/{key}</link>"
            f"<pubDate>{_pub_date(i)}</pubDate>"
            f'<enclosure url="https://example.com/{key}.mp3" type="audio/mpeg" length="1"/></item>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{title}</title>{''.join(items)}</channel></rss>"
    )


def _write_episode(root, name: str, *, title: str, podcast: str = "测试台",
                   article: str | None = "# 测试标题\n\n旧金山 又一次地震。\n"):
    """在输出目录里造一条记录（article=None 时只写 meta.json）。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"title": title, "podcast": podcast,
                    "url": f"https://example.com/{name}"}, ensure_ascii=False),
        encoding="utf-8",
    )
    if article is not None:
        (d / "article.md").write_text(article, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def clean_usage_slot(monkeypatch):
    """usage 的活动记录器是模块级全局状态，每条用例跑完都要还原，避免污染别的用例。"""
    from podcast_article import usage

    monkeypatch.setattr(usage, "_active", None)


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """把队列 / 订阅 / 设置都指到临时目录，返回三个落盘路径。"""
    from podcast_article import feeds, queue
    from podcast_article import settings as st

    paths = {
        "queue": tmp_path / "queue.json",
        "feeds": tmp_path / "feeds.json",
        "settings": tmp_path / "settings.json",
    }
    monkeypatch.setattr(queue, "QUEUE_PATH", paths["queue"])
    monkeypatch.setattr(feeds, "FEEDS_PATH", paths["feeds"])
    monkeypatch.setattr(st, "SETTINGS_PATH", paths["settings"])
    return paths


@pytest.fixture()
def fake_feed(monkeypatch):
    """把 feeds 唯一的网络出口换成假解析器：state["content"] 换内容就等于 feed 更新。"""
    import feedparser

    from podcast_article import feeds

    state = {"content": _rss(["第一集", "第二集"]), "error": None, "calls": []}

    def fake_fetch(url, timeout=25.0):
        state["calls"].append(url)
        if state["error"] is not None:
            raise state["error"]
        return feedparser.parse(state["content"])

    monkeypatch.setattr(feeds, "_fetch", fake_fetch)
    return state


# ------------------------------------------------------------------ 全文检索


def test_search_empty_query_returns_zero_and_stats(client):
    data = client.get("/api/search").get_json()
    assert data["count"] == 0, f"空查询应返回 0 条，实际 {data['count']}"
    assert data["items"] == [], f"空查询不该返回条目：{data['items']}"
    stats = data["stats"]
    assert {"episodes", "indexed"} <= set(stats), f"stats 必须带 episodes / indexed，实际 {stats}"
    assert stats["episodes"] == 1, f"临时输出目录里只有 1 集，实际 {stats['episodes']}"


def test_search_hits_article_with_dir_title_podcast(client):
    data = client.get("/api/search?q=旧金山").get_json()
    assert data["count"] == 1, f"应命中 1 集，实际 {data['count']}"
    item = data["items"][0]
    assert item["dir"] == DIR, f"命中目录错误：{item['dir']}"
    assert item["title"] == "测试单集", f"标题应来自 meta.json，实际 {item['title']}"
    assert item["podcast"] == "测试台", f"播客名应来自 meta.json，实际 {item['podcast']}"
    assert item["hits"], "命中项必须带 hits 明细"
    assert any(h["field"] == "article" and h["match"] == "旧金山" for h in item["hits"]), \
        f"应有一条 article 命中，实际 {item['hits']}"


def test_search_transcript_hit_carries_timestamp(client):
    data = client.get("/api/search?q=这是原话").get_json()
    assert data["count"] == 1, f"应命中 1 集，实际 {data['count']}"
    hits = [h for h in data["items"][0]["hits"] if h["field"] == "transcript"]
    assert hits, f"应命中 transcript.txt，实际 {data['items'][0]['hits']}"
    assert hits[0]["ts"] == "00:10:07", f"hit 应带行首时间戳，实际 {hits[0]['ts']!r}"


def test_search_items_carry_category_and_status(client):
    item = client.get("/api/search?q=旧金山").get_json()["items"][0]
    assert "category" in item and "status" in item, f"结果项应带 category / status：{item}"
    assert item["category"] is None, f"未归类时应是 None，实际 {item['category']!r}"
    assert item["status"] == "unread", f"未标记时应是默认的 unread，实际 {item['status']!r}"


def test_search_limit_caps_results(client, tmp_output):
    _write_episode(tmp_output, "20240202-测试台-第二集", title="第二集")
    assert client.get("/api/search?q=旧金山").get_json()["count"] == 2, "两集都应命中"

    limited = client.get("/api/search?q=旧金山&limit=1").get_json()
    assert limited["count"] == 1, f"limit=1 时应只返回 1 条，实际 {limited['count']}"
    assert len(limited["items"]) == 1


def test_search_without_match_returns_empty_not_500(client):
    resp = client.get("/api/search?q=根本不存在的词汇zzz")
    assert resp.status_code == 200, f"命中不到不该是错误码，实际 {resp.status_code}"
    assert resp.get_json()["count"] == 0, f"命中不到应返回 0 条，实际 {resp.get_json()['count']}"


# ------------------------------------------------------------------ 阅读状态


def test_status_sets_value_and_counts(client):
    resp = client.post("/api/status", json={"dir": DIR, "status": "read"})
    assert resp.status_code == 200, f"设置阅读状态应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["value"] == "read", f"应回显设置的状态，实际 {body['value']!r}"
    assert body["state"]["status"][DIR] == "read", f"状态里应有该目录：{body['state']['status']}"
    assert body["state"]["status_counts"]["read"] == 1, \
        f"已读计数应 +1，实际 {body['state']['status_counts']}"


def test_status_rejects_unknown_value(client):
    resp = client.post("/api/status", json={"dir": DIR, "status": "已读"})
    assert resp.status_code == 400, f"非法状态应 400，实际 {resp.status_code}"
    error = resp.get_json()["error"]
    assert "未知的阅读状态" in error, f"报错应可读，实际 {error!r}"


def test_status_blank_clears_mark(client):
    client.post("/api/status", json={"dir": DIR, "status": "later"})
    resp = client.post("/api/status", json={"dir": DIR, "status": ""})
    assert resp.status_code == 200, f"清除标记应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["value"] == "unread", f"清除后应回到默认未读，实际 {body['value']!r}"
    assert DIR not in body["state"]["status"], f"标记应被删除：{body['state']['status']}"
    assert body["state"]["status_counts"]["later"] == 0, \
        f"稍后读计数应回到 0，实际 {body['state']['status_counts']}"


def test_status_requires_dir(client):
    resp = client.post("/api/status", json={"status": "read"})
    assert resp.status_code == 400, f"缺 dir 应 400，实际 {resp.status_code}"
    assert resp.get_json()["error"], "应给出可读的报错"


# ------------------------------------------------------------------ 用量与费用


def test_usage_without_records(client):
    body = client.get("/api/usage").get_json()
    assert body["total"]["episodes"] == 0, \
        f"没有 usage.json 时应是 0 集，实际 {body['total']['episodes']}"
    assert body["live"] is None, f"没有活动记录器时 live 应是 None，实际 {body['live']!r}"
    assert "deepseek-flash" in body["prices"], f"价格表应含 flash，实际 {list(body['prices'])}"
    assert body["busy"] is False, f"没有运行中的任务，实际 busy={body['busy']}"


def test_usage_totals_from_saved_records(client, episode):
    from podcast_article import usage

    record = usage.empty("deepseek-flash")
    usage.add(record, {"hit": 1000, "miss": 2000, "out": 500}, peak=False)
    usage.save(episode, record)

    total = client.get("/api/usage").get_json()["total"]
    assert total["episodes"] == 1, f"应统计到 1 集，实际 {total['episodes']}"
    assert total["total_tokens"] == 3500, \
        f"total_tokens 应为 1000+2000+500=3500，实际 {total['total_tokens']}"
    # 空闲时段 flash：命中 0.02 / 未命中 1.0 / 输出 4.0（元每百万 token）
    expected = round(1000 / 1_000_000 * 0.02 + 2000 / 1_000_000 * 1.0
                     + 500 / 1_000_000 * 4.0, 4)
    assert total["cost_cny"] == expected, f"费用应为 {expected} 元，实际 {total['cost_cny']}"
    assert "deepseek-flash" in total["by_model"], f"应按模型分组，实际 {list(total['by_model'])}"
    assert total["by_model"]["deepseek-flash"]["total_tokens"] == 3500


def test_usage_reports_live_recorder(client):
    from podcast_article import usage

    usage.start("deepseek-flash")
    try:
        live = client.get("/api/usage").get_json()["live"]
        assert live is not None, "有活动记录器时 live 不该是 None"
        assert live["calls"] == 0, f"还没调用过模型，calls 应为 0，实际 {live['calls']}"
        assert live["model"] == "deepseek-flash", f"模型名应回传，实际 {live['model']!r}"
        assert live["cost_cny"] == 0.0, f"没有 token 时费用应为 0，实际 {live['cost_cny']}"
    finally:
        usage.stop()          # 全局状态必须还原，否则会污染别的用例


# ------------------------------------------------------------------ 单篇导出


def test_export_markdown_has_front_matter_and_utf8_name(client):
    resp = client.get(f"/api/export/{DIR}?fmt=md")
    assert resp.status_code == 200, f"导出 md 应成功，实际 {resp.status_code}"
    disposition = resp.headers["Content-Disposition"]
    assert "UTF-8''" in disposition, f"中文文件名要用 RFC 5987 编码，实际 {disposition!r}"
    text = resp.get_data(as_text=True)
    assert text.startswith("---"), f"导出的 md 应带 YAML front matter，实际开头 {text[:20]!r}"
    assert "旧金山" in text, "正文应原样带出"


def test_export_html_is_standalone_document(client):
    resp = client.get(f"/api/export/{DIR}?fmt=html")
    assert resp.status_code == 200, f"导出 html 应成功，实际 {resp.status_code}"
    assert "<!DOCTYPE html>" in resp.get_data(as_text=True), "应是一份完整 HTML 文档"


def test_export_txt_is_transcript(client, episode):
    resp = client.get(f"/api/export/{DIR}?fmt=txt")
    assert resp.status_code == 200, f"导出 txt 应成功，实际 {resp.status_code}"
    assert resp.get_data(as_text=True) == (episode / "transcript.txt").read_text(encoding="utf-8"), \
        "txt 导出应就是文字稿原文"


def test_export_rejects_unknown_format(client):
    resp = client.get(f"/api/export/{DIR}?fmt=pdf")
    assert resp.status_code == 400, f"未知格式应 400，实际 {resp.status_code}"
    assert resp.get_json()["error"], "应给出可读的报错"


def test_export_unknown_dir_404(client):
    resp = client.get("/api/export/不存在的目录?fmt=md")
    assert resp.status_code == 404, f"目录不存在应 404，实际 {resp.status_code}"


def test_export_missing_article_400(client, tmp_output):
    _write_episode(tmp_output, "20240303-只有元信息", title="只有元信息", article=None)
    resp = client.get("/api/export/20240303-只有元信息?fmt=md")
    assert resp.status_code == 400, f"没有 article.md 应 400，实际 {resp.status_code}"
    assert "article.md" in resp.get_json()["error"], \
        f"报错应说明缺什么，实际 {resp.get_json()['error']!r}"


def test_export_path_traversal_rejected(client, tmp_path):
    """输出根目录之外的目录一个都不能被导出（这里真的造了一个在根目录外的目标）。"""
    import webapp

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "article.md").write_text("# 不该被导出\n", encoding="utf-8")

    for path in ("..%2F..%2Fetc", "..%2Foutside", "%2e%2e%2Foutside", "..%5Coutside"):
        resp = client.get(f"/api/export/{path}?fmt=md")
        assert resp.status_code == 404, f"{path} 必须 404（越界），实际 {resp.status_code}"

    # 直接检查解析逻辑：越界的目录名一律给 None，正常的目录名仍然能解析出来
    for name in ("..", "../outside", "../..", "outside/../../etc"):
        assert webapp._safe_dir(name) is None, f"{name} 不该被解析成可导出的目录"
    assert webapp._safe_dir(DIR) is not None, "正常的目录名应能解析出来"


# ------------------------------------------------------------------ 整库导出


def test_export_bundle_returns_zip(client):
    resp = client.get("/api/export")
    assert resp.status_code == 200, f"整库导出应成功，实际 {resp.status_code}"
    assert "zip" in resp.headers["Content-Type"], \
        f"Content-Type 应是 zip，实际 {resp.headers['Content-Type']!r}"
    assert resp.headers["X-Episodes"] == "1", \
        f"X-Episodes 应是 1，实际 {resp.headers['X-Episodes']!r}"

    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.testzip() is None, "zip 内容应完好"
        names = zf.namelist()
    assert "README.md" in names, f"zip 里应有目录页，实际 {names}"
    assert any(n.endswith(".md") and n != "README.md" for n in names), f"zip 里应有文章，实际 {names}"
    assert any("文字稿" in n for n in names), f"默认应带文字稿，实际 {names}"


def test_export_bundle_without_articles_400(client, episode):
    (episode / "article.md").unlink()
    resp = client.get("/api/export")
    assert resp.status_code == 400, f"一篇都没有时应 400，实际 {resp.status_code}"
    assert resp.get_json()["error"], "应给出可读的报错"


def test_export_bundle_unknown_category_400(client):
    resp = client.get("/api/export?category=cnope")
    assert resp.status_code == 400, f"指定的分类里没有文章应 400，实际 {resp.status_code}"


def test_export_bundle_can_skip_transcript(client):
    resp = client.get("/api/export?transcript=0")
    assert resp.status_code == 200, f"transcript=0 也应能导出，实际 {resp.status_code}"
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        names = zf.namelist()
    assert not any("文字稿" in n for n in names), f"transcript=0 时不该有文字稿，实际 {names}"


# ------------------------------------------------------------------ 批量队列


def test_queue_adds_multiple_urls_with_pending_state(client, stores):
    resp = client.post("/api/queue", json={
        "urls": "https://a.com/1\nhttps://a.com/2\nhttps://a.com/3",
        "opts": {"mode": "deep"},
    })
    assert resp.status_code == 200, f"入队应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert len(body["added"]) == 3, f"应加入 3 条，实际 {len(body['added'])}"
    items = body["queue"]["items"]
    assert [i["url"] for i in items] == ["https://a.com/1", "https://a.com/2", "https://a.com/3"], \
        f"顺序应与粘贴顺序一致：{[i['url'] for i in items]}"
    assert all(i["state"] == "pending" for i in items), \
        f"新入队的都应是 pending，实际 {[i['state'] for i in items]}"
    assert all(i["opts"]["mode"] == "deep" for i in items), \
        f"opts 应保留传进来的 mode：{[i['opts'] for i in items]}"


def test_queue_snapshots_toplevel_options(client, stores):
    resp = client.post("/api/queue", json={"urls": "https://b.com/1", "mode": "concise", "lang": "en"})
    item = resp.get_json()["queue"]["items"][0]
    assert item["opts"] == {"mode": "concise", "lang": "en"}, \
        f"没显式给 opts 时应快照首页选项，实际 {item['opts']}"


def test_queue_add_skips_duplicate_url(client, stores):
    resp1 = client.post("/api/queue", json={"urls": "https://a.com/1"})
    assert len(resp1.get_json()["added"]) == 1, "第一次应入队 1 条"
    resp2 = client.post("/api/queue", json={"urls": "https://a.com/1"})
    assert resp2.get_json()["added"] == [], \
        f"同一链接重复提交不该再入队，实际 {resp2.get_json()['added']}"
    assert len(resp2.get_json()["queue"]["items"]) == 1, "队列里仍应只有 1 条"


def test_queue_add_without_links_400(client, stores):
    resp = client.post("/api/queue", json={"urls": "这里没有链接\n随便写点什么"})
    assert resp.status_code == 400, f"没有链接应 400，实际 {resp.status_code}"
    assert resp.get_json()["error"], "应给出可读的报错"
    assert not stores["queue"].exists(), "参数不合法时不该落盘 queue.json"


def test_queue_delete_item_and_unknown_404(client, stores):
    added = client.post("/api/queue", json={"urls": "https://a.com/1\nhttps://a.com/2"}).get_json()["added"]
    ids = [i["id"] for i in added]

    resp = client.delete(f"/api/queue/{ids[0]}")
    assert resp.status_code == 200, f"删除应成功，实际 {resp.status_code}"
    assert [i["id"] for i in resp.get_json()["items"]] == [ids[1]], \
        f"队列应变短：{[i['id'] for i in resp.get_json()['items']]}"
    assert client.delete("/api/queue/qnope").status_code == 404, "不存在的 id 应 404"


def test_queue_move_up_with_negative_delta(client, stores):
    added = client.post("/api/queue", json={"urls": "https://a.com/1\nhttps://a.com/2\nhttps://a.com/3"}).get_json()["added"]
    ids = [i["id"] for i in added]

    resp = client.post(f"/api/queue/{ids[1]}/move", json={"delta": -1})
    assert resp.status_code == 200, f"移动应成功，实际 {resp.status_code}"
    assert resp.get_json()["position"] == 0, f"第二条应换到第 0 位，实际 {resp.get_json()['position']}"
    assert [i["id"] for i in resp.get_json()["queue"]["items"]] == [ids[1], ids[0], ids[2]], \
        "队列顺序应变成 2、1、3"
    assert client.post("/api/queue/qnope/move", json={"delta": -1}).status_code == 404


def test_queue_retry_puts_failed_back_to_pending(client, stores):
    from podcast_article import queue

    added = queue.add("https://a.com/1")
    queue.finish(added[0]["id"], "error", error="x")

    resp = client.post("/api/queue/retry")
    assert resp.status_code == 200, f"重试应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["retried"] == 1, f"应重试 1 条，实际 {body['retried']}"
    assert [i["state"] for i in body["queue"]["items"]] == ["pending"], \
        f"失败项应回到 pending，实际 {[i['state'] for i in body['queue']['items']]}"


def test_queue_clear_keeps_pending(client, stores):
    from podcast_article import queue

    added = queue.add("https://a.com/1\nhttps://a.com/2\nhttps://a.com/3")
    queue.finish(added[0]["id"], "done", dir_name="某目录")
    queue.finish(added[1]["id"], "error", error="崩了")

    resp = client.post("/api/queue/clear")
    assert resp.status_code == 200, f"清空应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["removed"] == 2, f"done 与 error 共 2 条被清，实际 {body['removed']}"
    assert [i["id"] for i in body["queue"]["items"]] == [added[2]["id"]], "pending 必须保留"


def test_queue_get_reports_counts_and_active(client, stores):
    from podcast_article import queue

    added = queue.add("https://a.com/1\nhttps://a.com/2\nhttps://a.com/3")
    queue.claim(added[0]["id"])                       # → running
    queue.finish(added[1]["id"], "done", dir_name="某目录")

    body = client.get("/api/queue").get_json()
    assert body["counts"] == {"pending": 1, "running": 1, "done": 1, "error": 0, "skipped": 0}, \
        f"计数错误：{body['counts']}"
    assert body["active"] == 2, f"active 应是 pending+running=2，实际 {body['active']}"


# ------------------------------------------------------------------ 订阅（不联网）


def test_feeds_add_subscribes_without_rediscovering(client, stores, fake_feed):
    resp = client.post("/api/feeds", json={"url": FEED_URL})
    assert resp.status_code == 200, f"订阅应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["feed"]["title"] == "测试电台", f"标题应来自 feed，实际 {body['feed']['title']!r}"
    assert body["feed"]["url"] == FEED_URL, f"链接应原样保存，实际 {body['feed']['url']!r}"
    assert body["enqueued"] == 0, f"backfill=0 时不该入队，实际 {body['enqueued']}"
    assert body["feeds"]["feeds"][0]["id"] == body["feed"]["id"], "订阅列表应含这一条"
    assert fake_feed["calls"], "没有走被替换的 _fetch，测试可能真的联网了"

    from podcast_article import feeds

    assert feeds.check() == [], "订阅时已把现有单集标成 seen，不该再当成新单集"


def test_feeds_add_reports_fetch_error(client, stores, fake_feed):
    from podcast_article import feeds

    fake_feed["error"] = ValueError("RSS 解析失败：Connection refused")
    resp = client.post("/api/feeds", json={"url": FEED_URL})
    assert resp.status_code == 400, f"抓取失败应 400，实际 {resp.status_code}"
    error = resp.get_json()["error"]
    assert "Connection refused" in error, f"报错应可读，实际 {error!r}"
    assert feeds.load()["feeds"] == [], "订阅失败不该落盘"


def test_feeds_discover_previews_episodes(client, stores, fake_feed):
    resp = client.post("/api/feeds/discover", json={"url": FEED_URL})
    assert resp.status_code == 200, f"预览应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["title"] == "测试电台", f"标题错误：{body['title']!r}"
    assert [e["index"] for e in body["episodes"]] == [1, 2], \
        f"index 应从 1 开始递增，实际 {[e['index'] for e in body['episodes']]}"
    assert body["episodes"][0]["title"] == "第一集", f"第一篇应是第一集：{body['episodes'][0]!r}"


def test_feeds_patch_auto_and_unknown_404(client, stores, fake_feed):
    fid = client.post("/api/feeds", json={"url": FEED_URL}).get_json()["feed"]["id"]
    resp = client.patch(f"/api/feeds/{fid}", json={"auto": False})
    assert resp.status_code == 200, f"改订阅应成功，实际 {resp.status_code}"
    assert resp.get_json()["feed"]["auto"] is False, \
        f"auto 应改成 False，实际 {resp.get_json()['feed']['auto']!r}"
    assert client.patch("/api/feeds/fnope", json={"auto": True}).status_code == 404, \
        "不存在的订阅应 404"


def test_feeds_delete_and_unknown_404(client, stores, fake_feed):
    fid = client.post("/api/feeds", json={"url": FEED_URL}).get_json()["feed"]["id"]
    resp = client.delete(f"/api/feeds/{fid}")
    assert resp.status_code == 200, f"删订阅应成功，实际 {resp.status_code}"
    assert resp.get_json()["feeds"] == [], f"订阅应被删掉：{resp.get_json()['feeds']}"
    assert client.delete(f"/api/feeds/{fid}").status_code == 404, "再删应 404"


def test_feeds_check_enqueues_new_episode_snapshot(client, stores, fake_feed):
    from podcast_article import queue

    client.post("/api/feeds", json={"url": FEED_URL})
    fake_feed["content"] = _rss(["新的一集", "第一集", "第二集"])

    resp = client.post("/api/feeds/check", json={})
    assert resp.status_code == 200, f"检查订阅应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["found"] == 1, f"应有 1 集新单集，实际 {body['found']}"
    assert body["enqueued"] == 1, f"应入队 1 条，实际 {body['enqueued']}"
    assert [e["title"] for e in body["episodes"]] == ["新的一集"], f"发现列表错误：{body['episodes']}"

    items = queue.snapshot()["items"]
    assert len(items) == 1, f"队列应多一条，实际 {len(items)} 条"
    item = items[0]
    assert item["state"] == "pending", f"应是待跑，实际 {item['state']}"
    assert item["source"].startswith("feed:"), f"来源应是 feed:<id>，实际 {item['source']!r}"
    assert item["url"] == FEED_URL, f"排队的是 feed 链接，实际 {item['url']!r}"

    episode = item["opts"]["episode"]
    assert episode["audio_url"] == "https://example.com/新的一集.mp3", \
        f"快照应带音频地址，实际 {episode['audio_url']!r}"
    assert episode["title"] == "新的一集", f"快照标题错误：{episode['title']!r}"
    assert episode["podcast"] == "测试电台", f"快照应带节目名，实际 {episode['podcast']!r}"
    assert episode["source"] == "rss", f"快照来源应是 rss，实际 {episode['source']!r}"
    assert episode["subtitle_tracks"] == [], f"快照不该带字幕轨：{episode['subtitle_tracks']!r}"


def test_feeds_check_enqueue_false_only_reports(client, stores, fake_feed):
    from podcast_article import queue

    client.post("/api/feeds", json={"url": FEED_URL})
    fake_feed["content"] = _rss(["新的一集", "第一集", "第二集"])

    body = client.post("/api/feeds/check", json={"enqueue": False}).get_json()
    assert body["found"] == 1, f"仍应发现 1 集，实际 {body['found']}"
    assert body["enqueued"] == 0, f"enqueue=false 时不该入队，实际 {body['enqueued']}"
    assert queue.snapshot()["items"] == [], "队列不该有变化"


def test_feeds_check_respects_auto_generate_off(client, stores, fake_feed):
    from podcast_article import queue
    from podcast_article import settings as st

    st.save(subscriptions={"auto_generate": False})
    client.post("/api/feeds", json={"url": FEED_URL})
    fake_feed["content"] = _rss(["新的一集", "第一集", "第二集"])

    body = client.post("/api/feeds/check", json={}).get_json()
    assert body["found"] == 1, f"仍应发现 1 集，实际 {body['found']}"
    assert body["enqueued"] == 0, f"关掉自动生成后不该入队，实际 {body['enqueued']}"
    assert queue.snapshot()["items"] == [], "队列不该有变化"


def test_feeds_settings_saves_interval_minutes(client, stores):
    resp = client.post("/api/feeds/settings", json={"interval_minutes": "45", "enabled": True})
    assert resp.status_code == 200, f"保存订阅设置应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["interval_minutes"] == 45, f"字符串应被转成整数，实际 {body['interval_minutes']!r}"
    assert body["enabled"] is True, f"enabled 应被保存，实际 {body['enabled']!r}"
    assert client.get("/api/settings").get_json()["subscriptions"]["interval_minutes"] == 45, \
        "重新读取设置时应拿到新值"


# ------------------------------------------------------------------ 设置新增段


def test_settings_exposes_subscription_defaults(client, stores):
    from podcast_article import settings as st

    body = client.get("/api/settings").get_json()
    assert "subscriptions" in body, f"设置里应有订阅段：{sorted(body)}"
    assert set(body["subscriptions"]) == set(st.SUBSCRIPTION_DEFAULTS), \
        f"订阅段应含四个默认键，实际 {sorted(body['subscriptions'])}"


def test_settings_saves_subscriptions_and_ignores_unknown_keys(client, stores):
    resp = client.post("/api/settings", json={"subscriptions": {"interval_minutes": 15, "xxx": 1}})
    assert resp.status_code == 200, f"保存设置应成功，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["subscriptions"]["interval_minutes"] == 15, \
        f"应回显保存后的值，实际 {body['subscriptions']['interval_minutes']!r}"
    assert "xxx" not in body["subscriptions"], f"非法键应被忽略：{sorted(body['subscriptions'])}"
    assert body["env_changed"] == [], f"没传密钥时不该改 .env，实际 {body['env_changed']}"
    assert client.get("/api/settings").get_json()["subscriptions"]["interval_minutes"] == 15


# ------------------------------------------------------------------ 任务锁


def test_run_returns_409_when_job_running(client, monkeypatch):
    import webapp

    webapp._JOBS["job-busy001"] = {"id": "job-busy001", "url": "https://example.com/busy",
                                   "status": "running"}
    try:
        resp = client.post("/api/run", json={"url": "https://example.com/new"})
        assert resp.status_code == 409, f"已有任务在跑应 409，实际 {resp.status_code}"
        body = resp.get_json()
        assert body["busy"] is True, f"busy 应是 True，实际 {body['busy']!r}"
        assert body["job_id"] == "job-busy001", f"应告诉前端在跑哪个任务，实际 {body['job_id']!r}"
        assert body["url"] == "https://example.com/busy", f"应回传在跑的链接，实际 {body['url']!r}"
    finally:
        webapp._JOBS.pop("job-busy001", None)


def test_run_requires_url(client):
    resp = client.post("/api/run", json={})
    assert resp.status_code == 400, f"空链接应 400，实际 {resp.status_code}"
    assert resp.get_json()["error"], "应给出可读的报错"


def test_run_creates_job_when_idle(client, monkeypatch):
    """没有 running 任务时不该 409；用假 _new_job 顶替，绝不真的跑 Pipeline（会下载音频）。"""
    import webapp

    calls = []

    def fake_new_job(url, opts, *, source="manual", queue_id=None):
        calls.append({"url": url, "opts": opts, "source": source, "queue_id": queue_id})
        return "job-fake001"

    monkeypatch.setattr(webapp, "_new_job", fake_new_job)
    resp = client.post("/api/run", json={"url": "https://example.com/ep", "mode": "deep"})
    assert resp.status_code != 409, "没有 running 任务时不该返回 409"
    assert resp.status_code == 200, f"应正常创建任务，实际 {resp.status_code}"
    assert resp.get_json()["job_id"] == "job-fake001", f"应回传假 job_id，实际 {resp.get_json()}"
    assert len(calls) == 1, f"应只开一个任务，实际 {len(calls)}"
    assert calls[0]["url"] == "https://example.com/ep", f"链接错误：{calls[0]['url']!r}"
    assert calls[0]["opts"]["mode"] == "deep", f"选项应透传，实际 {calls[0]['opts']}"
    assert calls[0]["source"] == "manual", f"首页提交的来源应是 manual，实际 {calls[0]['source']!r}"
    assert calls[0]["queue_id"] is None, f"首页提交不该带 queue_id，实际 {calls[0]['queue_id']!r}"
