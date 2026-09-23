from __future__ import annotations

import hashlib
import secrets
import sqlite3
import stat
from decimal import Decimal
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest

from podcast_article.platform_store import AccountQuota, InviteError


def _new_invite(store, admin, quota=None, expires_at=2_000):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    store.create_invite(admin.id, token_hash, quota=quota or AccountQuota(), expires_at=expires_at)
    return raw_token, token_hash


def test_bootstrap_admin_is_only_available_for_an_empty_store(platform_store):
    admin = platform_store.bootstrap_admin("Kevin", "$argon2id$admin", now=1_000)

    assert admin.role == "admin"
    assert admin.enabled is True
    assert platform_store.user_by_id(admin.id) == admin
    assert platform_store.credential_for_username("kevin").account == admin
    with pytest.raises(ValueError):
        platform_store.bootstrap_admin("second-admin", "$argon2id$other", now=1_001)


def test_usernames_are_unique_after_unicode_nfkc_casefold(platform_store, platform_admin, platform_password_hash):
    _, token_hash = _new_invite(platform_store, platform_admin)
    alice = platform_store.register_invite(token_hash, "Ａlice", platform_password_hash, now=1_100)

    assert alice.username == "Ａlice"
    assert platform_store.credential_for_username("alice").account.id == alice.id
    _, second_hash = _new_invite(platform_store, platform_admin)
    with pytest.raises(ValueError):
        platform_store.register_invite(second_hash, "alice", platform_password_hash, now=1_101)


def test_invite_is_single_use_and_only_hash_is_persisted(platform_store, platform_admin, platform_password_hash, tmp_path):
    raw_token, token_hash = _new_invite(platform_store, platform_admin)
    quota = AccountQuota(3_600, Decimal("1.00"), 1_048_576, 5, 2_097_152)
    # Update the invitation with a fresh token to prove every quota value round-trips.
    raw_token, token_hash = _new_invite(platform_store, platform_admin, quota=quota)
    account = platform_store.register_invite(token_hash, "alice", platform_password_hash, now=1_000)

    assert account.role == "member"
    assert account.quota == quota
    with pytest.raises(InviteError) as error:
        platform_store.register_invite(token_hash, "alice2", platform_password_hash, now=1_001)
    assert error.value.code == "consumed"
    assert raw_token.encode() not in (tmp_path / "platform.sqlite").read_bytes()


def test_invite_rejects_invalid_expired_and_revoked_tokens(platform_store, platform_admin, platform_password_hash):
    with pytest.raises(InviteError) as invalid:
        platform_store.register_invite("0" * 64, "alice", platform_password_hash, now=1_000)
    assert invalid.value.code == "invalid"

    _, expired_hash = _new_invite(platform_store, platform_admin, expires_at=1_000)
    with pytest.raises(InviteError) as expired:
        platform_store.register_invite(expired_hash, "expired", platform_password_hash, now=1_000)
    assert expired.value.code == "expired"

    _, revoked_hash = _new_invite(platform_store, platform_admin)
    assert platform_store.revoke_invite(revoked_hash, now=1_010) is True
    with pytest.raises(InviteError) as revoked:
        platform_store.register_invite(revoked_hash, "revoked", platform_password_hash, now=1_011)
    assert revoked.value.code == "revoked"


def test_invite_can_only_be_consumed_once_under_concurrent_registration(platform_store, platform_admin, platform_password_hash):
    _, token_hash = _new_invite(platform_store, platform_admin)
    barrier = Barrier(2)

    def register(username):
        barrier.wait()
        try:
            return platform_store.register_invite(token_hash, username, platform_password_hash, now=1_200).username
        except InviteError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, ("alice", "bob")))

    assert sum(result in {"alice", "bob"} for result in results) == 1
    assert results.count("consumed") == 1


def test_sessions_expire_and_can_be_revoked(platform_store, platform_admin):
    sliding_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    platform_store.create_session(platform_admin.id, sliding_hash, 1_000, 1_100, 1_200)
    assert platform_store.resolve_session(sliding_hash, now=1_099) == platform_admin
    assert platform_store.resolve_session(sliding_hash, now=1_199) == platform_admin
    assert platform_store.resolve_session(sliding_hash, now=1_200) is None

    idle_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    platform_store.create_session(platform_admin.id, idle_hash, 1_000, 1_100, 2_000)
    assert platform_store.resolve_session(idle_hash, now=1_100) is None

    another_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    platform_store.create_session(platform_admin.id, another_hash, 1_000, 2_000, 3_000)
    assert platform_store.revoke_session(another_hash) is True
    assert platform_store.resolve_session(another_hash, now=1_001) is None


def test_login_attempts_are_throttled_by_account_and_ip(platform_store):
    account_key, ip_key = "alice", "ip-hash-1"
    assert platform_store.login_allowed(account_key, ip_key, now=1_000)
    for second in range(1_000, 1_005):
        platform_store.record_login_attempt(account_key, ip_key, now=second)
    assert platform_store.login_allowed(account_key, ip_key, now=1_004) is False
    assert platform_store.login_allowed("bob", "different-ip", now=1_004) is True
    assert platform_store.login_allowed(account_key, ip_key, now=1_900) is True


def test_login_throttle_limits_an_account_across_ips_and_an_ip_across_accounts(platform_store):
    for second in range(1_000, 1_005):
        platform_store.record_login_attempt("alice", f"ip-{second}", now=second)
    assert platform_store.login_allowed("alice", "new-ip", now=1_005) is False

    for index in range(20):
        platform_store.record_login_attempt(f"user-{index}", "shared-ip", now=2_000 + index)
    assert platform_store.login_allowed("new-user", "shared-ip", now=2_020) is False
    assert platform_store.login_allowed("new-user", "shared-ip", now=2_900) is True


def test_disabling_or_resetting_account_revokes_its_sessions(platform_store, platform_admin):
    first_hash = hashlib.sha256(b"session-1").hexdigest()
    platform_store.create_session(platform_admin.id, first_hash, 1_000, 2_000, 3_000)
    assert platform_store.set_enabled(platform_admin.id, False).enabled is False
    assert platform_store.resolve_session(first_hash, now=1_100) is None

    platform_store.set_enabled(platform_admin.id, True)
    second_hash = hashlib.sha256(b"session-2").hexdigest()
    platform_store.create_session(platform_admin.id, second_hash, 1_200, 2_000, 3_000)
    updated = platform_store.replace_password_hash(platform_admin.id, "$argon2id$new", must_change=True)
    assert updated.must_change_password is True
    assert platform_store.credential_for_username("admin").password_hash == "$argon2id$new"
    assert platform_store.resolve_session(second_hash, now=1_300) is None


def test_database_has_schema_version(platform_store, tmp_path):
    with sqlite3.connect(tmp_path / "platform.sqlite") as db:
        version = db.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version >= 1
    assert stat.S_IMODE((tmp_path / "platform.sqlite").stat().st_mode) == 0o600
