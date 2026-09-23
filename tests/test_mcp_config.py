"""MCP 配置：命令行解析、env 展开与打码、预设目录。"""
import pytest

from podcast_article import mcp_config as mc


def test_parse_command_line():
    assert mc.parse_command_line("notion-mcp-server") == ("notion-mcp-server", [])
    assert mc.parse_command_line("npx -y @scope/pkg --flag") == ("npx", ["-y", "@scope/pkg", "--flag"])
    with pytest.raises(ValueError, match="不能为空"):
        mc.parse_command_line("   ")


def test_parse_handles_quoted_args():
    assert mc.parse_command_line('uv --directory "/tmp/a b" run x') == ("uv", ["--directory", "/tmp/a b", "run", "x"])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "CONFIG_PATH", tmp_path / "mcp_servers.json")


def test_upsert_and_load_with_command_line():
    e = mc.upsert_server({"name": "demo", "command_line": "npx -y pkg", "env": {"TOKEN": "abc"}})
    assert e["command"] == "npx" and e["args"] == ["-y", "pkg"]
    assert [s["name"] for s in mc.load_servers()] == ["demo"]


def test_upsert_requires_name_and_command():
    with pytest.raises(ValueError, match="名称"):
        mc.upsert_server({"command_line": "x"})
    with pytest.raises(ValueError, match="不能为空"):
        mc.upsert_server({"name": "x", "command_line": "  "})


def test_upsert_replaces_same_name():
    mc.upsert_server({"name": "a", "command_line": "one"})
    mc.upsert_server({"name": "a", "command_line": "two"})
    servers = mc.load_servers()
    assert len(servers) == 1 and servers[0]["command"] == "two"


def test_delete_server():
    mc.upsert_server({"name": "a", "command_line": "x"})
    assert mc.delete_server("a") is True
    assert mc.delete_server("a") is False


def test_env_expansion_and_empty_dropped(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-value")
    entry = {"env": {"A": "${MY_TOKEN}", "B": "${NOT_SET_ANYWHERE}", "C": "literal"}}
    assert mc.resolved_env(entry) == {"A": "secret-value", "C": "literal"}


def test_mask_entry_hides_secret():
    masked = mc.mask_entry({"name": "n", "command": "c", "args": [],
                            "env": {"NOTION_TOKEN": "ntn_1234567890abcdef"}})
    assert "ntn_1234567890abcdef" not in str(masked)
    assert masked["env"]["NOTION_TOKEN"].startswith("ntn_12")
    assert masked["has_secret"] is True


def test_mask_keeps_variable_reference_visible():
    masked = mc.mask_entry({"name": "n", "command": "c", "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"}})
    assert masked["env"]["NOTION_TOKEN"] == "${NOTION_TOKEN}"


def test_preset_catalog_reports_state(monkeypatch):
    # 不能依赖真实 .env（CI 上没有该文件）：把「已配置的键」直接注入
    monkeypatch.setattr(mc, "read_env_keys", lambda: {"NOTION_TOKEN": "ntn_x"})
    ids = {p["id"] for p in mc.preset_catalog()}
    assert {"notion", "podcast-article"} <= ids
    notion = next(p for p in mc.preset_catalog() if p["id"] == "notion")
    assert notion["added"] is False and notion["ready"] is True

    # 没有密钥时就绪状态应为 False
    monkeypatch.setattr(mc, "read_env_keys", lambda: {})
    notion2 = next(p for p in mc.preset_catalog() if p["id"] == "notion")
    assert notion2["ready"] is False


def test_build_from_preset_keeps_secret_as_environment_reference():
    e = mc.build_from_preset("notion")
    assert e["env"]["NOTION_TOKEN"] == "${NOTION_TOKEN}"
    assert e["command"] == "notion-mcp-server"
    with pytest.raises(ValueError, match="encrypted per-account"):
        mc.build_from_preset("notion", "ntn_from_user")
    with pytest.raises(ValueError, match="未知的预设"):
        mc.build_from_preset("nope")


def test_resolved_env_accepts_encrypted_account_values():
    e = mc.build_from_preset("notion")
    assert mc.resolved_env(e, extra_env={"NOTION_TOKEN": "account-token"}) == {
        "NOTION_TOKEN": "account-token",
    }


