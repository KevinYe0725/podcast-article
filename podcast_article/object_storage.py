"""Persistent OSS audio storage for server-side transcription and playback."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class ObjectRef:
    key: str
    content_type: str
    size: int
    sha256: str


def _safe_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-+\.", ".", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-.")
    return normalized or "audio"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def account_prefix(user_id: str, *, base_prefix: str | None = None,
                   is_legacy_owner: bool = False) -> str:
    """Keep the existing prefix for the legacy owner; isolate every member below it."""
    base = (base_prefix or os.environ.get("OSS_PREFIX") or "podcast-article/audio").strip("/")
    if not base or any(part in {".", ".."} for part in base.split("/")):
        raise ValueError("invalid OSS_PREFIX")
    try:
        canonical_id = str(UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("user_id must be a UUID") from exc
    if canonical_id != str(user_id):
        raise ValueError("user_id must be a canonical UUID")
    return base if is_legacy_owner else f"{base}/users/{canonical_id}"


class ObjectStorage:
    """Small OSS wrapper with no deletion capability exposed to the pipeline."""

    def __init__(self, *, bucket=None, prefix: str | None = None):
        self.prefix = (prefix or os.environ.get("OSS_PREFIX") or "podcast-article/audio").strip("/")
        self.bucket = bucket or self._create_bucket()

    @classmethod
    def for_account(cls, user_id: str, *, legacy_owner_id: str | None, bucket=None,
                    base_prefix: str | None = None) -> "ObjectStorage":
        return cls(
            bucket=bucket,
            prefix=account_prefix(user_id, base_prefix=base_prefix,
                                  is_legacy_owner=(user_id == legacy_owner_id)),
        )

    def _validate_key(self, object_key: str) -> str:
        key = str(object_key or "").strip("/")
        if not key or any(part in {"", ".", ".."} for part in key.split("/")):
            raise ValueError("invalid OSS object key")
        if not key.startswith(f"{self.prefix}/"):
            raise ValueError("OSS object key is outside this account prefix")
        return key

    @staticmethod
    def _create_bucket():
        import oss2

        access_key = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
        secret = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
        bucket_name = os.environ.get("OSS_BUCKET", "").strip()
        endpoint = os.environ.get("OSS_ENDPOINT", "").strip()
        if not all((access_key, secret, bucket_name, endpoint)):
            raise RuntimeError(
                "OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_BUCKET、OSS_ENDPOINT 必须全部配置"
            )
        return oss2.Bucket(oss2.Auth(access_key, secret), endpoint, bucket_name)

    def object_key(self, audio_path: Path, episode_slug: str, *, sha256: str | None = None) -> str:
        filename = _safe_component(audio_path.name)
        digest = sha256 or _sha256(audio_path)
        return f"{self.prefix}/{_safe_component(episode_slug)}/{digest[:8]}-{filename}"

    def put_file(self, path: Path, object_key: str) -> ObjectRef:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        object_key = self._validate_key(object_key)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        digest = _sha256(path)
        self.bucket.put_object_from_file(
            object_key,
            str(path),
            headers={"Content-Type": content_type},
        )
        return ObjectRef(object_key, content_type, path.stat().st_size, digest)

    def exists(self, object_key: str) -> bool:
        object_key = self._validate_key(object_key)
        return bool(self.bucket.object_exists(object_key))

    def signed_url(self, object_key: str, *, expires: int = 900) -> str:
        if expires < 1:
            raise ValueError("expires must be positive")
        return self.bucket.sign_url("GET", self._validate_key(object_key), expires)
