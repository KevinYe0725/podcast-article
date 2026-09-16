"""只读镜像模式（PA_READONLY=1）。

部署到公网服务器时那台机器只负责「看」：文章、检索、时间戳回听、导出都能用；
生成 / 转写 / AI 助手 / 发布 / 删除一律拦住 —— 因为算力（转写）与密钥（DeepSeek、
Notion）都留在你自己的 Mac 上。这份测试守的就是"哪些动不了、哪些照常用"。
"""
from __future__ import annotations

import pytest

DIR = "20240101-测试台-测试单集"


@pytest.fixture()
def readonly(client, monkeypatch):
    """把 webapp.READONLY 打开（它在**调用时**读全局，所以直接改就行）。"""
    import webapp

    monkeypatch.setattr(webapp, "READONLY", True)
    return client


# --------------------------------------------------------------- 默认（本机）

def test_config_reports_readwrite_by_default(client):
    d = client.get("/api/config").get_json()
    assert d["readonly"] is False
    assert d["assistant"] is True and d["publish"] is True
    assert d["hint"] == ""


# --------------------------------------------------------------- 拦住的动怍


@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/run", {"url": "https://example.com/ep"}),
    ("post", "/api/queue", {"urls": "https://example.com/ep"}),
    ("post", "/api/queue/run", {}),
    ("post", "/api/queue/retry", {}),
    ("post", "/api/queue/clear", {}),
    ("post", "/api/ask", {"dir": DIR, "question": "这段什么意思"}),
    ("post", "/api/publish", {"dir": DIR, "target": "notion"}),
    ("post", "/api/notion", {"dir": DIR}),
    ("post", "/api/covers/backfill", {}),
    ("post", "/api/status", {"dir": DIR, "status": "read"}),
    ("post", "/api/assign", {"dir": DIR, "category_id": ""}),
    ("post", "/api/categories", {"name": "新分类"}),
    ("post", "/api/feeds", {"url": "https://example.com/feed.xml"}),
    ("post", "/api/feeds/check", {}),
    ("post", "/api/settings", {"secrets": {"DEEPSEEK_API_KEY": "x"}}),
    ("delete", f"/api/episode/{DIR}", None),
    ("delete", "/api/queue/q123", None),
    ("delete", "/api/feeds/f1", None),
])
def test_readonly_blocks_writes(readonly, method, path, payload):
    resp = getattr(readonly, method)(path, json=payload) if payload is not None else getattr(readonly, method)(path)
    assert resp.status_code == 503, f"{method.upper()} {path} 在只读镜像下应 503，实际 {resp.status_code}"
    body = resp.get_json()
    assert body["readonly"] is True, f"{path} 的报错里应带 readonly 标记：{body}"
    assert "Mac" in body["error"], f"{path} 的报错应告诉用户去哪做：{body['error']}"


def test_readonly_config_flags(readonly):
    d = readonly.get("/api/config").get_json()
    assert d["readonly"] is True
    assert d["assistant"] is False, "这台机器没有 DeepSeek 密钥，助手要标成不可用"
    assert d["publish"] is False, "Notion / MCP 发布同理"
    assert "Mac" in d["hint"], "要写清「算力在 Mac 上」"


# --------------------------------------------------------------- 照常可用的


def test_readonly_keeps_reading_available(readonly):
    """读的路一条都不能被拦：库、检索、正文、文字稿、音频、导出、用量、设置（只读）。"""
    assert readonly.get("/api/library").status_code == 200
    assert readonly.get("/api/search?q=地震").status_code == 200
    assert readonly.get(f"/api/file/{DIR}/article.md").status_code == 200
    assert readonly.get(f"/api/file/{DIR}/transcript.txt").status_code == 200
    assert readonly.get(f"/api/audio/{DIR}").status_code == 200
    assert readonly.get(f"/api/export/{DIR}?fmt=md").status_code == 200
    assert readonly.get("/api/usage").status_code == 200
    assert readonly.get("/api/settings").status_code == 200
    assert readonly.get("/api/queue").status_code == 200


def test_readonly_index_page_still_served(readonly):
    resp = readonly.get("/")
    assert resp.status_code == 200 and b"podcast" in resp.data.lower()


def test_readonly_does_not_leak_job_state(readonly, monkeypatch):
    """只读镜像上不该有跑着的任务；/api/jobs/current 要老实说 idle。"""
    assert readonly.get("/api/jobs/current").get_json()["status"] == "idle"
