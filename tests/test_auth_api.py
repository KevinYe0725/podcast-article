from __future__ import annotations

import hashlib
import secrets
import time
from decimal import Decimal

from podcast_article.auth import hash_password
from podcast_article.platform_store import AccountQuota, InviteError


def _issue_invite(store, admin, expires_at=None):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    store.create_invite(
        admin.id,
        token_hash,
        AccountQuota(3_600, Decimal("5.00"), 1_000_000, 5, 2_000_000),
        expires_at=expires_at if expires_at is not None else time.time() + 600,
    )
    return raw_token, token_hash


def test_guest_api_is_denied(guest_client):
    response = guest_client.get("/api/library")
    assert response.status_code == 401
    assert response.get_json()["error"] == "authentication_required"


def test_guest_page_redirects_to_login_and_account_headers_cannot_authenticate(guest_client, auth_system):
    _, _, admin, _ = auth_system
    page = guest_client.get("/")
    assert page.status_code == 302
    assert page.headers["Location"].startswith("/login?")
    forged = guest_client.get("/api/library", headers={"X-Account-ID": admin.id})
    assert forged.status_code == 401


def test_favicon_request_does_not_create_an_auth_redirect_or_404(guest_client):
    assert guest_client.get("/favicon.ico").status_code == 204


def test_valid_login_sets_secure_http_only_lax_cookie(guest_client, auth_system):
    _, _, admin, password = auth_system
    response = guest_client.post("/api/auth/login", json={
        "username": admin.username,
        "password": password,
        "next": "/api/library",
    })

    assert response.status_code == 200
    assert response.get_json()["next"] == "/api/library"
    cookies = response.headers.getlist("Set-Cookie")
    session_cookie = next(cookie for cookie in cookies if cookie.startswith("pa_session="))
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie


def test_login_cookie_domain_can_be_scoped_to_the_public_host(guest_client, auth_system):
    webapp, _, admin, password = auth_system
    guest_client.get("/api/auth/csrf")
    webapp.app.config["PA_COOKIE_DOMAIN"] = "podcast.squareconf.cn"
    response = guest_client.post("/api/auth/login", json={"username": admin.username, "password": password})
    session_cookie = next(cookie for cookie in response.headers.getlist("Set-Cookie") if cookie.startswith("pa_session="))
    assert "Domain=podcast.squareconf.cn" in session_cookie


