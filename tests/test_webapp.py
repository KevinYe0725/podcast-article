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
    assert '<a class="ts" data-sec="607"' in html
    assert "[00:10:07]" in html


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
