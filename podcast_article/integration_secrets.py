"""Encrypted per-account credentials for external integrations."""
from __future__ import annotations

import json
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

_ALLOWED_NAMES = frozenset({"NOTION_TOKEN", "TTS_API_KEY"})
_FORMAT = b"v1:"


def _mask(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "•••"


class IntegrationSecrets:
    """Encrypt each user's integration values under the server-only Fernet key."""

    def __init__(self, data_root: Path, key: bytes | str | None = None):
        self.data_root = Path(data_root)
        raw_key = key if key is not None else os.environ.get("PA_USER_SECRETS_KEY")
        if isinstance(raw_key, str):
            raw_key = raw_key.strip().encode("ascii")
        if not raw_key:
            raise RuntimeError("PA_USER_SECRETS_KEY is not configured")
        try:
            self._fernet = Fernet(raw_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("PA_USER_SECRETS_KEY must be a valid Fernet key") from exc
        self._decryptors = [self._fernet]
        if key is None:
            for previous in os.environ.get("PA_USER_SECRETS_PREVIOUS_KEYS", "").split(","):
                previous = previous.strip()
                if not previous:
                    continue
                try:
                    self._decryptors.append(Fernet(previous.encode("ascii")))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "PA_USER_SECRETS_PREVIOUS_KEYS contains an invalid Fernet key"
                    ) from exc

    @staticmethod
    def _validate_name(name: str) -> str:
        value = str(name or "").strip().upper()
        if value not in _ALLOWED_NAMES:
            raise ValueError("unsupported integration secret")
        return value

    @staticmethod
    def _validate_user_id(user_id: str) -> str:
        try:
            canonical = str(UUID(str(user_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("user_id must be a UUID") from exc
        if canonical != str(user_id):
            raise ValueError("user_id must be a canonical UUID")
        return canonical

    def _path_for(self, user_id: str) -> Path:
        canonical = self._validate_user_id(user_id)
        return self.data_root / "users" / canonical / "integrations.enc"

    @contextmanager
    def _user_lock(self, user_id: str):
        path = self._path_for(user_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        lock_path = path.with_suffix(path.suffix + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read_locked(self, user_id: str) -> tuple[dict[str, str], bool]:
        path = self._path_for(user_id)
        if not path.exists():
            return {}, False
        raw = path.read_bytes()
        if not raw.startswith(_FORMAT):
            raise ValueError("unsupported integration secret file version")
        encrypted = raw[len(_FORMAT):]
        decoded = None
        needs_rotation = False
        for index, decryptor in enumerate(self._decryptors):
            try:
                decoded = decryptor.decrypt(encrypted)
                needs_rotation = index > 0
                break
            except InvalidToken:
                continue
        if decoded is None:
            raise InvalidToken
        data = json.loads(decoded.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid integration secret payload")
        values = {
            self._validate_name(name): value
            for name, value in data.items()
            if isinstance(value, str) and value
        }
        return values, needs_rotation

    def _write_locked(self, user_id: str, values: dict[str, str]) -> None:
        path = self._path_for(user_id)
        plaintext = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ciphertext = _FORMAT + self._fernet.encrypt(plaintext)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                directory_fd = os.open(
                    path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    def _load(self, user_id: str) -> dict[str, str]:
        canonical = self._validate_user_id(user_id)
        with self._user_lock(canonical):
            values, needs_rotation = self._read_locked(canonical)
            if needs_rotation:
                self._write_locked(canonical, values)
            return values

    def set_for_user(self, user_id: str, name: str, value: str) -> None:
        key = self._validate_name(name)
        canonical = self._validate_user_id(user_id)
        with self._user_lock(canonical):
            values, _needs_rotation = self._read_locked(canonical)
            secret = str(value or "").strip()
            if secret:
                values[key] = secret
            else:
                values.pop(key, None)
            self._write_locked(canonical, values)

    def get_for_user(self, user_id: str, name: str) -> str | None:
        key = self._validate_name(name)
        return self._load(user_id).get(key)

    def delete_for_user(self, user_id: str, name: str) -> bool:
        key = self._validate_name(name)
        canonical = self._validate_user_id(user_id)
        with self._user_lock(canonical):
            values, _needs_rotation = self._read_locked(canonical)
            if key not in values:
                return False
            values.pop(key)
            self._write_locked(canonical, values)
            return True

    def status_for_user(self, user_id: str) -> dict[str, dict[str, bool | str]]:
        values = self._load(user_id)
        return {
            name: {
                "configured": bool(values.get(name)),
                "masked": _mask(values[name]) if values.get(name) else "",
            }
            for name in sorted(_ALLOWED_NAMES)
        }

    def rotate_all(self) -> int:
        """Re-encrypt any files read with a previous key using the current key."""
        users_root = self.data_root / "users"
        if not users_root.exists():
            return 0
        rotated = 0
        for user_dir in users_root.iterdir():
            try:
                user_id = self._validate_user_id(user_dir.name)
            except ValueError:
                continue
            if not user_dir.is_dir():
                continue
            with self._user_lock(user_id):
                values, needs_rotation = self._read_locked(user_id)
                if needs_rotation:
                    self._write_locked(user_id, values)
                    rotated += 1
        return rotated
