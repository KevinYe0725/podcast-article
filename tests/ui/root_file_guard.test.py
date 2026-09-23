"""Regression coverage for recursive UI-test data isolation checks."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from root_file_guard import differences, snapshot


def test_nested_data_artifact_is_detected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        before = snapshot(root)
        leaked = root / "data" / "users" / "test-user" / "session.json"
        leaked.parent.mkdir(parents=True)
        leaked.write_text("test session", encoding="utf-8")
        changed = differences(before, snapshot(root))
        assert changed == ["added: data/users/test-user/session.json"]


def test_same_size_and_mtime_content_change_is_detected() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sample = root / "workspace" / "state.json"
        sample.parent.mkdir()
        sample.write_text("before", encoding="utf-8")
        before = snapshot(root)
        old_stat = sample.stat()
        sample.write_text("after!", encoding="utf-8")
        import os
        os.utime(sample, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        changed = differences(before, snapshot(root))
        assert changed == ["modified: workspace/state.json"]


if __name__ == "__main__":
    test_nested_data_artifact_is_detected()
    test_same_size_and_mtime_content_change_is_detected()
    print("✓ 递归仓库文件守护测试通过")
