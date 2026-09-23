"""Recursive file manifest used to ensure UI tests keep user data in $TMP."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


VOLATILE_DIRECTORIES = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache"}


def _fingerprint(path: Path, info: os.stat_result) -> dict[str, Any]:
    if stat.S_ISLNK(info.st_mode):
        digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        # Avoid reading the ignored root MCP config, which may contain credentials.
        digest = None if path.name == "mcp_servers.json" else _file_digest(path)
        kind = "file"
    else:
        digest = None
        kind = "other"
    return {"kind": kind, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": digest}


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot(root: Path) -> dict[str, dict[str, Any]]:
    """Record all files recursively except known high-churn dependency/cache dirs."""
    root = Path(root).resolve()
    manifest: dict[str, dict[str, Any]] = {}
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        directories[:] = sorted(
            name for name in directories
            if name not in VOLATILE_DIRECTORIES and not (base / name).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            relative = path.relative_to(root).as_posix()
            manifest[relative] = _fingerprint(path, info)
        # Directory symlinks are not traversed, but their own paths remain monitored.
        for name in sorted(os.listdir(base)):
            path = base / name
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                try:
                    manifest[relative] = _fingerprint(path, path.lstat())
                except FileNotFoundError:
                    pass
    return manifest


def differences(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[str]:
    changes = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append(f"added: {path}")
        elif path not in after:
            changes.append(f"removed: {path}")
        elif before[path] != after[path]:
            changes.append(f"modified: {path}")
    return changes


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: root_file_guard.py snapshot ROOT | verify ROOT MANIFEST", file=sys.stderr)
        return 2
    command, root = argv[1], Path(argv[2])
    if command == "snapshot" and len(argv) == 3:
        print(json.dumps(snapshot(root), sort_keys=True))
        return 0
    if command == "verify" and len(argv) == 4:
        manifest = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
        changed = differences(manifest, snapshot(root))
        if changed:
            print("✕ UI 测试期间仓库文件发生变化：" + ", ".join(changed))
            return 1
        print("✓ 仓库文件大小、修改时间和内容摘要保持不变")
        return 0
    print("usage: root_file_guard.py snapshot ROOT | verify ROOT MANIFEST", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
