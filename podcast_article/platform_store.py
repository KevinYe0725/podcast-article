"""SQLite-backed identities, invitations, sessions and login throttling."""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator


_SCHEMA_VERSION = 1
_SESSION_IDLE_SECONDS = 12 * 60 * 60
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_MAX_IP_ATTEMPTS = 20
_TOKEN_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class AccountQuota:
    asr_month_seconds: int | None = None
    llm_month_cny: Decimal | None = None
    cache_bytes: int | None = None
    queue_items: int | None = None
    max_upload_bytes: int | None = None


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    role: str
    enabled: bool
    must_change_password: bool
    quota: AccountQuota


@dataclass(frozen=True)
class AuthCredential:
    account: Account
    password_hash: str


class InviteError(ValueError):
    """A safely distinguishable invitation lifecycle error."""

    def __init__(self, code: str):
        if code not in {"invalid", "expired", "revoked", "consumed"}:
            raise ValueError("unknown invitation error code")
        self.code = code
        super().__init__(f"invite {code}")


def normalize_username(username: str) -> tuple[str, str]:
    if not isinstance(username, str):
        raise ValueError("username must be text")
    display = username.strip()
    key = unicodedata.normalize("NFKC", display).casefold()
    if not key:
        raise ValueError("username cannot be empty")
    if len(display) > 64 or len(key) > 64:
        raise ValueError("username must be at most 64 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in display):
        raise ValueError("username contains a control character")
    return display, key


def _validate_token_hash(token_hash: str) -> None:
    if not isinstance(token_hash, str) or not _TOKEN_HASH_RE.fullmatch(token_hash):
        raise ValueError("token hash must be a lowercase SHA-256 hex digest")


def _validate_password_hash(password_hash: str) -> None:
    if not isinstance(password_hash, str) or not password_hash or len(password_hash.encode("utf-8")) > 1024:
        raise ValueError("password hash is invalid")


def _validate_quota(quota: AccountQuota) -> AccountQuota:
    if not isinstance(quota, AccountQuota):
        raise ValueError("quota must be an AccountQuota")
    for name in ("asr_month_seconds", "cache_bytes", "queue_items", "max_upload_bytes"):
        value = getattr(quota, name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")
    money = quota.llm_month_cny
    if money is not None:
        try:
            money = Decimal(str(money))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("llm_month_cny must be a finite non-negative decimal") from exc
        if not money.is_finite() or money < 0:
            raise ValueError("llm_month_cny must be a finite non-negative decimal")
    return AccountQuota(
        quota.asr_month_seconds,
        money,
        quota.cache_bytes,
        quota.queue_items,
        quota.max_upload_bytes,
    )


def _quota_values(quota: AccountQuota) -> tuple[object, ...]:
    quota = _validate_quota(quota)
    return (
        quota.asr_month_seconds,
        str(quota.llm_month_cny) if quota.llm_month_cny is not None else None,
        quota.cache_bytes,
        quota.queue_items,
        quota.max_upload_bytes,
    )


def _account_from_row(row: sqlite3.Row) -> Account:
    money = Decimal(row["llm_month_cny"]) if row["llm_month_cny"] is not None else None
    return Account(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        enabled=bool(row["enabled"]),
        must_change_password=bool(row["must_change_password"]),
        quota=AccountQuota(
            asr_month_seconds=row["asr_month_seconds"],
            llm_month_cny=money,
            cache_bytes=row["cache_bytes"],
            queue_items=row["queue_items"],
            max_upload_bytes=row["max_upload_bytes"],
        ),
    )


class PlatformStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    must_change_password INTEGER NOT NULL CHECK (must_change_password IN (0, 1)),
                    asr_month_seconds INTEGER,
                    llm_month_cny TEXT,
                    cache_bytes INTEGER,
                    queue_items INTEGER,
                    max_upload_bytes INTEGER,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invites (
                    token_hash TEXT PRIMARY KEY,
                    created_by TEXT NOT NULL REFERENCES accounts(id),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    revoked_at REAL,
                    asr_month_seconds INTEGER,
                    llm_month_cny TEXT,
                    cache_bytes INTEGER,
                    queue_items INTEGER,
                    max_upload_bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    idle_expires_at REAL NOT NULL,
                    absolute_expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id, revoked_at);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL,
                    ip_key TEXT NOT NULL,
                    attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_attempts_by_subject
                    ON login_attempts(account_key, ip_key, attempted_at);
                """
            )
            rows = db.execute("SELECT version FROM schema_version").fetchall()
            if not rows:
                db.execute("INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,))
            elif len(rows) != 1 or rows[0][0] != _SCHEMA_VERSION:
                raise RuntimeError("unsupported platform store schema version")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # The store still works on filesystems that do not expose POSIX modes.
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _insert_account(
        db: sqlite3.Connection,
        username: str,
        username_key: str,
        password_hash: str,
        role: str,
        quota: AccountQuota,
        now: float,
        must_change_password: bool = False,
    ) -> str:
        account_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO accounts(
                id, username, username_key, password_hash, role, enabled,
                must_change_password, asr_month_seconds, llm_month_cny,
                cache_bytes, queue_items, max_upload_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                account_id, username, username_key, password_hash, role,
                int(must_change_password), *_quota_values(quota), now,
            ),
        )
        return account_id

    @staticmethod
    def _account_by_id(db: sqlite3.Connection, user_id: str) -> Account | None:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (user_id,)).fetchone()
        return _account_from_row(row) if row else None

    def bootstrap_admin(self, username: str, password_hash: str, now: float) -> Account:
        display, key = normalize_username(username)
        _validate_password_hash(password_hash)
        quota = AccountQuota()  # Bootstrap is an explicit, administrator-controlled unlimited account.
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                raise ValueError("administrator bootstrap is only allowed in an empty store")
            user_id = self._insert_account(db, display, key, password_hash, "admin", quota, now)
            return self._account_by_id(db, user_id)  # type: ignore[return-value]

    def create_invite(self, created_by: str, token_hash: str, quota: AccountQuota, expires_at: float) -> None:
        _validate_token_hash(token_hash)
        values = _quota_values(quota)
        now = time.time()
        with self._transaction() as db:
            creator = db.execute("SELECT role, enabled FROM accounts WHERE id=?", (created_by,)).fetchone()
            if not creator or not creator["enabled"] or creator["role"] != "admin":
                raise ValueError("only an enabled administrator can create invites")
            db.execute(
                """INSERT INTO invites(
                    token_hash, created_by, created_at, expires_at,
                    asr_month_seconds, llm_month_cny, cache_bytes, queue_items, max_upload_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (token_hash, created_by, now, float(expires_at), *values),
            )

    def revoke_invite(self, token_hash: str, now: float | None = None) -> bool:
        _validate_token_hash(token_hash)
        revoked_at = time.time() if now is None else float(now)
        with self._transaction() as db:
            row = db.execute("SELECT used_at, revoked_at FROM invites WHERE token_hash=?", (token_hash,)).fetchone()
            if not row:
                raise InviteError("invalid")
            if row["used_at"] is not None:
                raise InviteError("consumed")
            if row["revoked_at"] is not None:
                return False
            db.execute("UPDATE invites SET revoked_at=? WHERE token_hash=?", (revoked_at, token_hash))
            return True

    def register_invite(self, token_hash: str, username: str, password_hash: str, now: float) -> Account:
        _validate_token_hash(token_hash)
        display, key = normalize_username(username)
        _validate_password_hash(password_hash)
        with self._transaction() as db:
            invite = db.execute("SELECT * FROM invites WHERE token_hash=?", (token_hash,)).fetchone()
            if not invite:
                raise InviteError("invalid")
            if invite["revoked_at"] is not None:
                raise InviteError("revoked")
            if invite["used_at"] is not None:
                raise InviteError("consumed")
            if float(now) >= invite["expires_at"]:
                raise InviteError("expired")
            if db.execute("SELECT 1 FROM accounts WHERE username_key=?", (key,)).fetchone():
                raise ValueError("username is already registered")
            quota = AccountQuota(
                invite["asr_month_seconds"],
                Decimal(invite["llm_month_cny"]) if invite["llm_month_cny"] is not None else None,
                invite["cache_bytes"],
                invite["queue_items"],
                invite["max_upload_bytes"],
            )
            user_id = self._insert_account(db, display, key, password_hash, "member", quota, float(now))
            updated = db.execute(
                "UPDATE invites SET used_at=? WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?",
                (float(now), token_hash, float(now)),
            )
            if updated.rowcount != 1:
                raise InviteError("expired")
            return self._account_by_id(db, user_id)  # type: ignore[return-value]

    def credential_for_username(self, username: str) -> AuthCredential | None:
        try:
            _, key = normalize_username(username)
        except ValueError:
            return None
        with self._connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE username_key=?", (key,)).fetchone()
        if not row:
            return None
        return AuthCredential(_account_from_row(row), row["password_hash"])

    def user_by_id(self, user_id: str) -> Account | None:
        with self._connection() as db:
            return self._account_by_id(db, user_id)

    def set_enabled(self, user_id: str, enabled: bool) -> Account:
        with self._transaction() as db:
            updated = db.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(bool(enabled)), user_id))
            if updated.rowcount != 1:
                raise KeyError("account not found")
            if not enabled:
                db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (time.time(), user_id))
            return self._account_by_id(db, user_id)  # type: ignore[return-value]

    def replace_password_hash(self, user_id: str, password_hash: str, must_change: bool) -> Account:
        _validate_password_hash(password_hash)
        with self._transaction() as db:
            updated = db.execute(
                "UPDATE accounts SET password_hash=?, must_change_password=? WHERE id=?",
                (password_hash, int(bool(must_change)), user_id),
            )
            if updated.rowcount != 1:
                raise KeyError("account not found")
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (time.time(), user_id))
            return self._account_by_id(db, user_id)  # type: ignore[return-value]

    @staticmethod
    def _privacy_key(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def record_login_attempt(self, account_key: str, ip_key: str, now: float) -> None:
        account_digest, ip_digest = self._privacy_key(account_key), self._privacy_key(ip_key)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO login_attempts(account_key, ip_key, attempted_at) VALUES (?, ?, ?)",
                (account_digest, ip_digest, float(now)),
            )
            db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (float(now) - 30 * 24 * 60 * 60,))

    def login_allowed(self, account_key: str, ip_key: str, now: float) -> bool:
        account_digest, ip_digest = self._privacy_key(account_key), self._privacy_key(ip_key)
        with self._connection() as db:
            account_count = db.execute(
                """SELECT COUNT(*) FROM login_attempts
                   WHERE account_key=? AND attempted_at>? AND attempted_at<=?""",
                (account_digest, float(now) - _LOGIN_WINDOW_SECONDS, float(now)),
            ).fetchone()[0]
            ip_count = db.execute(
                """SELECT COUNT(*) FROM login_attempts
                   WHERE ip_key=? AND attempted_at>? AND attempted_at<=?""",
                (ip_digest, float(now) - _LOGIN_WINDOW_SECONDS, float(now)),
            ).fetchone()[0]
        return account_count < _LOGIN_MAX_ATTEMPTS and ip_count < _LOGIN_MAX_IP_ATTEMPTS

    def create_session(
        self,
        user_id: str,
        token_hash: str,
        created_at: float,
        idle_expires_at: float,
        absolute_expires_at: float,
    ) -> None:
        _validate_token_hash(token_hash)
        if idle_expires_at <= created_at or absolute_expires_at <= created_at:
            raise ValueError("session expiry must be after creation")
        with self._transaction() as db:
            account = db.execute("SELECT enabled FROM accounts WHERE id=?", (user_id,)).fetchone()
            if not account or not account["enabled"]:
                raise ValueError("cannot create a session for a missing or disabled account")
            db.execute(
                """INSERT INTO sessions(token_hash, user_id, created_at, idle_expires_at, absolute_expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (token_hash, user_id, float(created_at), float(idle_expires_at), float(absolute_expires_at)),
            )

    def resolve_session(self, token_hash: str, now: float) -> Account | None:
        _validate_token_hash(token_hash)
        with self._transaction() as db:
            session = db.execute(
                "SELECT * FROM sessions WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)
            ).fetchone()
            if not session:
                return None
            if float(now) >= session["idle_expires_at"] or float(now) >= session["absolute_expires_at"]:
                db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (float(now), token_hash))
                return None
            account = self._account_by_id(db, session["user_id"])
            if not account or not account.enabled:
                db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (float(now), token_hash))
                return None
            next_idle_expiry = min(float(now) + _SESSION_IDLE_SECONDS, session["absolute_expires_at"])
            db.execute("UPDATE sessions SET idle_expires_at=? WHERE token_hash=?", (next_idle_expiry, token_hash))
            return account

    def revoke_session(self, token_hash: str) -> bool:
        _validate_token_hash(token_hash)
        with self._transaction() as db:
            updated = db.execute(
                "UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (time.time(), token_hash),
            )
            return updated.rowcount == 1

    def revoke_user_sessions(self, user_id: str) -> int:
        with self._transaction() as db:
            updated = db.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (time.time(), user_id),
            )
            return updated.rowcount
