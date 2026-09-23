from __future__ import annotations

import hashlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from podcast_article.platform_store import AccountQuota, PlatformStore, QuotaExceeded


def _member(store, admin, username, queue_limit=10):
    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    store.create_invite(admin.id, token_hash, AccountQuota(3_600, Decimal("1.00"), 1_000_000,
                                                           queue_limit, 2_000_000), time.time() + 3_600)
    return store.register_invite(token_hash, username, "$argon2id$test", now=time.time())


def test_platform_jobs_rotate_accounts_and_keep_fifo_within_each_account(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice")
    bob = _member(platform_store, platform_admin, "bob")
    enqueued = [
        platform_store.enqueue_job(alice.id, {"url": "alice-1"}, created_at=1),
        platform_store.enqueue_job(alice.id, {"url": "alice-2"}, created_at=2),
        platform_store.enqueue_job(bob.id, {"url": "bob-1"}, created_at=1),
        platform_store.enqueue_job(bob.id, {"url": "bob-2"}, created_at=2),
    ]

    claimed = []
    for _ in range(4):
        job = platform_store.next_job()
        assert job is not None
        claimed.append(job)
        assert platform_store.finish_job(job.owner_id, job.id, "done")
    owners = [job.owner_id for job in claimed]
    assert owners[0] != owners[1]
    assert owners[0] == owners[2]
    assert owners[1] == owners[3]
    for owner_id in {alice.id, bob.id}:
        urls = [job.payload["url"] for job in claimed if job.owner_id == owner_id]
        assert urls[0].endswith("-1") and urls[1].endswith("-2")
    assert all(job.state == "running" for job in claimed)
    assert len(enqueued) == 4


def test_global_queue_does_not_claim_a_second_job_while_one_is_running(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice")
    bob = _member(platform_store, platform_admin, "bob")
    first = platform_store.enqueue_job(alice.id, {"url": "alice"}, created_at=1)
    platform_store.enqueue_job(bob.id, {"url": "bob"}, created_at=2)

    assert platform_store.claim_job(alice.id, first.id) is not None
    assert platform_store.next_job() is None


def test_platform_jobs_are_owner_scoped_and_queue_limit_is_enforced(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", queue_limit=1)
    bob = _member(platform_store, platform_admin, "bob", queue_limit=1)
    alice_job = platform_store.enqueue_job(alice.id, {"url": "alice private"}, created_at=1)
    bob_job = platform_store.enqueue_job(bob.id, {"url": "bob private"}, created_at=2)

    with pytest.raises(QuotaExceeded):
        platform_store.enqueue_job(alice.id, {"url": "alice extra"}, created_at=3)
    assert platform_store.get_job(alice.id, alice_job.id).payload["url"] == "alice private"
    assert platform_store.get_job(bob.id, alice_job.id) is None
    assert platform_store.list_jobs(alice.id)[0].id == alice_job.id
    assert platform_store.list_jobs(bob.id)[0].id == bob_job.id


def test_batch_enqueue_rejects_atomically_when_account_queue_cap_is_too_small(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", queue_limit=1)
    with pytest.raises(QuotaExceeded):
        platform_store.enqueue_jobs(alice.id, [{"url": "first"}, {"url": "second"}], created_at=1)
    assert platform_store.list_jobs(alice.id) == []


def test_concurrent_enqueue_deduplicates_active_episode_inside_transaction(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice")
    payload = {"url": "https://example.test/episode", "pick": 1}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: platform_store.enqueue_jobs(alice.id, [payload], created_at=time.time()),
            range(2),
        ))

    assert sum(len(items) for items in results) == 1
    assert len(platform_store.list_jobs(alice.id)) == 1


def test_platform_job_claim_is_atomic(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice")
    job = platform_store.enqueue_job(alice.id, {"url": "one"}, created_at=1)
    assert platform_store.claim_job(alice.id, job.id) is not None
    assert platform_store.claim_job(alice.id, job.id) is None
    assert platform_store.finish_job(alice.id, job.id, "done", dir_name="episode-a") is True
    assert platform_store.finish_job(alice.id, job.id, "done", dir_name="episode-a") is True
    assert platform_store.get_job(alice.id, job.id).state == "done"
