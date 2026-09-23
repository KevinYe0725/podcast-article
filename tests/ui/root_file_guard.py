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
PRIVATE_PATH_COMPONENTS = {
    "data", "output", "uploads", "storage", "private", "secrets", "credentials",
    ".ssh", ".aws", ".azure", ".npm", ".config",
}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".p8"}
PRIVATE_FILENAMES = {
    ".npmrc", ".pypirc", ".netrc", "auth.json", "credentials.json", "secrets.json",
    "library.json", "settings.json", "feeds.json", "queue.json", "usage.json", "meta.json",
    "transcript.json", "mcp_servers.json",
}
HASHABLE_CODE_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm",
    ".sh", ".bash", ".toml", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".lock",
}
HASHABLE_CONFIG_NAMES = {
    "package.json", "package-lock.json", "tsconfig.json", "jsconfig.json",
    "mcp_servers.example.json",
}


def _is_private_path(relative: Path) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    name = parts[-1] if parts else ""
    if any(part in PRIVATE_PATH_COMPONENTS for part in parts):
        return True
    if name in PRIVATE_FILENAMES or name == ".env" or name.startswith(".env."):
        return True
    if any(marker in name for marker in ("secret", "credential", "_key", "-key", "token")) \
            or Path(name).suffix in PRIVATE_SUFFIXES:
        return True
    return False


def _is_safe_code_or_config(relative: Path) -> bool:
    if _is_private_path(relative):
        return False
    name = relative.name.lower()
    return (
        Path(name).suffix in HASHABLE_CODE_SUFFIXES
        or name in HASHABLE_CONFIG_NAMES
        or name.endswith(".config.json")
    )


def _fingerprint(path: Path, info: os.stat_result, relative: Path) -> dict[str, Any]:
    if stat.S_ISLNK(info.st_mode):
        digest = (hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
                  if _is_safe_code_or_config(relative) else None)
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        digest = _file_digest(path) if _is_safe_code_or_config(relative) else None
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
            manifest[relative] = _fingerprint(path, info, Path(relative))
        # Directory symlinks are not traversed, but their own paths remain monitored.
        for name in sorted(os.listdir(base)):
            path = base / name
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                try:
                    manifest[relative] = _fingerprint(path, path.lstat(), Path(relative))
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
