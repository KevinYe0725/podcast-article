from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken
from concurrent.futures import ThreadPoolExecutor

from podcast_article.integration_secrets import IntegrationSecrets


def test_integration_secrets_are_encrypted_and_scoped_by_account(tmp_path):
    key = Fernet.generate_key()
    alice_id, bob_id = str(uuid4()), str(uuid4())
    store = IntegrationSecrets(tmp_path, key=key)

    store.set_for_user(alice_id, "NOTION_TOKEN", "alice-only-token")

    encrypted = (tmp_path / "users" / alice_id / "integrations.enc").read_bytes()
    assert b"alice-only-token" not in encrypted
    assert (tmp_path / "users" / alice_id / "integrations.enc").stat().st_mode & 0o777 == 0o600
    assert store.get_for_user(alice_id, "NOTION_TOKEN") == "alice-only-token"
    assert store.get_for_user(bob_id, "NOTION_TOKEN") is None
    assert store.status_for_user(alice_id)["NOTION_TOKEN"]["configured"] is True
    assert store.status_for_user(alice_id)["NOTION_TOKEN"]["masked"] != "alice-only-token"
    with pytest.raises(InvalidToken):
        IntegrationSecrets(tmp_path, key=Fernet.generate_key()).get_for_user(alice_id, "NOTION_TOKEN")


def test_integration_secrets_reject_unknown_names_and_bad_ids(tmp_path):
    store = IntegrationSecrets(tmp_path, key=Fernet.generate_key())
    with pytest.raises(ValueError):
        store.set_for_user(str(uuid4()), "DEEPSEEK_API_KEY", "must-stay-server-managed")
    with pytest.raises(ValueError):
        store.get_for_user("../escape", "NOTION_TOKEN")


def test_integration_secret_store_fails_closed_without_server_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PA_USER_SECRETS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PA_USER_SECRETS_KEY"):
        IntegrationSecrets(tmp_path)


def test_integration_secret_key_rotation_reencrypts_with_previous_key(tmp_path, monkeypatch):
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    user_id = str(uuid4())
    old_store = IntegrationSecrets(tmp_path, key=old_key)
    old_store.set_for_user(user_id, "NOTION_TOKEN", "rotate-me")

    monkeypatch.setenv("PA_USER_SECRETS_KEY", new_key.decode("ascii"))
    monkeypatch.setenv("PA_USER_SECRETS_PREVIOUS_KEYS", old_key.decode("ascii"))
    rotated_store = IntegrationSecrets(tmp_path)
    assert rotated_store.get_for_user(user_id, "NOTION_TOKEN") == "rotate-me"

    ciphertext = (tmp_path / "users" / user_id / "integrations.enc").read_bytes()[3:]
    assert Fernet(new_key).decrypt(ciphertext)
    with pytest.raises(InvalidToken):
        Fernet(old_key).decrypt(ciphertext)


def test_concurrent_updates_preserve_different_secret_names(tmp_path):
    store = IntegrationSecrets(tmp_path, key=Fernet.generate_key())
    user_id = str(uuid4())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.set_for_user, user_id, "NOTION_TOKEN", "notion-value"),
            pool.submit(store.set_for_user, user_id, "TTS_API_KEY", "tts-value"),
        ]
        for future in futures:
            future.result()

    assert store.get_for_user(user_id, "NOTION_TOKEN") == "notion-value"
    assert store.get_for_user(user_id, "TTS_API_KEY") == "tts-value"


def test_server_fernet_key_is_not_loaded_from_project_dotenv(tmp_path, monkeypatch):
    from podcast_article import config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "PA_USER_SECRETS_KEY=not-a-server-environment-key\n"
        "PA_USER_SECRETS_PREVIOUS_KEYS=old-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("PA_USER_SECRETS_KEY", raising=False)

    config._load_dotenv()

    assert "PA_USER_SECRETS_KEY" not in __import__("os").environ
    assert "PA_USER_SECRETS_PREVIOUS_KEYS" not in __import__("os").environ


