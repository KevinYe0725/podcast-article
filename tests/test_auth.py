from __future__ import annotations

import pytest
from argon2 import Type, extract_parameters

from podcast_article.auth import hash_password, safe_next_path, validate_new_password, verify_password


def test_password_hash_is_argon2id_and_verifies():
    password = "a long test passphrase"
    encoded = hash_password(password)

    assert encoded.startswith("$argon2id$")
    assert encoded != password
    assert verify_password(encoded, password)
    assert not verify_password(encoded, "a different passphrase")
    assert not verify_password("not an argon2 hash", password)
    parameters = extract_parameters(encoded)
    assert parameters.type is Type.ID
    assert (parameters.memory_cost, parameters.time_cost, parameters.parallelism) == (19 * 1024, 2, 1)


def test_password_policy_uses_utf8_bytes_and_allows_spaces():
    validate_new_password("two words and more")
    validate_new_password("密码" * 6)

    with pytest.raises(ValueError):
        validate_new_password("short")
    with pytest.raises(ValueError):
        validate_new_password("密码" * 43)  # 258 UTF-8 bytes
    with pytest.raises(ValueError):
        validate_new_password("\ud800" * 12)


def test_password_verification_rejects_non_utf8_surrogate_input():
    encoded = hash_password("a long test passphrase")
    assert not verify_password(encoded, "\ud800")


@pytest.mark.parametrize("raw, expected", [(None, "/"), ("", "/"), ("/", "/"), ("/reader?q=one", "/reader?q=one")])
def test_safe_next_path_accepts_only_local_absolute_paths(raw, expected):
    assert safe_next_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["https://evil.example", "//evil.example/path", "///evil.example", "reader", r"/\\evil.example", "/login\r\nLocation: https://evil.example"],
)
def test_safe_next_path_rejects_external_or_malformed_redirects(raw):
    assert safe_next_path(raw) == "/"
