"""Password hashing and same-origin redirect policy."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


_PASSWORD_HASHER = PasswordHasher(memory_cost=19 * 1024, time_cost=2, parallelism=1)
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("not-a-real-podcast-article-password")


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


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _request_store():
    from flask import current_app

    configured = current_app.config.get("PLATFORM_STORE")
    if configured is not None:
        return configured
    from .platform_store import PlatformStore
    from .workspace import data_root

    path = data_root() / "platform.sqlite"
    cached = current_app.extensions.get("podcast_platform_store")
    if cached is not None and cached.path == path:
        return cached
    store = PlatformStore(path)
    current_app.extensions["podcast_platform_store"] = store
    return store


def resolve_request(request):
    """Resolve only a server-side session cookie; never trust IDs in request data."""
    raw_token = request.cookies.get("pa_session", "")
    if not raw_token or len(raw_token) > 128:
        return None
    return _request_store().resolve_session(token_hash(raw_token), now=time.time())


def csrf_request_valid(request, account=None) -> bool:
    cookie_token = request.cookies.get("pa_csrf", "")
    header_token = request.headers.get("X-CSRF-Token", "")
    if not cookie_token or not header_token or len(cookie_token) > 128 or len(header_token) > 128:
        return False
    if not hmac.compare_digest(cookie_token, header_token):
        return False
    if account is None:
        return True
    raw_session = request.cookies.get("pa_session", "")
    if not raw_session:
        return False
    return _request_store().session_csrf_matches(token_hash(raw_session), token_hash(cookie_token))


def issue_csrf(request, account=None) -> str:
    """Reuse a valid double-submit token, or rotate a server-bound session token."""
    existing = request.cookies.get("pa_csrf", "")
    raw_session = request.cookies.get("pa_session", "")
    if account is None and existing and len(existing) <= 128:
        return existing
    if account is not None and raw_session and existing:
        session_hash = token_hash(raw_session)
        store = _request_store()
        existing_hash = token_hash(existing)
        if store.session_csrf_matches(session_hash, existing_hash):
            return existing
        replacement = new_token()
        store.set_session_csrf(session_hash, token_hash(replacement))
        return replacement
    return new_token()
