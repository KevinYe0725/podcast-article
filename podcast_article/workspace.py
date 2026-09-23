"""Explicit, per-account filesystem locations for web request data."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .config import PROJECT_ROOT


@dataclass(frozen=True)
class WorkspacePaths:
    account_id: str
    root: Path
    output_root: Path
    library_path: Path
    settings_path: Path
    feeds_path: Path
    kb_path: Path
    integrations_path: Path


def data_root() -> Path:
    """Return the configured data root without creating it."""
    configured = os.environ.get("PA_DATA_ROOT")
    return Path(configured).expanduser() if configured else PROJECT_ROOT / "data"


def workspace_for(account_id: str, data_root: Path) -> WorkspacePaths:
    """Build confined workspace paths for a canonical, lowercase UUID account id."""
    if not isinstance(account_id, str):
        raise ValueError("account id must be a canonical UUID")
    try:
        canonical = str(UUID(account_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("account id must be a canonical UUID") from exc
    if canonical != account_id:
        raise ValueError("account id must be a canonical UUID")

    root = Path(data_root) / "users" / canonical
    return WorkspacePaths(
        account_id=canonical,
        root=root,
        output_root=root / "output",
        library_path=root / "library.json",
        settings_path=root / "settings.json",
        feeds_path=root / "feeds.json",
        kb_path=root / "kb.sqlite",
        integrations_path=root / "integrations.json",
    )
