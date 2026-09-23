"""Regression coverage for recursive UI-test data isolation checks."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import root_file_guard
from root_file_guard import differences, snapshot


def test_nested_data_artifact_is_detected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        before = snapshot(root)
        leaked = root / "data" / "users" / "test-user" / "session.json"
        leaked.parent.mkdir(parents=True)
        leaked.write_text("test session", encoding="utf-8")
        changed = differences(before, snapshot(root))
        assert changed == ["added: data/users/test-user/session.json"]


def test_same_size_and_mtime_content_change_is_detected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        sample = root / "workspace" / "state.py"
        sample.parent.mkdir()
        sample.write_text("before", encoding="utf-8")
        before = snapshot(root)
        old_stat = sample.stat()
        sample.write_text("after!", encoding="utf-8")
        import os
        os.utime(sample, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        changed = differences(before, snapshot(root))
        assert changed == ["modified: workspace/state.py"]


def test_private_paths_use_metadata_without_reading_contents() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        private_paths = [
            root / ".env",
            root / ".local-secrets.env",
            root / "data" / "users" / "session.json",
            root / "output" / "article.md",
            root / "service.key",
            root / "mcp_servers.json",
            root / "library.json",
        ]
        for path in private_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic private fixture", encoding="utf-8")
        safe_path = root / "tests" / "sample.py"
        safe_path.parent.mkdir()
        safe_path.write_text("print('safe')", encoding="utf-8")
        safe_config = root / "package.json"
        safe_config.write_text("{}", encoding="utf-8")
        notes = root / "notes.md"
        notes.write_text("synthetic notes", encoding="utf-8")

        original_digest = root_file_guard._file_digest
        private_relpaths = {path.relative_to(root).as_posix() for path in private_paths}
        digested: list[str] = []

        def deny_private_digest(path: Path) -> str:
            relative = path.relative_to(root).as_posix()
            if relative in private_relpaths:
                raise AssertionError(f"private file content was read: {relative}")
            digested.append(relative)
            return original_digest(path)

        root_file_guard._file_digest = deny_private_digest
        try:
            manifest = snapshot(root)
        finally:
            root_file_guard._file_digest = original_digest

        assert digested == ["package.json", "tests/sample.py"]
        assert all(manifest[path.relative_to(root).as_posix()]["sha256"] is None for path in private_paths)
        assert manifest["tests/sample.py"]["sha256"] is not None
        assert manifest["package.json"]["sha256"] is not None
        assert manifest["notes.md"]["sha256"] is None


if __name__ == "__main__":
    test_nested_data_artifact_is_detected()
    test_same_size_and_mtime_content_change_is_detected()
    test_private_paths_use_metadata_without_reading_contents()
    print("✓ 递归仓库文件守护测试通过")
