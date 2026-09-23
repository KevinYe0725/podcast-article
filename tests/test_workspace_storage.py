from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from podcast_article import feeds, kb, library, settings
from podcast_article.platform_store import AccountQuota
from podcast_article.workspace import data_root, workspace_for


@pytest.fixture()
def member_clients(tmp_output, auth_system):
    webapp, store, admin, _ = auth_system
    password = "workspace test passphrase"
    password_hash = webapp.auth_mod.hash_password(password)
    result = {}
    for username in ("alice", "bob"):
        raw = f"invite-for-{username}"
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        store.create_invite(
            admin.id,
            token_hash,
            AccountQuota(3_600, Decimal("5.00"), 1_000_000, 5, 2_000_000),
            time.time() + 3_600,
        )
        account = store.register_invite(token_hash, username, password_hash, now=time.time())
        client = webapp.app.test_client()
        response = client.post("/api/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200
        result[username] = (client, account, workspace_for(account.id, data_root()))
    return result


def test_library_store_uses_requested_workspace(tmp_path):
    alice_path = tmp_path / "users" / "alice" / "library.json"
    bob_path = tmp_path / "users" / "bob" / "library.json"

    library.create("Alice only", store_path=alice_path)

    assert [row["name"] for row in library.snapshot(store_path=alice_path)["categories"]] == ["Alice only"]
    assert library.snapshot(store_path=bob_path)["categories"] == []


def test_settings_store_uses_requested_workspace(tmp_path):
    alice_path = tmp_path / "users" / "alice" / "settings.json"
    bob_path = tmp_path / "users" / "bob" / "settings.json"

    settings.save(profile={"name": "Alice"}, settings_path=alice_path)

    assert settings.load(settings_path=alice_path)["profile"]["name"] == "Alice"
    assert settings.load(settings_path=bob_path)["profile"]["name"] == ""


def test_article_writer_uses_the_callers_profile_path(tmp_path, monkeypatch):
    from podcast_article import summarize

    alice_path = tmp_path / "alice" / "settings.json"
    bob_path = tmp_path / "bob" / "settings.json"
    settings.save(profile={"name": "Alice", "interests": "Alice interests"}, settings_path=alice_path)
    settings.save(profile={"name": "Bob", "interests": "Bob interests"}, settings_path=bob_path)
    seen = {}

    def fake_chat(_client, _model, _system, user, **_kwargs):
        seen["user"] = user
        return "# A story\n\n" + "具体内容。" * 180

    monkeypatch.setattr(summarize, "_client", lambda: object())
    monkeypatch.setattr(summarize, "_chat", fake_chat)
    args = dict(segments=[{"start": 10, "text": "原文内容"}], title="标题", podcast="节目",
                author="作者", shownotes_html=None, outlined=False)

    summarize.write_article(**args, settings_path=alice_path)
    assert "Alice interests" in seen["user"]
    assert "Bob interests" not in seen["user"]
    summarize.write_article(**args, settings_path=bob_path)
    assert "Bob interests" in seen["user"]
    assert "Alice interests" not in seen["user"]


def test_feed_store_uses_requested_workspace(tmp_path, monkeypatch):
    parsed = SimpleNamespace(
        bozo=False,
        feed={"title": "Example show"},
        entries=[{"id": "episode-1", "title": "Episode 1", "link": "https://example.test/1",
                  "enclosures": [{"href": "https://example.test/1.mp3"}]}],
    )
    monkeypatch.setattr(feeds, "_fetch", lambda *_args, **_kwargs: parsed)
    alice_path = tmp_path / "users" / "alice" / "feeds.json"
    bob_path = tmp_path / "users" / "bob" / "feeds.json"

    feeds.add("https://example.test/feed.xml", feeds_path=alice_path)

    assert len(feeds.snapshot(feeds_path=alice_path)["feeds"]) == 1
    assert feeds.snapshot(feeds_path=bob_path)["feeds"] == []


def test_kb_creates_database_only_under_requested_workspace(tmp_path):
    alice_path = tmp_path / "data" / "users" / "alice" / "kb.sqlite"
    bob_path = tmp_path / "data" / "users" / "bob" / "kb.sqlite"

    kb.memory_add("Alice only memory", db_path=alice_path)

    assert kb.memory_list(db_path=alice_path)[0]["text"] == "Alice only memory"
    assert kb.memory_list(db_path=bob_path) == []
    assert alice_path.is_file()
    assert not (Path(__file__).resolve().parents[1] / "kb.sqlite").exists()
    empty_output = tmp_path / "data" / "users" / "new-user" / "output"
    empty_db = tmp_path / "data" / "users" / "new-user" / "kb.sqlite"
    result = kb.index_all(empty_output, embed=False, db_path=empty_db)
    assert result["episodes"] == 0
    assert empty_db.is_file()


def test_authenticated_routes_keep_category_settings_feed_and_memory_per_account(member_clients, monkeypatch):
    alice = member_clients["alice"][0]
    bob = member_clients["bob"][0]
    parsed = SimpleNamespace(
        bozo=False,
        feed={"title": "Example show"},
        entries=[{"id": "episode-1", "title": "Episode 1", "link": "https://example.test/1",
                  "enclosures": [{"href": "https://example.test/1.mp3"}]}],
    )
    monkeypatch.setattr(feeds, "_fetch", lambda *_args, **_kwargs: parsed)

    assert alice.post("/api/categories", json={"name": "Alice category"}).status_code == 200
    assert bob.get("/api/categories").get_json()["categories"] == []

    assert alice.post("/api/settings", json={"profile": {"name": "Alice"}}).status_code == 200
    assert alice.get("/api/settings").get_json()["profile"]["name"] == "Alice"
    assert bob.get("/api/settings").get_json()["profile"]["name"] == ""

    assert alice.post("/api/feeds", json={"url": "https://example.test/feed.xml"}).status_code == 200
    assert len(alice.get("/api/feeds").get_json()["feeds"]) == 1
    assert bob.get("/api/feeds").get_json()["feeds"] == []

    assert alice.post("/api/memory", json={"text": "Alice private memory"}).status_code == 200
    assert [item["text"] for item in alice.get("/api/memory").get_json()["items"]] == ["Alice private memory"]
    assert bob.get("/api/memory").get_json()["items"] == []
    assert alice.get("/api/kb/status").get_json()["memory"] == 1
    assert bob.get("/api/kb/status").get_json()["memory"] == 0


def test_member_settings_cannot_write_server_credentials(member_clients, tmp_path, monkeypatch):
    from podcast_article import settings as settings_mod

    alice = member_clients["alice"][0]
    env_file = tmp_path / "server.env"
    env_file.write_text("DEEPSEEK_API_KEY=server-only\n", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_file)

    response = alice.post("/api/settings", json={"secrets": {"DEEPSEEK_API_KEY": "sk-test-not-to-store"}})

    assert response.status_code == 400
    assert "secrets" not in (alice.get("/api/settings").get_json())
    assert env_file.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=server-only\n"