def test_preset_is_not_ready_when_its_encrypted_reference_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "CONFIG_PATH", tmp_path / "mcp_servers.json")
    monkeypatch.setattr(mc, "read_env_keys", lambda: {})
    mc.save_servers([mc.build_from_preset("notion")])

    missing = next(p for p in mc.preset_catalog() if p["id"] == "notion")
    configured = next(p for p in mc.preset_catalog(extra_env={"NOTION_TOKEN": "admin-token"})
                      if p["id"] == "notion")

    assert missing["ready"] is False
    assert configured["ready"] is True


def test_default_admin_config_is_private_and_under_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "CONFIG_PATH", mc._DEFAULT_CONFIG_PATH)
    monkeypatch.setattr(mc, "SERVERS_PATH", mc._DEFAULT_CONFIG_PATH)

    mc.save_servers([{"name": "safe", "command": "echo", "args": [], "env": {}, "note": ""}])

    path = tmp_path / "data" / "mcp_servers.json"
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_legacy_project_mcp_config_moves_to_private_data_root(tmp_path, monkeypatch):
    legacy = tmp_path / "project" / "mcp_servers.json"
    legacy.parent.mkdir()
    legacy.write_text(
        '{"servers":[{"name":"old","command":"echo","args":[],"env":{"TOKEN":"hidden"},"note":""}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "_DEFAULT_CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "SERVERS_PATH", legacy)

    loaded = mc.load_servers()

    destination = tmp_path / "data" / "mcp_servers.json"
    assert loaded[0]["name"] == "old"
    assert destination.exists() and destination.stat().st_mode & 0o777 == 0o600
    assert not legacy.exists()


@pytest.mark.parametrize("contents", ["not-json", '{"unexpected":"shape"}'])
def test_unmigratable_legacy_mcp_config_is_made_private(tmp_path, monkeypatch, contents):
    legacy = tmp_path / "project" / "mcp_servers.json"
    legacy.parent.mkdir()
    legacy.write_text(contents, encoding="utf-8")
    legacy.chmod(0o644)
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "_DEFAULT_CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "SERVERS_PATH", legacy)

    assert mc.load_servers() == []

    assert legacy.exists()
    assert legacy.stat().st_mode & 0o777 == 0o600


def test_legacy_mcp_config_permission_failure_stops_migration(tmp_path, monkeypatch):
    legacy = tmp_path / "project" / "mcp_servers.json"
    legacy.parent.mkdir()
    legacy.write_text("not-json", encoding="utf-8")
    legacy.chmod(0o644)
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "_DEFAULT_CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "SERVERS_PATH", legacy)

    def denied_fchmod(_fd, _mode):
        raise PermissionError("chmod denied")

    monkeypatch.setattr(mc.os, "fchmod", denied_fchmod)
    with pytest.raises(RuntimeError, match="secure"):
        mc.load_servers()


def test_legacy_mcp_config_symlink_is_not_migrated(tmp_path, monkeypatch):
    legacy = tmp_path / "project" / "mcp_servers.json"
    target = tmp_path / "external.json"
    legacy.parent.mkdir()
    target.write_text('{"servers":[{"name":"external","command":"echo"}]}', encoding="utf-8")
    target.chmod(0o644)
    legacy.symlink_to(target)
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "_DEFAULT_CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "CONFIG_PATH", legacy)
    monkeypatch.setattr(mc, "SERVERS_PATH", legacy)

    with pytest.raises(RuntimeError, match="regular file"):
        mc.load_servers()

    assert legacy.is_symlink()
    assert target.exists()
    assert not (tmp_path / "data" / "mcp_servers.json").exists()


def test_default_mcp_config_lives_under_data_root(tmp_path, monkeypatch):
    import podcast_article.mcp_config as mc

    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("PA_MCP_CONFIG", raising=False)
    monkeypatch.setattr(mc, "CONFIG_PATH", mc._DEFAULT_CONFIG_PATH)
    monkeypatch.setattr(mc, "SERVERS_PATH", mc._DEFAULT_CONFIG_PATH)

    mc.save_servers([])

    target = tmp_path / "data" / "mcp_servers.json"
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600


def test_ui_runner_uses_temporary_mcp_config_for_server(tmp_path):
    from pathlib import Path

    runner = Path(__file__).parent / "ui" / "run.sh"
    source = runner.read_text(encoding="utf-8")

    assert 'export PA_MCP_CONFIG="$PA_DATA_ROOT/mcp_servers.json"' in source
    assert 'PA_MCP_CONFIG="$PA_MCP_CONFIG"' in source
