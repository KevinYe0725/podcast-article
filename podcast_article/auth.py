"""Password hashing and same-origin redirect policy."""
from __future__ import annotations

from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


_PASSWORD_HASHER = PasswordHasher(memory_cost=19 * 1024, time_cost=2, parallelism=1)


def validate_new_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("password must be text")
    size = len(password.encode("utf-8"))
    if size < 12 or size > 256:
        raise ValueError("password must be 12 to 256 UTF-8 bytes")


def hash_password(password: str) -> str:
    validate_new_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(encoded_hash: str, password: str) -> bool:
    if not isinstance(encoded_hash, str) or not isinstance(password, str):
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(encoded_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError, TypeError, ValueError):
        return False


def safe_next_path(raw: str | None) -> str:
    """Return an app-local absolute path, rejecting all authority-changing forms."""
    if (
        not isinstance(raw, str)
        or not raw
        or raw.startswith("//")
        or "\\" in raw
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        return "/"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "") + (f"#{parsed.fragment}" if parsed.fragment else "")
