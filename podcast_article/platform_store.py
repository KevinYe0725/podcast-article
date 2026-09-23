"""SQLite-backed identities, invitations, sessions and login throttling."""
from __future__ import annotations

import hashlib
import json
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


_SCHEMA_VERSION = 2
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


class QuotaExceeded(ValueError):
    def __init__(self, resource: str, limit, used, reserved, requested):
        self.resource = resource
        self.limit = limit
        self.used = used
        self.reserved = reserved
        self.requested = requested
        self.remaining = max(limit - used - reserved, 0) if limit is not None else None
        super().__init__(f"{resource} quota exceeded")


@dataclass(frozen=True)
class Reservation:
    id: str
    owner_id: str
    resource: str
    request_id: str
    month_key: str
    reserved_amount: int | Decimal
    actual_amount: int | Decimal | None
    status: str
    price_table_version: str | None = None


@dataclass(frozen=True)
class PlatformJob:
    id: str
    owner_id: str
    payload: dict
    created_at: float
    state: str
    started_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    dir_name: str | None = None
    position: int = 0


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
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            return
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
                CREATE TABLE IF NOT EXISTS session_csrf (
                    session_hash TEXT PRIMARY KEY REFERENCES sessions(token_hash) ON DELETE CASCADE,
                    csrf_hash TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS quota_usage (
                    owner_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    month_key TEXT NOT NULL,
                    asr_used_seconds INTEGER NOT NULL DEFAULT 0,
                    asr_reserved_seconds INTEGER NOT NULL DEFAULT 0,
                    llm_used_cny TEXT NOT NULL DEFAULT '0',
                    llm_reserved_cny TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY(owner_id, month_key)
                );
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    resource TEXT NOT NULL CHECK (resource IN ('asr', 'llm')),
                    request_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    reserved_amount TEXT NOT NULL,
                    actual_amount TEXT,
                    price_table_version TEXT,
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'released')),
                    created_at REAL NOT NULL,
                    settled_at REAL,
                    UNIQUE(owner_id, resource, request_id)
                );
                CREATE INDEX IF NOT EXISTS quota_reservations_by_owner
                    ON quota_reservations(owner_id, month_key, resource, status);
                CREATE TABLE IF NOT EXISTS jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0,
                    position INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'done', 'error', 'skipped', 'cancelled')),
                    started_at REAL,
                    finished_at REAL,
                    error TEXT NOT NULL DEFAULT '',
                    dir_name TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_by_owner_state
                    ON jobs(owner_id, state, created_at, sequence);
                CREATE TABLE IF NOT EXISTS queue_scheduler (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    last_owner_id TEXT
                );
                INSERT OR IGNORE INTO queue_scheduler(singleton, last_owner_id) VALUES(1, NULL);
                """
            )
            rows = db.execute("SELECT version FROM schema_version").fetchall()
            if not rows:
                db.execute("INSERT INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,))
            elif len(rows) == 1 and rows[0][0] == 1:
                # Version 2 only adds quota/queue tables; existing identities and sessions stay intact.
                db.execute("UPDATE schema_version SET version=?", (_SCHEMA_VERSION,))
            elif len(rows) != 1 or rows[0][0] != _SCHEMA_VERSION:
                raise RuntimeError("unsupported platform store schema version")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # The store still works on filesystems that do not expose POSIX modes.
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        else:
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
        if self.read_only:
            raise RuntimeError("read-only platform store does not support transactions")
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

    def validate_invite(self, token_hash: str, now: float) -> None:
        """Cheap read-only preflight; registration still rechecks and consumes atomically."""
        _validate_token_hash(token_hash)
        with self._connection() as db:
            invite = db.execute("SELECT * FROM invites WHERE token_hash=?", (token_hash,)).fetchone()
        if not invite:
            raise InviteError("invalid")
        if invite["revoked_at"] is not None:
            raise InviteError("revoked")
        if invite["used_at"] is not None:
            raise InviteError("consumed")
        if float(now) >= invite["expires_at"]:
            raise InviteError("expired")

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

    def enabled_account_ids(self) -> list[str]:
        with self._connection() as db:
            rows = db.execute("SELECT id FROM accounts WHERE enabled=1 ORDER BY id").fetchall()
        return [row["id"] for row in rows]

    def legacy_owner_id(self) -> str | None:
        """Return the bootstrap owner whose existing OSS prefix must remain unchanged."""
        with self._connection() as db:
            row = db.execute(
                "SELECT id FROM accounts WHERE role='admin' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

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
        csrf_token_hash: str | None = None,
    ) -> None:
        _validate_token_hash(token_hash)
        if csrf_token_hash is not None:
            _validate_token_hash(csrf_token_hash)
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
            if csrf_token_hash is not None:
                db.execute(
                    "INSERT INTO session_csrf(session_hash, csrf_hash) VALUES (?, ?)",
                    (token_hash, csrf_token_hash),
                )

    def session_csrf_matches(self, token_hash: str, csrf_token_hash: str) -> bool:
        _validate_token_hash(token_hash)
        _validate_token_hash(csrf_token_hash)
        with self._connection() as db:
            row = db.execute(
                """SELECT 1 FROM sessions AS s
                   JOIN session_csrf AS c ON c.session_hash=s.token_hash
                   JOIN accounts AS a ON a.id=s.user_id
                   WHERE s.token_hash=? AND c.csrf_hash=? AND s.revoked_at IS NULL AND a.enabled=1""",
                (token_hash, csrf_token_hash),
            ).fetchone()
            return row is not None

    def set_session_csrf(self, token_hash: str, csrf_token_hash: str) -> bool:
        _validate_token_hash(token_hash)
        _validate_token_hash(csrf_token_hash)
        with self._transaction() as db:
            active = db.execute(
                "SELECT 1 FROM sessions WHERE token_hash=? AND revoked_at IS NULL", (token_hash,)
            ).fetchone()
            if not active:
                return False
            db.execute(
                """INSERT INTO session_csrf(session_hash, csrf_hash) VALUES (?, ?)
                   ON CONFLICT(session_hash) DO UPDATE SET csrf_hash=excluded.csrf_hash""",
                (token_hash, csrf_token_hash),
            )
            return True

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

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> PlatformJob:
        return PlatformJob(
            id=row["id"], owner_id=row["owner_id"], payload=json.loads(row["payload_json"]),
            created_at=float(row["created_at"]), state=row["state"],
            started_at=row["started_at"], finished_at=row["finished_at"],
            error=row["error"] or "", dir_name=row["dir_name"], position=int(row["position"]),
        )

    def enqueue_job(self, owner_id: str, payload: dict, created_at: float) -> PlatformJob:
        return self.enqueue_jobs(owner_id, [payload], created_at)[0]

    def enqueue_jobs(self, owner_id: str, payloads: list[dict], created_at: float) -> list[PlatformJob]:
        if not payloads:
            return []
        try:
            encoded = [(payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                       for payload in payloads]
        except (TypeError, ValueError) as exc:
            raise ValueError("job payload must be JSON serializable") from exc
        with self._transaction() as db:
            account = db.execute("SELECT enabled, queue_items FROM accounts WHERE id=?", (owner_id,)).fetchone()
            if not account or not account["enabled"]:
                raise ValueError("account is missing or disabled")
            active_rows = db.execute(
                "SELECT payload_json FROM jobs WHERE owner_id=? AND state IN ('pending','running')",
                (owner_id,),
            ).fetchall()
            active_keys = set()
            for row in active_rows:
                try:
                    existing = json.loads(row["payload_json"])
                except (TypeError, ValueError):
                    continue
                if isinstance(existing, dict):
                    active_keys.add((existing.get("url"), existing.get("pick", 1)))
            candidates = []
            for payload, payload_json in encoded:
                key = (payload.get("url"), payload.get("pick", 1)) if isinstance(payload, dict) else None
                if key is not None and key in active_keys:
                    continue
                if key is not None:
                    active_keys.add(key)
                candidates.append(payload_json)
            queued = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner_id=? AND state IN ('pending','running')", (owner_id,)
            ).fetchone()[0]
            position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM jobs WHERE owner_id=?",
                                  (owner_id,)).fetchone()[0]
            limit = account["queue_items"]
            if limit is not None and queued + len(candidates) > int(limit):
                raise QuotaExceeded("queue_items", int(limit), int(queued), 0, len(candidates))
            jobs = []
            for offset, payload_json in enumerate(candidates):
                job_id = uuid.uuid4().hex
                db.execute(
                    """INSERT INTO jobs(id,owner_id,payload_json,created_at,updated_at,position,state)
                       VALUES(?,?,?,?,?,?, 'pending')""",
                    (job_id, owner_id, payload_json, float(created_at), float(created_at), int(position + offset)),
                )
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                jobs.append(self._job_from_row(row))
            return jobs

    def get_job(self, owner_id: str, job_id: str) -> PlatformJob | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone()
        return self._job_from_row(row) if row else None

    def list_jobs(self, owner_id: str, *, limit: int = 100) -> list[PlatformJob]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM jobs WHERE owner_id=? ORDER BY position, sequence LIMIT ?",
                (owner_id, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def running_jobs(self, owner_id: str | None = None) -> list[PlatformJob]:
        with self._connection() as db:
            if owner_id is None:
                rows = db.execute("SELECT * FROM jobs WHERE state='running' ORDER BY started_at, sequence").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM jobs WHERE owner_id=? AND state='running' ORDER BY started_at, sequence",
                    (owner_id,),
                ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def claim_job(self, owner_id: str, job_id: str) -> PlatformJob | None:
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE owner_id=? AND id=? AND state='pending'",
                              (owner_id, job_id)).fetchone()
            if not row:
                return None
            now = time.time()
            db.execute("UPDATE jobs SET state='running', started_at=?, updated_at=? WHERE id=?",
                       (now, now, job_id))
            db.execute("UPDATE queue_scheduler SET last_owner_id=? WHERE singleton=1", (owner_id,))
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(claimed)

    def next_job(self) -> PlatformJob | None:
        """Claim one pending job, rotating owners and keeping FIFO within each owner."""
        with self._transaction() as db:
            # Enforce the service-wide single-pipeline limit across processes too;
            # the Flask thread lock alone only coordinates one process.
            if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                return None
            owners = [row[0] for row in db.execute(
                "SELECT DISTINCT owner_id FROM jobs WHERE state='pending' ORDER BY owner_id").fetchall()]
            if not owners:
                return None
            cursor = db.execute("SELECT last_owner_id FROM queue_scheduler WHERE singleton=1").fetchone()
            last_owner = cursor[0] if cursor else None
            owner_id = next((owner for owner in owners if last_owner is not None and owner > last_owner), owners[0])
            row = db.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND state='pending' ORDER BY position, sequence LIMIT 1",
                (owner_id,),
            ).fetchone()
            if not row:
                return None
            now = time.time()
            db.execute("UPDATE jobs SET state='running', started_at=?, updated_at=? WHERE id=?",
                       (now, now, row["id"]))
            db.execute("UPDATE queue_scheduler SET last_owner_id=? WHERE singleton=1", (owner_id,))
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return self._job_from_row(claimed)

    def finish_job(self, owner_id: str, job_id: str, state: str, *, error: str = "",
                   dir_name: str | None = None) -> bool:
        if state not in {"done", "error", "skipped", "cancelled"}:
            raise ValueError("final job state must be done, error, skipped or cancelled")
        with self._transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone()
            if not row:
                return False
            if row["state"] == state:
                return True
            if row["state"] not in {"pending", "running"}:
                return False
            db.execute(
                "UPDATE jobs SET state=?, error=?, dir_name=?, finished_at=?, updated_at=? WHERE owner_id=? AND id=?",
                (state, error[:2_000], dir_name, time.time(), time.time(), owner_id, job_id),
            )
            return True

    def update_job(self, owner_id: str, job_id: str, fields: dict) -> PlatformJob | None:
        allowed = {"title", "pick", "opts", "source"}
        with self._transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload_json"])
            payload.update({key: value for key, value in fields.items() if key in allowed})
            db.execute("UPDATE jobs SET payload_json=?, updated_at=? WHERE owner_id=? AND id=?",
                       (json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        time.time(), owner_id, job_id))
            updated = db.execute("SELECT * FROM jobs WHERE owner_id=? AND id=?", (owner_id, job_id)).fetchone()
            return self._job_from_row(updated)

    def remove_job(self, owner_id: str, job_id: str) -> bool:
        with self._transaction() as db:
            deleted = db.execute(
                "DELETE FROM jobs WHERE owner_id=? AND id=? AND state!='running'", (owner_id, job_id)
            )
            return deleted.rowcount == 1

    def clear_jobs(self, owner_id: str, *, keep_failed: bool = False) -> int:
        states = ("done", "skipped", "cancelled") if keep_failed else ("done", "error", "skipped", "cancelled")
        marks = ",".join("?" for _ in states)
        with self._transaction() as db:
            deleted = db.execute(
                f"DELETE FROM jobs WHERE owner_id=? AND state IN ({marks})", (owner_id, *states)
            )
            return deleted.rowcount

    def move_job(self, owner_id: str, job_id: str, delta: int) -> int:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id, state FROM jobs WHERE owner_id=? ORDER BY position, sequence", (owner_id,)
            ).fetchall()
            index = next((i for i, row in enumerate(rows) if row["id"] == job_id), None)
            if index is None:
                raise ValueError("queue item not found")
            if rows[index]["state"] != "pending":
                return index
            pending = [row["id"] for row in rows if row["state"] == "pending"]
            pending_index = pending.index(job_id)
            target = max(0, min(len(pending) - 1, pending_index + int(delta)))
            if target != pending_index:
                pending.insert(target, pending.pop(pending_index))
                for position, item_id in enumerate(pending):
                    db.execute("UPDATE jobs SET position=? WHERE owner_id=? AND id=?",
                               (position, owner_id, item_id))
            return target

    def reorder_jobs(self, owner_id: str, job_ids: list[str]) -> None:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE owner_id=? ORDER BY position, sequence", (owner_id,)
            ).fetchall()
            existing = [row["id"] for row in rows]
            ordered = [item_id for item_id in job_ids if item_id in existing]
            ordered.extend(item_id for item_id in existing if item_id not in set(ordered))
            for position, item_id in enumerate(ordered):
                db.execute("UPDATE jobs SET position=? WHERE owner_id=? AND id=?",
                           (position, owner_id, item_id))

    def retry_failed_jobs(self, owner_id: str) -> int:
        with self._transaction() as db:
            rows = db.execute("SELECT id FROM jobs WHERE owner_id=? AND state='error' ORDER BY position, sequence",
                              (owner_id,)).fetchall()
            position = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM jobs WHERE owner_id=?",
                                  (owner_id,)).fetchone()[0]
            for row in rows:
                db.execute("UPDATE jobs SET state='pending',error='',finished_at=NULL,position=?,updated_at=? "
                           "WHERE owner_id=? AND id=?", (position, time.time(), owner_id, row["id"]))
                position += 1
            return len(rows)

    def recover_running_jobs(self, owner_id: str | None = None) -> int:
        with self._transaction() as db:
            if owner_id is None:
                changed = db.execute(
                    "UPDATE jobs SET state='pending',started_at=NULL,updated_at=? WHERE state='running'",
                    (time.time(),),
                )
            else:
                changed = db.execute(
                    "UPDATE jobs SET state='pending',started_at=NULL,updated_at=? WHERE state='running' AND owner_id=?",
                    (time.time(), owner_id),
                )
            return changed.rowcount

    @staticmethod
    def _validate_month_key(month_key: str) -> None:
        if not isinstance(month_key, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month_key):
            raise ValueError("month_key must be YYYY-MM")

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> Reservation:
        amount = int(row["reserved_amount"]) if row["resource"] == "asr" else Decimal(row["reserved_amount"])
        actual = row["actual_amount"]
        if actual is not None:
            actual = int(actual) if row["resource"] == "asr" else Decimal(actual)
        return Reservation(row["id"], row["owner_id"], row["resource"], row["request_id"],
                           row["month_key"], amount, actual, row["status"], row["price_table_version"])

    def _reserve_quota(self, owner_id: str, request_id: str, month_key: str,
                       resource: str, amount: int | Decimal,
                       price_table_version: str | None = None) -> Reservation:
        self._validate_month_key(month_key)
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError("request_id must be a non-empty string of at most 200 characters")
        if resource == "asr":
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise ValueError("ASR reservation must be a non-negative integer")
            limit_column, used_column, reserved_column = (
                "asr_month_seconds", "asr_used_seconds", "asr_reserved_seconds")
            amount_text = str(amount)
        elif resource == "llm":
            amount = Decimal(str(amount))
            if not amount.is_finite() or amount < 0:
                raise ValueError("LLM reservation must be a finite non-negative decimal")
            limit_column, used_column, reserved_column = "llm_month_cny", "llm_used_cny", "llm_reserved_cny"
            amount_text = str(amount)
        else:
            raise ValueError("unknown quota resource")

        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM quota_reservations WHERE owner_id=? AND resource=? AND request_id=?",
                (owner_id, resource, request_id),
            ).fetchone()
            if existing:
                current = self._reservation_from_row(existing)
                if current.month_key != month_key or current.reserved_amount != amount:
                    raise ValueError("reservation request_id was already used with different terms")
                return current

            account = db.execute(
                f"SELECT enabled, {limit_column} AS quota_limit FROM accounts WHERE id=?", (owner_id,)
            ).fetchone()
            if not account or not account["enabled"]:
                raise ValueError("account is missing or disabled")
            db.execute("INSERT OR IGNORE INTO quota_usage(owner_id, month_key) VALUES (?, ?)",
                       (owner_id, month_key))
            totals = db.execute(
                f"SELECT {used_column} AS used, {reserved_column} AS reserved FROM quota_usage "
                "WHERE owner_id=? AND month_key=?", (owner_id, month_key),
            ).fetchone()
            if resource == "asr":
                used, reserved = int(totals["used"]), int(totals["reserved"])
                limit = account["quota_limit"]
                requested = int(amount)
                exceeds = limit is not None and used + reserved + requested > int(limit)
            else:
                used, reserved = Decimal(totals["used"]), Decimal(totals["reserved"])
                limit = Decimal(account["quota_limit"]) if account["quota_limit"] is not None else None
                requested = Decimal(amount)
                exceeds = limit is not None and used + reserved + requested > limit
            if exceeds:
                raise QuotaExceeded(resource, limit, used, reserved, requested)

            reservation_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO quota_reservations(
                    id, owner_id, resource, request_id, month_key, reserved_amount,
                    price_table_version, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?)""",
                (reservation_id, owner_id, resource, request_id, month_key, amount_text,
                 price_table_version, time.time()),
            )
            new_reserved = reserved + requested
            db.execute(
                f"UPDATE quota_usage SET {reserved_column}=? WHERE owner_id=? AND month_key=?",
                (str(new_reserved) if resource == "llm" else new_reserved, owner_id, month_key),
            )
            row = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            return self._reservation_from_row(row)

    def reserve_asr(self, owner_id: str, job_id: str, month_key: str, audio_seconds: int) -> Reservation:
        return self._reserve_quota(owner_id, job_id, month_key, "asr", audio_seconds)

    def resize_asr_reservation(self, reservation_id: str, audio_seconds: int) -> Reservation:
        if not isinstance(audio_seconds, int) or isinstance(audio_seconds, bool) or audio_seconds < 0:
            raise ValueError("ASR reservation must be a non-negative integer")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise KeyError("reservation not found")
            current = self._reservation_from_row(row)
            if current.resource != "asr" or current.status != "reserved":
                raise ValueError("only an active ASR reservation can be resized")
            delta = audio_seconds - int(current.reserved_amount)
            totals = db.execute(
                "SELECT asr_used_seconds, asr_reserved_seconds FROM quota_usage "
                "WHERE owner_id=? AND month_key=?", (current.owner_id, current.month_key),
            ).fetchone()
            next_reserved = int(totals["asr_reserved_seconds"]) + delta
            if delta > 0:
                account = db.execute(
                    "SELECT enabled, asr_month_seconds FROM accounts WHERE id=?", (current.owner_id,)
                ).fetchone()
                if not account or not account["enabled"]:
                    raise ValueError("account is missing or disabled")
                limit = account["asr_month_seconds"]
                used = int(totals["asr_used_seconds"])
                if limit is not None and used + next_reserved > int(limit):
                    raise QuotaExceeded("asr", int(limit), used,
                                        int(totals["asr_reserved_seconds"]), delta)
            db.execute(
                "UPDATE quota_usage SET asr_reserved_seconds=? WHERE owner_id=? AND month_key=?",
                (next_reserved, current.owner_id, current.month_key),
            )
            db.execute("UPDATE quota_reservations SET reserved_amount=? WHERE id=?",
                       (str(audio_seconds), reservation_id))
            resized = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            return self._reservation_from_row(resized)

    def reserve_llm_call(self, owner_id: str, call_id: str, month_key: str,
                         quote_cny: Decimal, *, price_table_version: str | None = None) -> Reservation:
        return self._reserve_quota(owner_id, call_id, month_key, "llm", quote_cny,
                                   price_table_version=price_table_version)

    def settle_reservation(self, reservation_id: str, actual_amount: int | Decimal) -> Reservation:
        """Record provider-reported usage, even if it overruns the hold.

        External usage is irreversible. In the exceptional case of a provider or
        estimate overrun, record the actual amount (which can put usage above the
        account limit); future reservations then fail against that true total.
        """
        with self._transaction() as db:
            row = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise KeyError("reservation not found")
            current = self._reservation_from_row(row)
            if current.status != "reserved":
                return current
            if current.resource == "asr":
                if not isinstance(actual_amount, int) or isinstance(actual_amount, bool) or actual_amount < 0:
                    raise ValueError("ASR settlement must be a non-negative integer")
                actual_text = str(actual_amount)
                used_column, reserved_column = "asr_used_seconds", "asr_reserved_seconds"
            else:
                actual = Decimal(str(actual_amount))
                if not actual.is_finite() or actual < 0:
                    raise ValueError("LLM settlement must be a finite non-negative decimal")
                actual_text = str(actual)
                used_column, reserved_column = "llm_used_cny", "llm_reserved_cny"
            totals = db.execute(
                f"SELECT {used_column} AS used, {reserved_column} AS reserved FROM quota_usage "
                "WHERE owner_id=? AND month_key=?", (current.owner_id, current.month_key),
            ).fetchone()
            if current.resource == "asr":
                next_used = int(totals["used"]) + int(actual_amount)
                next_reserved = max(0, int(totals["reserved"]) - int(current.reserved_amount))
            else:
                next_used = Decimal(totals["used"]) + Decimal(actual_text)
                next_reserved = max(Decimal(0), Decimal(totals["reserved"]) - Decimal(current.reserved_amount))
            db.execute(
                f"UPDATE quota_usage SET {used_column}=?, {reserved_column}=? WHERE owner_id=? AND month_key=?",
                (str(next_used) if current.resource == "llm" else next_used,
                 str(next_reserved) if current.resource == "llm" else next_reserved,
                 current.owner_id, current.month_key),
            )
            db.execute(
                "UPDATE quota_reservations SET actual_amount=?, status='settled', settled_at=? WHERE id=?",
                (actual_text, time.time(), reservation_id),
            )
            settled = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            return self._reservation_from_row(settled)

    def release_reservation(self, reservation_id: str) -> Reservation:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            if not row:
                raise KeyError("reservation not found")
            current = self._reservation_from_row(row)
            if current.status != "reserved":
                return current
            reserved_column = "asr_reserved_seconds" if current.resource == "asr" else "llm_reserved_cny"
            totals = db.execute(
                f"SELECT {reserved_column} AS reserved FROM quota_usage WHERE owner_id=? AND month_key=?",
                (current.owner_id, current.month_key),
            ).fetchone()
            if current.resource == "asr":
                next_reserved = max(0, int(totals["reserved"]) - int(current.reserved_amount))
            else:
                next_reserved = max(Decimal(0), Decimal(totals["reserved"]) - Decimal(current.reserved_amount))
            db.execute(
                f"UPDATE quota_usage SET {reserved_column}=? WHERE owner_id=? AND month_key=?",
                (str(next_reserved) if current.resource == "llm" else next_reserved,
                 current.owner_id, current.month_key),
            )
            db.execute("UPDATE quota_reservations SET status='released', settled_at=? WHERE id=?",
                       (time.time(), reservation_id))
            released = db.execute("SELECT * FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            return self._reservation_from_row(released)

    def quota_usage(self, owner_id: str, month_key: str) -> dict:
        self._validate_month_key(month_key)
        with self._connection() as db:
            row = db.execute("SELECT * FROM quota_usage WHERE owner_id=? AND month_key=?",
                              (owner_id, month_key)).fetchone()
        if not row:
            return {"asr_used_seconds": 0, "asr_reserved_seconds": 0,
                    "llm_used_cny": Decimal(0), "llm_reserved_cny": Decimal(0)}
        return {
            "asr_used_seconds": int(row["asr_used_seconds"]),
            "asr_reserved_seconds": int(row["asr_reserved_seconds"]),
            "llm_used_cny": Decimal(row["llm_used_cny"]),
            "llm_reserved_cny": Decimal(row["llm_reserved_cny"]),
        }

    def reconcile_stale_reservations(self, *, now: float | None = None,
                                     max_age_seconds: int = 24 * 60 * 60) -> int:
        """Conservatively charge stale holds after a process crash.

        A provider request may have been accepted just before the process died, so
        stale reservations are settled at their upper bound instead of released.
        """
        if not isinstance(max_age_seconds, int) or isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be a positive integer")
        instant = time.time() if now is None else float(now)
        cutoff = instant - max_age_seconds
        with self._connection() as db:
            rows = db.execute(
                "SELECT id, resource, reserved_amount FROM quota_reservations "
                "WHERE status='reserved' AND created_at<=? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        reconciled = 0
        for row in rows:
            amount = int(row["reserved_amount"]) if row["resource"] == "asr" else Decimal(row["reserved_amount"])
            settled = self.settle_reservation(row["id"], amount)
            if settled.status == "settled":
                reconciled += 1
        return reconciled
