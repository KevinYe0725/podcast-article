"""Account-local avatar normalization and storage.

Avatars are deliberately kept outside the platform database.  The route only
ever uses the authenticated account workspace and stores one sanitized PNG,
so a profile image cannot introduce a user-controlled path or metadata into
the application.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import warnings
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps


MAX_AVATAR_BYTES = 5 * 1024 * 1024
# Current iPhone photos are commonly around 24 MP.  Allow those images while
# keeping a hard bound, and ask Pillow's JPEG decoder for a smaller draft
# before pixel operations so an upload does not need to occupy its full size.
MAX_AVATAR_PIXELS = 36_000_000
AVATAR_SIZE = 512
AVATAR_FILENAME = "avatar.png"
_SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP"}


class AvatarError(ValueError):
    """A safe, stable error that can be returned directly by the API."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def avatar_path(workspace) -> Path:
    """Return the only permitted avatar path for an authenticated workspace."""
    return workspace.root / AVATAR_FILENAME


def avatar_url(workspace) -> str | None:
    """Return a cache-busting URL for the current avatar, if one exists."""
    path = avatar_path(workspace)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return f"/api/auth/avatar?v={digest}"


def _read_upload(stream: BinaryIO, *, max_bytes: int | None = None) -> bytes:
    limit = MAX_AVATAR_BYTES if max_bytes is None else max(0, min(MAX_AVATAR_BYTES, int(max_bytes)))
    data = stream.read(limit + 1)
    if not isinstance(data, (bytes, bytearray)):
        raise AvatarError("invalid_avatar", "头像图片无效，请换一张图片")
    data = bytes(data)
    if len(data) > limit:
        message = "头像图片不能超过 5 MB" if limit == MAX_AVATAR_BYTES else "头像图片超过账号上传上限"
        raise AvatarError("avatar_too_large", message, 413)
    return data


def normalize_avatar(data: bytes) -> bytes:
    """Decode, orient, center-crop and re-encode an uploaded image.

    The image is opened once for cheap format/dimension checks and verified,
    then reopened for pixel processing because Pillow invalidates an image
    object after ``verify``.  The final image is copied into a fresh RGBA
    object before saving, which drops EXIF, ICC and text metadata.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise AvatarError("invalid_avatar", "头像图片无效，请换一张图片")
    if len(data) > MAX_AVATAR_BYTES:
        raise AvatarError("avatar_too_large", "头像图片不能超过 5 MB", 413)
    if not data:
        raise AvatarError("invalid_avatar", "头像图片无效，请换一张图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                image_format = (source.format or "").upper()
                if image_format not in _SUPPORTED_FORMATS:
                    raise AvatarError("unsupported_avatar", "头像只支持 JPG、PNG 或 WebP 图片")
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_AVATAR_PIXELS:
                    raise AvatarError("avatar_too_large", "头像图片尺寸过大，请选择更小的图片", 413)
                source.verify()

            with Image.open(io.BytesIO(data)) as source:
                if image_format == "JPEG":
                    source.draft("RGB", (2048, 2048))
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                side = min(width, height)
                left = (width - side) // 2
                top = (height - side) // 2
                cropped = oriented.crop((left, top, left + side, top + side))
                resized = cropped.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
                rgba = resized.convert("RGBA")
                clean = Image.new("RGBA", rgba.size)
                clean.paste(rgba)
                output = io.BytesIO()
                clean.save(output, format="PNG", optimize=True)
                return output.getvalue()
    except AvatarError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise AvatarError("avatar_too_large", "头像图片尺寸过大，请选择更小的图片", 413) from exc
    except (Image.UnidentifiedImageError, EOFError, SyntaxError, OSError, ValueError, MemoryError) as exc:
        raise AvatarError("invalid_avatar", "头像图片无效，请换一张图片") from exc


def save_avatar(workspace, data: bytes) -> str:
    """Atomically persist normalized bytes with owner-only permissions."""
    normalized = normalize_avatar(data)
    root = Path(workspace.root)
    root.mkdir(parents=True, exist_ok=True)
    target = avatar_path(workspace)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".avatar-", suffix=".tmp", dir=root, delete=False) as temp:
            temp_path = Path(temp.name)
            os.fchmod(temp.fileno(), 0o600)
            temp.write(normalized)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, target)
        os.chmod(target, 0o600)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    return hashlib.sha256(normalized).hexdigest()


def delete_avatar(workspace) -> None:
    """Remove only the account-local avatar file."""
    try:
        avatar_path(workspace).unlink()
    except FileNotFoundError:
        pass
