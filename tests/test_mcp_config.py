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
    monkeypatch.setenv("NOTION_TOKEN", "ntn_x")
    ids = {p["id"] for p in mc.preset_catalog()}
    assert {"notion", "podcast-article"} <= ids
    notion = next(p for p in mc.preset_catalog() if p["id"] == "notion")
    assert notion["added"] is False and notion["ready"] is True


def test_build_from_preset_with_secret():
    e = mc.build_from_preset("notion", "ntn_from_user")
    assert e["env"]["NOTION_TOKEN"] == "ntn_from_user"
    assert e["command"] == "notion-mcp-server"
    with pytest.raises(ValueError, match="未知的预设"):
        mc.build_from_preset("nope")
