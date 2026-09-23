from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from podcast_article.object_storage import ObjectStorage, account_prefix


class FakeBucket:
    def __init__(self):
        self.keys = []

    def put_object_from_file(self, key, path, headers=None):
        self.keys.append(key)

    def object_exists(self, key):
        return True

    def sign_url(self, method, key, expires):
        return f"https://oss.example/{key}?expires={expires}"


def test_member_object_prefixes_are_separate_and_owner_prefix_is_legacy():
    alice_id, bob_id = str(uuid4()), str(uuid4())
    base = "existing/audio-prefix"

    alice = account_prefix(alice_id, base_prefix=base)
    bob = account_prefix(bob_id, base_prefix=base)
    owner = account_prefix(str(uuid4()), base_prefix=base, is_legacy_owner=True)

    assert alice == f"{base}/users/{alice_id}"
    assert bob == f"{base}/users/{bob_id}"
    assert alice != bob
    assert owner == base


def test_account_storage_keeps_old_owner_key_and_rejects_cross_prefix_access(tmp_path):
    owner_id, alice_id, bob_id = str(uuid4()), str(uuid4()), str(uuid4())
    bucket = FakeBucket()
    owner = ObjectStorage.for_account(owner_id, legacy_owner_id=owner_id,
                                      bucket=bucket, base_prefix="old-prefix")
    alice = ObjectStorage.for_account(alice_id, legacy_owner_id=owner_id,
                                      bucket=bucket, base_prefix="old-prefix")
    bob = ObjectStorage.for_account(bob_id, legacy_owner_id=owner_id,
                                    bucket=bucket, base_prefix="old-prefix")
    second_admin_id = str(uuid4())
    second_admin = ObjectStorage.for_account(second_admin_id, legacy_owner_id=owner_id,
                                             bucket=bucket, base_prefix="old-prefix")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    owner_key = owner.object_key(audio, "episode")
    alice_key = alice.object_key(audio, "episode")

    assert owner_key.startswith("old-prefix/")
    assert alice_key.startswith(f"old-prefix/users/{alice_id}/")
    assert second_admin.object_key(audio, "episode").startswith(
        f"old-prefix/users/{second_admin_id}/"
    )
    assert owner.exists(owner_key)
    assert alice.exists(alice_key)
    with pytest.raises(ValueError):
        bob.signed_url(alice_key)