def test_user_secret_routes_never_return_or_store_plaintext(two_user_clients, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    monkeypatch.setenv("PA_USER_SECRETS_KEY", Fernet.generate_key().decode("ascii"))

    response = alice["client"].put(
        "/api/integrations/secrets/NOTION_TOKEN",
        json={"value": "per-user-notion-token"},
    )

    assert response.status_code == 200
    assert "per-user-notion-token" not in response.get_data(as_text=True)
    stored = (alice["workspace"].root / "integrations.enc").read_bytes()
    assert b"per-user-notion-token" not in stored
    assert alice["client"].get("/api/integrations").get_json()["secrets"]["NOTION_TOKEN"]["configured"]
    assert webapp.app.config["PLATFORM_STORE"].user_by_id(alice["account"].id) is not None


def test_member_mcp_management_and_execution_are_admin_only(two_user_clients, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    episode = alice["workspace"].output_root / "episode"
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "article.md").write_text("# article", encoding="utf-8")
    (episode / "meta.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(webapp.mcp_client, "list_tools", lambda **kwargs: pytest.fail("must not spawn MCP"))
    monkeypatch.setattr(webapp.mcp_client, "call_tool", lambda **kwargs: pytest.fail("must not spawn MCP"))

    responses = [
        alice["client"].get("/api/mcp/servers"),
        alice["client"].post("/api/mcp/servers/preset", json={"preset": "notion"}),
        alice["client"].post("/api/mcp/servers", json={"name": "evil", "command_line": "echo hi"}),
        alice["client"].post("/api/mcp/test", json={"command": "echo", "args": ["hi"]}),
        alice["client"].post("/api/mcp/call", json={"command": "echo", "tool": "x"}),
        alice["client"].post("/api/publish", json={"dir": "episode", "target": "mcp:evil:run"}),
        alice["client"].delete("/api/mcp/servers/evil"),
    ]

    assert [response.status_code for response in responses] == [403] * len(responses)


def test_admin_can_read_mcp_configuration(auth_system):
    webapp, _store, _admin, password = auth_system
    client = webapp.app.test_client()
    assert client.post("/api/auth/login", json={
        "username": "test-admin", "password": password,
    }).status_code == 200
    assert client.get("/api/mcp/servers").status_code == 200


def test_admin_mcp_notion_preset_keeps_token_encrypted(tmp_output, auth_system, monkeypatch):
    import webapp
    from cryptography.fernet import Fernet

    webapp_module, _store, admin, password = auth_system
    client = webapp_module.app.test_client()
    assert client.post("/api/auth/login", json={
        "username": "test-admin", "password": password,
    }).status_code == 200
    token = "admin-notion-secret-value"
    monkeypatch.setenv("PA_USER_SECRETS_KEY", Fernet.generate_key().decode("ascii"))

    response = client.post("/api/mcp/servers/preset", json={"preset": "notion", "secret": token})
    assert response.status_code == 200
    server_file = webapp.mcp_config.SERVERS_PATH
    encrypted_file = webapp.data_root() / "users" / admin.id / "integrations.enc"
    assert token.encode() not in server_file.read_bytes()
    assert token.encode() not in encrypted_file.read_bytes()
    assert b"${NOTION_TOKEN}" in server_file.read_bytes()

    seen = []
    monkeypatch.setattr(webapp.mcp_client, "list_tools", lambda **kwargs: seen.append(kwargs) or {"tools": []})
    assert client.post("/api/mcp/test", json={"name": "notion"}).status_code == 200
    assert seen[0]["env"]["NOTION_TOKEN"] == token

    episode = webapp.OUTPUT_ROOT / "episode"
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "article.md").write_text("# admin article", encoding="utf-8")
    (episode / "meta.json").write_text('{"title":"admin episode"}', encoding="utf-8")
    monkeypatch.setattr(
        webapp.mcp_client, "call_tool",
        lambda **kwargs: seen.append(kwargs) or {"ok": True, "text": "published"},
    )
    published = client.post("/api/publish", json={
        "dir": "episode", "target": "mcp:notion:create_page",
    })
    assert published.status_code == 200
    assert seen[-1]["env"]["NOTION_TOKEN"] == token


def test_builtin_notion_publish_uses_each_accounts_token_and_destination(two_user_clients, monkeypatch):
    import webapp
    from podcast_article import settings as settings_mod
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PA_USER_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    captured = []
    def fake_publish(ctx, *, target, template, integration):
        captured.append((webapp.g.current_user.id, integration))
        return {"ok": True}

    monkeypatch.setattr(webapp.publish_mod, "publish", fake_publish)
    for username, database, token in (
        ("alice", "alice-database", "alice-secret-token"),
        ("bob", "bob-database", "bob-secret-token"),
    ):
        account = two_user_clients[username]
        episode = account["workspace"].output_root / "same-episode"
        episode.mkdir(parents=True, exist_ok=True)
        (episode / "meta.json").write_text('{"title":"episode"}', encoding="utf-8")
        (episode / "article.md").write_text("# article", encoding="utf-8")
        settings_mod.save(notion={"database_id": database},
                          settings_path=account["workspace"].settings_path)
        assert account["client"].put(
            "/api/integrations/secrets/NOTION_TOKEN", json={"value": token},
        ).status_code == 200
        response = account["client"].post("/api/notion", json={"dir": "same-episode"})
        assert response.status_code == 200

    assert captured[0][1]["token"] == "alice-secret-token"
    assert captured[0][1]["database_id"] == "alice-database"
    assert captured[1][1]["token"] == "bob-secret-token"
    assert captured[1][1]["database_id"] == "bob-database"


def test_tts_key_is_resolved_from_member_encrypted_store(two_user_clients, monkeypatch):
    import webapp
    from cryptography.fernet import Fernet

    monkeypatch.setenv("PA_USER_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    alice = two_user_clients["alice"]
    assert alice["client"].put(
        "/api/integrations/secrets/TTS_API_KEY", json={"value": "alice-tts-secret"},
    ).status_code == 200

    assert webapp._tts_key(alice["workspace"]) == "alice-tts-secret"
    assert webapp._tts_key(two_user_clients["bob"]["workspace"]) == ""
