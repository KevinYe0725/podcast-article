from __future__ import annotations

import pytest

from podcast_article.workspace import data_root, workspace_for


def test_workspace_paths_are_distinct_and_confined(tmp_path):
    alice = workspace_for("11111111-1111-4111-8111-111111111111", tmp_path)
    bob = workspace_for("22222222-2222-4222-8222-222222222222", tmp_path)

    assert alice.account_id != bob.account_id
    assert alice.root == tmp_path / "users" / alice.account_id
    assert alice.output_root != bob.output_root
    assert alice.kb_path != bob.kb_path
    assert alice.root.is_relative_to(tmp_path)
    assert alice.output_root == alice.root / "output"
    assert alice.library_path == alice.root / "library.json"
    assert alice.settings_path == alice.root / "settings.json"
    assert alice.feeds_path == alice.root / "feeds.json"
    assert alice.integrations_path == alice.root / "integrations.json"


@pytest.mark.parametrize("account_id", ["../outside", "not-a-uuid", "11111111-1111-4111-8111-11111111111", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])
def test_workspace_rejects_noncanonical_account_ids(account_id, tmp_path):
    with pytest.raises(ValueError):
        workspace_for(account_id, tmp_path)


def test_data_root_uses_environment_override_and_project_default(monkeypatch, tmp_path):
    from podcast_article import workspace

    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path / "configured"))
    assert data_root() == tmp_path / "configured"

    monkeypatch.delenv("PA_DATA_ROOT")
    monkeypatch.setattr(workspace, "PROJECT_ROOT", tmp_path)
    assert data_root() == tmp_path / "data"
