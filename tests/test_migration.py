import json
import sqlite3
from pathlib import Path

import pytest

from podcast_article.migration import LegacyMigrator
from podcast_article.platform_store import PlatformStore
from podcast_article import admin


@pytest.fixture
def legacy_tree(tmp_path):
    project = tmp_path / "legacy-project"
    source = tmp_path / "legacy-data"
    project.mkdir()
    (source / "output" / "ep-one").mkdir(parents=True)
    (source / "output" / "ep-two").mkdir(parents=True)
    (source / "output" / "ep-one" / "article.md").write_text("one", encoding="utf-8")
    (source / "output" / "ep-one" / "meta.json").write_text(
        json.dumps({"audio_storage": "oss", "audio_object_key": "podcasts/one/audio.mp3"}), encoding="utf-8"
    )
    (source / "output" / "ep-two" / "article.md").write_text("two", encoding="utf-8")
    (project / "library.json").write_text(json.dumps({"categories": [], "assignments": {"ep-one": "c1"}}), encoding="utf-8")
    (project / "settings.json").write_text(json.dumps({
        "profile": {"name": "Kevin"}, "notion": {"database_id": "db-id", "token": "provider-secret"}
    }), encoding="utf-8")
    (project / "queue.json").write_text(json.dumps({"items": [
        {"id": "q-pending", "url": "https://example.com/new", "state": "pending", "added_at": 2},
        {"id": "q-done", "url": "https://example.com/old", "state": "done", "added_at": 3,
         "finished_at": 4, "dir": "old-episode"},
    ]}), encoding="utf-8")
    (project / ".env").write_text("DEEPSEEK_API_KEY=must-not-copy\n", encoding="utf-8")
    store = PlatformStore(tmp_path / "data" / "platform.sqlite")
    account = store.bootstrap_admin("kevin", "hashed", now=1)
    return source, project, tmp_path / "data", account.id


@pytest.fixture
def migration(legacy_tree):
    source, project, data_root, _ = legacy_tree
    return LegacyMigrator(data_root=data_root, platform_store=PlatformStore(data_root / "platform.sqlite"))


def test_legacy_migration_dry_run_does_not_change_source(legacy_tree, migration):
    source, project, _, owner_id = legacy_tree
    before = {p: p.read_bytes() for root in (source, project) for p in root.rglob("*") if p.is_file()}
    report = migration.dry_run(source, project, owner_id)
    assert report.episode_count == 2
    assert {p: p.read_bytes() for p in before} == before
    assert not (source.parent / "data" / "users" / owner_id).exists()


def test_legacy_migration_imports_owner_data_and_preserves_oss_key(legacy_tree, migration):
    source, project, data_root, owner_id = legacy_tree
    before = {p: p.read_bytes() for root in (source, project) for p in root.rglob("*") if p.is_file()}
    migration.apply(source, project, owner_id)
    workspace = data_root / "users" / owner_id
    metadata = json.loads((workspace / "output" / "ep-one" / "meta.json").read_text())
    assert metadata["audio_object_key"] == "podcasts/one/audio.mp3"
    assert json.loads((workspace / "library.json").read_text())["assignments"] == {"ep-one": "c1"}
    assert json.loads((workspace / "settings.json").read_text())["profile"]["name"] == "Kevin"
    assert "token" not in json.loads((workspace / "settings.json").read_text())["notion"]
    assert not (workspace / ".env").exists()
    assert {p: p.read_bytes() for p in before} == before


def test_legacy_migration_rerun_is_idempotent(legacy_tree, migration):
    source, project, data_root, owner_id = legacy_tree
    migration.apply(source, project, owner_id)
    first = (data_root / "users" / owner_id / "output" / "ep-one" / "article.md").read_bytes()
    report = migration.apply(source, project, owner_id)
    assert report.already_applied is True
    assert (data_root / "users" / owner_id / "output" / "ep-one" / "article.md").read_bytes() == first


def test_legacy_migration_rejects_changed_source_after_completion(legacy_tree, migration):
    source, project, _data_root, owner_id = legacy_tree
    migration.apply(source, project, owner_id)
    (source / "output" / "ep-one" / "article.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source data changed"):
        migration.apply(source, project, owner_id)


def test_legacy_migration_rejects_conflicting_destination(legacy_tree, migration):
    source, project, data_root, owner_id = legacy_tree
    target = data_root / "users" / owner_id / "output" / "ep-one" / "article.md"
    target.parent.mkdir(parents=True)
    target.write_text("different", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        migration.apply(source, project, owner_id)
    assert target.read_text() == "different"


def test_legacy_migration_rejects_file_destination_ancestor(legacy_tree, migration):
    _source, _project, data_root, owner_id = legacy_tree
    workspace = data_root / "users" / owner_id
    workspace.mkdir(parents=True)
    (workspace / "output").write_text("occupied", encoding="utf-8")
    with pytest.raises(ValueError, match="conflict"):
        migration.apply(_source, _project, owner_id)


def test_legacy_migration_records_completion_in_platform_store(legacy_tree, migration):
    source, project, data_root, owner_id = legacy_tree
    migration.apply(source, project, owner_id)
    with sqlite3.connect(data_root / "platform.sqlite") as db:
        row = db.execute("SELECT owner_id FROM legacy_migrations").fetchone()
    assert row == (owner_id,)


def test_legacy_migration_imports_queue_state_for_owner(legacy_tree, migration):
    _source, _project, data_root, owner_id = legacy_tree
    migration.apply(_source, _project, owner_id)
    jobs = PlatformStore(data_root / "platform.sqlite").list_jobs(owner_id, limit=10)
    by_url = {job.payload["url"]: job for job in jobs}
    assert by_url["https://example.com/new"].state == "pending"
    assert by_url["https://example.com/old"].state == "done"
    assert by_url["https://example.com/old"].dir_name == "old-episode"


def test_admin_migrate_legacy_dry_run_resolves_owner_and_prints_inventory(legacy_tree, monkeypatch, capsys):
    source, project, data_root, owner_id = legacy_tree
    store = PlatformStore(data_root / "platform.sqlite")
    monkeypatch.setattr(admin, "_store", lambda: store)
    monkeypatch.setattr(admin, "data_root", lambda: data_root)
    assert admin.main(["migrate-legacy", "--source-root", str(source),
                       "--legacy-project-root", str(project), "--owner", "kevin", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert owner_id in output
    assert "2 episodes" in output
