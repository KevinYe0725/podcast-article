"""Owner-preserving import of a single-user installation into account workspaces."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .platform_store import PlatformStore
from .workspace import workspace_for


_SINGLE_FILES = ("library.json", "queue.json", "feeds.json", "settings.json", "kb.sqlite")
_SECRET_NAME = re.compile(
    r"(?:secret|token|password|credential|authorization|api.?key|access.?key|private.?key)", re.I
)


@dataclass(frozen=True)
class MigrationReport:
    owner_id: str
    episode_count: int
    file_count: int
    source_paths: tuple[str, ...]
    already_applied: bool = False
    source_changed: bool = False


class LegacyMigrator:
    def __init__(self, *, data_root: Path, platform_store: PlatformStore):
        self.data_root = Path(data_root)
        self.platform_store = platform_store
        self.marker_path = self.data_root / "platform.sqlite"

    def _sources(self, source_root: Path, legacy_project_root: Path) -> list[tuple[Path, Path]]:
        source_root, legacy_project_root = Path(source_root), Path(legacy_project_root)
        output = source_root / "output"
        if not output.exists():
            output = legacy_project_root / "output"
        sources = [(output, Path("output"))] if output.exists() else []
        for name in _SINGLE_FILES:
            candidate = source_root / name
            if not candidate.exists():
                candidate = legacy_project_root / name
            if candidate.exists():
                sources.append((candidate, Path(name)))
        return sources

    @staticmethod
    def _files(sources: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
        result: list[tuple[Path, Path]] = []
        for source, relative in sources:
            if source.is_symlink():
                raise ValueError(f"symlinks are not supported in legacy data: {source}")
            if source.is_dir():
                for file in sorted(source.rglob("*")):
                    if file.is_symlink():
                        raise ValueError(f"symlinks are not supported in legacy data: {file}")
                    if file.is_file():
                        result.append((file, relative / file.relative_to(source)))
            elif source.is_file() and source.name not in {".env", "integrations.enc"}:
                result.append((source, relative))
        return result

    @staticmethod
    def _episode_count(sources: list[tuple[Path, Path]]) -> int:
        for source, relative in sources:
            if relative == Path("output"):
                return sum(1 for entry in source.iterdir() if entry.is_dir())
        return 0

    def _report(
        self, source_root: Path, legacy_project_root: Path, owner_id: str,
        already_applied: bool = False,
    ) -> MigrationReport:
        sources = self._sources(source_root, legacy_project_root)
        files = self._files(sources)
        stored_digest = self._stored_digest(owner_id)
        source_changed = stored_digest is not None and stored_digest != self._source_digest(sources)
        return MigrationReport(
            owner_id, self._episode_count(sources), len(files),
            tuple(str(path) for path, _ in files), already_applied, source_changed,
        )

    def dry_run(self, source_root: Path, legacy_project_root: Path, owner_id: str) -> MigrationReport:
        if self.platform_store.user_by_id(owner_id) is None:
            raise ValueError("owner account not found")
        return self._report(source_root, legacy_project_root, owner_id,
                            self._is_applied(owner_id))

    def _is_applied(self, owner_id: str) -> bool:
        return self._stored_digest(owner_id) is not None

    def _stored_digest(self, owner_id: str) -> str | None:
        if not self.marker_path.exists():
            return None
        with sqlite3.connect(self.marker_path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_migrations'").fetchone()
            if not exists:
                return None
            row = db.execute("SELECT source_digest FROM legacy_migrations WHERE owner_id=?", (owner_id,)).fetchone()
        return row[0] if row else None

    def _source_digest(self, sources: list[tuple[Path, Path]]) -> str:
        digest = hashlib.sha256()
        for source, relative in self._files(sources):
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _sanitize_settings(source: Path) -> bytes:
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid settings JSON: {source}") from exc

        def clean(item):
            if isinstance(item, dict):
                return {k: clean(v) for k, v in item.items() if not _SECRET_NAME.search(str(k))}
            if isinstance(item, list):
                return [clean(v) for v in item]
            return item

        return (json.dumps(clean(value), ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def apply(self, source_root: Path, legacy_project_root: Path, owner_id: str) -> MigrationReport:
        if self.platform_store.user_by_id(owner_id) is None:
            raise ValueError("owner account not found")
        report = self._report(source_root, legacy_project_root, owner_id, self._is_applied(owner_id))
        existing_digest = self._stored_digest(owner_id)
        if existing_digest is not None:
            if report.source_changed:
                raise ValueError("legacy source data changed after migration; destination was not modified")
            return report
        workspace = workspace_for(owner_id, self.data_root)
        files = self._files(self._sources(source_root, legacy_project_root))
        if self.data_root.is_symlink():
            raise ValueError(f"conflict at destination {self.data_root}")
        planned: list[tuple[Path, Path, bytes | None]] = []
        for source, relative in files:
            if relative.name.startswith(".") or relative.name in {".env", "integrations.enc"}:
                continue
            if relative == Path("queue.json"):
                continue  # queue entries are normalized into the owner's platform job queue
            content = self._sanitize_settings(source) if relative == Path("settings.json") else None
            target = workspace.root / relative
            for parent in target.parents:
                if parent == self.data_root.parent:
                    break
                if parent.is_symlink():
                    raise ValueError(f"conflict at destination {parent}")
                if parent.exists() and not parent.is_dir():
                    raise ValueError(f"conflict at destination {parent}")
            if target.exists():
                incoming = content if content is not None else source.read_bytes()
                if target.is_symlink() or not target.is_file() or target.read_bytes() != incoming:
                    raise ValueError(f"conflict at destination {target}")
            planned.append((source, target, content))

        self.data_root.mkdir(parents=True, exist_ok=True)
        workspace.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            for source, target, content in planned:
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temp = target.with_name(target.name + ".migrate-tmp")
                if content is None:
                    shutil.copyfile(source, temp)
                else:
                    temp.write_bytes(content)
                os.chmod(temp, 0o600)
                os.replace(temp, target)
            self._import_queue(Path(source_root), Path(legacy_project_root), owner_id)
            with sqlite3.connect(self.marker_path, timeout=10) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("""CREATE TABLE IF NOT EXISTS legacy_migrations(
                    owner_id TEXT PRIMARY KEY REFERENCES accounts(id),
                    completed_at REAL NOT NULL,
                    source_digest TEXT NOT NULL
                )""")
                digest = self._source_digest(self._sources(source_root, legacy_project_root))
                db.execute("INSERT OR IGNORE INTO legacy_migrations(owner_id, completed_at, source_digest) VALUES(?,?,?)",
                           (owner_id, time.time(), digest))
        except Exception:
            # Keep copied files so an interrupted import can safely resume; source remains untouched.
            raise
        return report

    def _import_queue(self, source_root: Path, legacy_project_root: Path, owner_id: str) -> None:
        candidate = source_root / "queue.json"
        if not candidate.exists():
            candidate = legacy_project_root / "queue.json"
        if not candidate.exists():
            return
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid queue JSON: {candidate}") from exc
        items = raw.get("items", []) if isinstance(raw, dict) else []
        jobs = []
        runtime_fields = {"id", "state", "error", "added_at", "started_at", "finished_at", "dir"}
        allowed_states = {"pending", "running", "done", "error", "skipped", "cancelled"}
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"].strip():
                continue
            payload = {key: value for key, value in item.items() if key not in runtime_fields}
            state = item.get("state") if item.get("state") in allowed_states else "pending"
            if state == "running":  # a process-local worker cannot survive migration
                state = "pending"
            old_id = str(item.get("id") or f"{item['url']}:{item.get('pick', 1)}:{index}")
            stable_id = "legacy-" + hashlib.sha256(f"{owner_id}:{old_id}".encode()).hexdigest()[:48]
            try:
                created_at = float(item.get("added_at", time.time()))
            except (TypeError, ValueError):
                created_at = time.time()
            if not math.isfinite(created_at):
                created_at = time.time()

            def optional_time(name: str):
                try:
                    value = float(item.get(name)) if item.get(name) is not None else None
                except (TypeError, ValueError):
                    return None
                return value if value is None or math.isfinite(value) else None
            jobs.append((
                stable_id, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), created_at,
                state, optional_time("started_at"), optional_time("finished_at"),
                str(item.get("error") or ""), item.get("dir"), len(jobs),
            ))
        if not jobs:
            return
        with self.platform_store._transaction() as db:
            if db.execute("SELECT 1 FROM accounts WHERE id=? AND enabled=1", (owner_id,)).fetchone() is None:
                raise ValueError("owner account is missing or disabled")
            next_position = db.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs WHERE owner_id=?", (owner_id,)
            ).fetchone()[0]
            for job_id, payload, created_at, state, started_at, finished_at, error, directory, offset in jobs:
                db.execute(
                    """INSERT OR IGNORE INTO jobs(
                    id, owner_id, payload_json, created_at, updated_at, position, state,
                    started_at, finished_at, error, dir_name
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (job_id, owner_id, payload, created_at, time.time(), int(next_position + offset),
                     state, started_at, finished_at, error, directory),
                )
