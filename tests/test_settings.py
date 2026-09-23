"""设置存储：个人资料、生成默认值、.env 写入语义（空值不清空、保留注释）。"""
import pytest

from podcast_article import settings as st


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "SETTINGS_PATH", tmp_path / "settings.json")
    env = tmp_path / ".env"
    env.write_text(
        "# DeepSeek 密钥（保留这行注释）\nDEEPSEEK_API_KEY=sk-old\n\n# 注释\nNOTION_TOKEN=ntn-old\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "ENV_PATH", env)
    for k in (*st.MANAGED_KEYS, "NOTION_TOKEN", "NOTION_DATABASE_ID",
              "NOTION_PARENT_PAGE_ID", "TTS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return env


def test_defaults_when_no_file():
    data = st.load()
    assert data["profile"]["name"] == ""
    assert data["generation"]["length_mode"] == "standard"
    assert data["generation"]["auto_polish"] is False


def test_save_profile_and_unknown_keys_ignored():
    saved = st.save(profile={"name": "Kevin", "interests": "AI", "hacker": "nope"})
    assert saved["profile"]["name"] == "Kevin"
    assert "hacker" not in saved["profile"]
    assert st.load()["profile"]["interests"] == "AI"


def test_max_chars_coerced_to_int():
    st.save(generation={"max_chars": "42000"})
    assert st.load()["generation"]["max_chars"] == 42000
    st.save(generation={"max_chars": "不是数字"})
    assert st.load()["generation"]["max_chars"] == st.GENERATION_DEFAULTS["max_chars"]


def test_profile_text_empty_and_filled():
    assert st.profile_text() == ""
    st.save(profile={"name": "Kevin", "interests": "AI 基础设施"})
    text = st.profile_text()
    assert "Kevin" in text and "AI 基础设施" in text


def test_update_env_replaces_in_place_and_keeps_comments(isolated):
    changed = st.update_env({"DEEPSEEK_API_KEY": "sk-new"})
    assert changed == ["DEEPSEEK_API_KEY"]
    content = isolated.read_text(encoding="utf-8")
    assert "sk-new" in content and "sk-old" not in content
    assert "# DeepSeek 密钥（保留这行注释）" in content
    assert content.count("DEEPSEEK_API_KEY") == 1


def test_update_env_empty_value_keeps_existing(isolated):
    assert st.update_env({"NOTION_TOKEN": "   "}) == []
    assert "ntn-old" in isolated.read_text(encoding="utf-8")


def test_update_env_does_not_store_personal_integration_settings(isolated):
    assert st.update_env({"NOTION_DATABASE_ID": "db-123", "TTS_API_KEY": "personal-tts"}) == []
    assert "NOTION_DATABASE_ID=db-123" not in isolated.read_text(encoding="utf-8")
    assert "personal-tts" not in isolated.read_text(encoding="utf-8")


def test_update_env_ignores_unmanaged_keys(isolated):
    assert st.update_env({"PATH": "/evil"}) == []
    assert "/evil" not in isolated.read_text(encoding="utf-8")


def test_secret_status_masks(isolated):
    status = st.secret_status()
    assert status["DEEPSEEK_API_KEY"]["configured"] is True
    assert "sk-old" not in status["DEEPSEEK_API_KEY"]["masked"]
    assert status["NOTION_PARENT_PAGE_ID"]["configured"] is False


def test_mask_short_and_long():
    assert st.mask("") == ""
    assert st.mask("short") == "•••"
    assert "…" in st.mask("x" * 40)


def test_notion_destinations_are_stored_per_settings_file(tmp_path):
    first = tmp_path / "alice.json"
    second = tmp_path / "bob.json"

    st.save(notion={"database_id": "alice-db"}, settings_path=first)
    st.save(notion={"parent_page_id": "bob-page"}, settings_path=second)

    assert st.load(settings_path=first)["notion"] == {"database_id": "alice-db", "parent_page_id": ""}
    assert st.load(settings_path=second)["notion"] == {"database_id": "", "parent_page_id": "bob-page"}
