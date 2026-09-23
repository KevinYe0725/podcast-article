"""Persistent OSS audio storage for server-side transcription and playback."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


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


class ObjectStorage:
    """Small OSS wrapper with no deletion capability exposed to the pipeline."""

    def __init__(self, *, bucket=None, prefix: str | None = None):
        self.prefix = (prefix or os.environ.get("OSS_PREFIX") or "podcast-article/audio").strip("/")
        self.bucket = bucket or self._create_bucket()

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
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        digest = _sha256(path)
        self.bucket.put_object_from_file(
            object_key,
            str(path),
            headers={"Content-Type": content_type},
        )
        return ObjectRef(object_key, content_type, path.stat().st_size, digest)

    def exists(self, object_key: str) -> bool:
        return bool(self.bucket.object_exists(object_key))

    def signed_url(self, object_key: str, *, expires: int = 900) -> str:
        if expires < 1:
            raise ValueError("expires must be positive")
        return self.bucket.sign_url("GET", object_key, expires)
