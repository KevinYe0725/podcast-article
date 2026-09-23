from __future__ import annotations

import base64
import hashlib
import re
import secrets
import sqlite3
import stat
from decimal import Decimal

import pytest

from podcast_article import admin
from podcast_article.platform_store import AccountQuota, PlatformStore


def _member(store, admin_account, username="alice"):
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    store.create_invite(admin_account.id, token_hash, AccountQuota(3_600, Decimal("5.00"), 1_000_000, 5, 2_000_000), expires_at=2_000_000_000)
    account = store.register_invite(token_hash, username, "$argon2id$initial", now=1_700_000_000)
    return account, raw, token_hash


def test_reset_password_help_has_no_password_value_argument(capsys):
    with pytest.raises(SystemExit) as result:
        admin.main(["user", "reset-password", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "--password" not in help_text
    assert "password_value" not in help_text


def test_bootstrap_reads_password_from_getpass_and_does_not_accept_cli_password(monkeypatch, tmp_path):
    monkeypatch.setenv("PA_DATA_ROOT", str(tmp_path))
    values = iter(("long passphrase for admin", "long passphrase for admin"))
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: next(values))

    assert admin.main(["bootstrap", "--username", "Kevin"]) == 0
    store = PlatformStore(tmp_path / "platform.sqlite")
    assert store.credential_for_username("kevin").account.role == "admin"
    with pytest.raises(SystemExit) as result:
        admin.main(["bootstrap", "--username", "Kevin", "secret-value"])
    assert result.value.code == 2


def test_secrets_key_is_written_once_with_private_mode_and_never_printed(capsys, tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("EXISTING_SETTING=1\n", encoding="utf-8")

    assert admin.main(["secrets-key", "--env-file", str(env_file)]) == 0
    first_output = capsys.readouterr().out
    content = env_file.read_text(encoding="utf-8")
    match = re.search(r"^PA_USER_SECRETS_KEY=(.+)$", content, flags=re.MULTILINE)
    assert match is not None
    encoded_key = match.group(1)
    assert len(base64.urlsafe_b64decode(encoded_key)) == 32
    assert encoded_key not in first_output
    assert "EXISTING_SETTING=1" in content
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

    assert admin.main(["secrets-key", "--env-file", str(env_file)]) == 1
    second_output = capsys.readouterr()
    assert encoded_key not in second_output.out + second_output.err
    assert env_file.read_text(encoding="utf-8").count("PA_USER_SECRETS_KEY=") == 1


def test_invite_create_prints_fragment_once_and_persists_only_hash(monkeypatch, capsys, tmp_path, platform_store, platform_admin):
    monkeypatch.setattr(admin, "_store", lambda: platform_store)
    monkeypatch.setattr(admin.secrets, "token_urlsafe", lambda _n: "one-time-random-invite-token")
    monkeypatch.setattr(admin.time, "time", lambda: 1_700_000_000)

    result = admin.main([
        "invite", "create", "--admin-username", platform_admin.username,
        "--asr-month-seconds", "3600", "--llm-month-cny", "7.50",
        "--cache-bytes", "1000000", "--queue-items", "5", "--max-upload-bytes", "2000000",
        "--base-url", "https://example.test",
    ])
    output = capsys.readouterr().out
    raw_token = "one-time-random-invite-token"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    assert result == 0
    assert output.count(raw_token) == 1
    assert "https://example.test/invite#one-time-random-invite-token" in output
    assert raw_token.encode() not in platform_store.path.read_bytes()
    with sqlite3.connect(platform_store.path) as db:
        invite = db.execute("SELECT token_hash, asr_month_seconds, llm_month_cny, expires_at FROM invites").fetchone()
    assert invite == (token_hash, 3600, "7.50", 1_700_604_800)


def test_invite_revoke_reads_hidden_token_without_echoing_it(monkeypatch, capsys, platform_store, platform_admin):
    monkeypatch.setattr(admin, "_store", lambda: platform_store)
    raw_token = "raw-invite-for-revocation"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    platform_store.create_invite(platform_admin.id, token_hash, AccountQuota(1, Decimal("1")), expires_at=2_000)
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: raw_token)

    assert admin.main(["invite", "revoke"]) == 0
    output = capsys.readouterr().out
    assert raw_token not in output
    with pytest.raises(ValueError):
        platform_store.register_invite(token_hash, "alice", "$argon2id$pw", now=1_000)


def test_reset_password_revokes_sessions_and_requires_change_on_next_login(monkeypatch, platform_store, platform_admin):
    account, _, _ = _member(platform_store, platform_admin)
    token_hash = hashlib.sha256(b"active-session").hexdigest()
    platform_store.create_session(account.id, token_hash, 1_700_000_000, 1_700_050_000, 1_800_000_000)
    monkeypatch.setattr(admin, "_store", lambda: platform_store)
    values = iter(("a new long password", "a new long password"))
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: next(values))

    assert admin.main(["user", "reset-password", "--username", "alice"]) == 0
    updated = platform_store.credential_for_username("alice")
    assert updated.account.must_change_password is True
    assert admin.auth.verify_password(updated.password_hash, "a new long password")
    assert platform_store.resolve_session(token_hash, now=1_700_000_001) is None


def test_disable_and_enable_commands_change_account_state(monkeypatch, platform_store, platform_admin):
    account, _, _ = _member(platform_store, platform_admin)
    monkeypatch.setattr(admin, "_store", lambda: platform_store)

    assert admin.main(["user", "disable", "--username", account.username]) == 0
    assert platform_store.user_by_id(account.id).enabled is False
    assert admin.main(["user", "enable", "--username", account.username]) == 0
    assert platform_store.user_by_id(account.id).enabled is True