def test_login_errors_are_generic_and_rate_limited(guest_client, auth_system):
    _, _, admin, password = auth_system
    missing = guest_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    wrong = guest_client.post("/api/auth/login", json={"username": admin.username, "password": "wrong"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.get_json() == wrong.get_json() == {"error": "invalid_credentials", "message": "用户名或密码错误"}

    for _ in range(4):
        response = guest_client.post("/api/auth/login", json={"username": admin.username, "password": "wrong"})
        assert response.status_code == 401
    limited = guest_client.post("/api/auth/login", json={"username": admin.username, "password": password})
    assert limited.status_code == 429
    assert limited.get_json()["error"] == "login_rate_limited"


def test_login_throttle_normalizes_unicode_username_aliases(guest_client, auth_system):
    _, _, admin, password = auth_system
    alias = "ｔｅｓｔ－ａｄｍｉｎ"
    for username in (admin.username, alias, admin.username, alias, admin.username):
        response = guest_client.post("/api/auth/login", json={"username": username, "password": "wrong"})
        assert response.status_code == 401
    limited = guest_client.post("/api/auth/login", json={"username": admin.username, "password": password})
    assert limited.status_code == 429


def test_login_throttle_uses_caddy_client_ip_only_when_proxy_is_trusted(guest_client, auth_system):
    webapp, _, _, _ = auth_system
    webapp.app.config["PA_TRUSTED_PROXY"] = True
    for index in range(20):
        response = guest_client.post(
            "/api/auth/login",
            json={"username": f"unknown-{index}", "password": "wrong"},
            headers={"X-Real-IP": f"198.51.100.{index + 1}"},
        )
        assert response.status_code == 401
    final = guest_client.post(
        "/api/auth/login",
        json={"username": "unknown-final", "password": "wrong"},
        headers={"X-Real-IP": "198.51.100.250"},
    )
    assert final.status_code == 401


def test_invite_registration_consumes_token_once_and_rejects_expiry(guest_client, auth_system):
    _, store, admin, _ = auth_system
    expired, _ = _issue_invite(store, admin, expires_at=time.time() - 1)
    response = guest_client.post("/api/auth/register", json={
        "invite_token": expired,
        "username": "expired-user",
        "password": "a valid long passphrase",
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_invite"

    valid, _ = _issue_invite(store, admin)
    registered = guest_client.post("/api/auth/register", json={
        "invite_token": valid,
        "username": "new-friend",
        "password": "a valid long passphrase",
    })
    assert registered.status_code == 200
    assert guest_client.get("/api/auth/me").get_json()["username"] == "new-friend"
    reused = guest_client.post("/api/auth/register", json={
        "invite_token": valid,
        "username": "another-friend",
        "password": "a valid long passphrase",
    })
    assert reused.status_code == 400
    assert reused.get_json()["error"] == "invalid_invite"


def test_invalid_invite_is_rejected_before_argon2_hashing(guest_client, auth_system, monkeypatch):
    webapp, _, _, _ = auth_system

    def unexpected_hash(_password):
        raise AssertionError("invalid invite must be checked before expensive password hashing")

    monkeypatch.setattr(webapp.auth_mod, "hash_password", unexpected_hash)
    response = guest_client.post("/api/auth/register", json={
        "invite_token": "not-a-valid-token",
        "username": "attacker",
        "password": "a valid long passphrase",
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_invite"


def test_registration_rate_limits_invalid_invites_by_source_ip(guest_client):
    for index in range(20):
        response = guest_client.post("/api/auth/register", json={
            "invite_token": f"invalid-token-{index}",
            "username": f"user-{index}",
            "password": "a valid long passphrase",
        })
        assert response.status_code == 400
    limited = guest_client.post("/api/auth/register", json={
        "invite_token": "another-invalid-token",
        "username": "last-user",
        "password": "a valid long passphrase",
    })
    assert limited.status_code == 429
    assert limited.get_json()["error"] == "registration_rate_limited"


def test_logout_revokes_server_side_session(client):
    session = client.get_cookie("pa_session")
    assert session is not None
    assert client.get("/api/library").status_code == 200

    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert client.get("/api/library").status_code == 401


def test_password_change_requires_current_password_and_rotates_session(client, auth_system):
    _, store, admin, current_password = auth_system
    old_cookie = client.get_cookie("pa_session").value
    old_hash = hashlib.sha256(old_cookie.encode()).hexdigest()

    wrong = client.post("/api/auth/password", json={
        "current_password": "not the current password",
        "new_password": "a replacement passphrase",
    })
    assert wrong.status_code == 401

    changed = client.post("/api/auth/password", json={
        "current_password": current_password,
        "new_password": "a replacement passphrase",
    })
    assert changed.status_code == 200
    assert store.resolve_session(old_hash, now=time.time()) is None
    assert client.get("/api/auth/me").get_json()["must_change_password"] is False
    assert client.get_cookie("pa_session").value != old_cookie
    assert store.credential_for_username(admin.username).password_hash != current_password


def test_unsafe_requests_require_matching_csrf_cookie_and_header(guest_client):
    guest_client.auto_csrf = False
    missing = guest_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong"})
    assert missing.status_code == 403

    token_response = guest_client.get("/api/auth/csrf")
    csrf = token_response.get_json()["csrf_token"]
    assert guest_client.get_cookie("pa_csrf").value == csrf
    mismatch = guest_client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "wrong"},
        headers={"X-CSRF-Token": "not-the-cookie-token"},
    )
    assert mismatch.status_code == 403


def test_external_next_redirect_is_replaced_with_root(guest_client, auth_system):
    _, _, admin, password = auth_system
    response = guest_client.post("/api/auth/login", json={
        "username": admin.username,
        "password": password,
        "next": "https://evil.example/path",
    })
    assert response.status_code == 200
    assert response.get_json()["next"] == "/"


def test_login_and_invite_pages_are_available_and_invite_clears_fragment_in_browser(guest_client):
    login_page = guest_client.get("/login")
    assert login_page.status_code == 200
    response = guest_client.get("/invite")
    assert response.status_code == 200
    assert b'id="invite-form"' in response.data
    assert b'id="login-form"' in login_page.data
    assert b'id="username"' in login_page.data
    assert b'id="password"' in login_page.data
    login_script = guest_client.get("/static/login.js")
    assert login_script.status_code == 200
    assert b"location.hash" in login_script.data
    assert b"history.replaceState" in login_script.data


def test_must_change_password_blocks_application_routes_until_password_changed(guest_client, auth_system):
    _, store, admin, password = auth_system
    store.replace_password_hash(admin.id, hash_password(password), must_change=True)
    login = guest_client.post("/api/auth/login", json={"username": admin.username, "password": password})
    assert login.status_code == 200
    blocked = guest_client.get("/api/library")
    assert blocked.status_code == 403
    assert blocked.get_json()["error"] == "password_change_required"
    allowed = guest_client.get("/api/auth/me")
    assert allowed.status_code == 200
    assert allowed.get_json()["must_change_password"] is True
