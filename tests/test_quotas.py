from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import time

import pytest

from podcast_article.platform_store import AccountQuota, InviteError, PlatformStore, QuotaExceeded
from podcast_article.quota import (
    LLMQuotaGuard,
    UnknownModelPrice,
    check_cache_capacity,
    check_upload_size,
    asr_reservation_seconds,
    month_key,
    quote_llm_upper_bound,
)


def _member(store, admin, username, quota):
    import hashlib
    import secrets
    import time

    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    store.create_invite(admin.id, token_hash, quota, time.time() + 3_600)
    return store.register_invite(token_hash, username, "$argon2id$test", now=time.time())


def test_llm_quote_is_decimal_and_uses_conservative_price_and_input_bytes():
    quote = quote_llm_upper_bound("deepseek-flash", prompt_utf8_bytes=1_000, max_tokens=100)
    assert quote == Decimal("0.00315")
    assert isinstance(quote, Decimal)


def test_unknown_llm_model_fails_closed_before_provider_invocation():
    with pytest.raises(UnknownModelPrice):
        quote_llm_upper_bound("unpriced-model", prompt_utf8_bytes=10, max_tokens=10)


def test_month_key_uses_asia_shanghai_calendar_month():
    from datetime import datetime, timezone

    assert month_key(datetime(2026, 9, 30, 16, 30, tzinfo=timezone.utc)) == "2026-10"


def test_unknown_asr_duration_reserves_a_configured_maximum(monkeypatch):
    monkeypatch.setenv("PA_ASR_MAX_JOB_SECONDS", "5400")
    assert asr_reservation_seconds(None) == 5_400
    assert asr_reservation_seconds(360.2) == 361
    with pytest.raises(QuotaExceeded) as error:
        asr_reservation_seconds(6_000)
    assert error.value.resource == "asr_job_seconds"


def test_asr_and_llm_quotas_are_independent_per_account(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(100, Decimal("0.01")))
    bob = _member(platform_store, platform_admin, "bob", AccountQuota(200, Decimal("0.02")))

    alice_asr = platform_store.reserve_asr(alice.id, "asr-a", "2026-09", 100)
    with pytest.raises(QuotaExceeded):
        platform_store.reserve_asr(alice.id, "asr-b", "2026-09", 1)
    bob_asr = platform_store.reserve_asr(bob.id, "asr-b", "2026-09", 100)
    assert alice_asr.reserved_amount == 100 and bob_asr.reserved_amount == 100

    llm_quote = Decimal("0.01")
    alice_llm = platform_store.reserve_llm_call(alice.id, "llm-a", "2026-09", llm_quote)
    with pytest.raises(QuotaExceeded):
        platform_store.reserve_llm_call(alice.id, "llm-over", "2026-09", Decimal("0.0001"))
    bob_llm = platform_store.reserve_llm_call(bob.id, "llm-b", "2026-09", llm_quote)
    assert alice_llm.owner_id == alice.id and bob_llm.owner_id == bob.id


def test_exact_asr_cap_settlement_is_idempotent_and_releases_reserve(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(100, Decimal("1.00")))
    reservation = platform_store.reserve_asr(alice.id, "job-a", "2026-09", 100)

    settled = platform_store.settle_reservation(reservation.id, actual_amount=60)
    repeated = platform_store.settle_reservation(reservation.id, actual_amount=60)
    totals = platform_store.quota_usage(alice.id, "2026-09")

    assert settled.status == "settled"
    assert repeated.actual_amount == 60
    assert totals["asr_used_seconds"] == 60
    assert totals["asr_reserved_seconds"] == 0
    platform_store.reserve_asr(alice.id, "job-b", "2026-09", 40)


def test_asr_reservation_can_be_adjusted_to_verified_audio_duration(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(100, Decimal("1.00")))
    reservation = platform_store.reserve_asr(alice.id, "job-a", "2026-09", 60)

    expanded = platform_store.resize_asr_reservation(reservation.id, 90)
    assert expanded.reserved_amount == 90
    assert platform_store.quota_usage(alice.id, "2026-09")["asr_reserved_seconds"] == 90
    with pytest.raises(QuotaExceeded):
        platform_store.resize_asr_reservation(reservation.id, 101)

    reduced = platform_store.resize_asr_reservation(reservation.id, 40)
    assert reduced.reserved_amount == 40
    assert platform_store.quota_usage(alice.id, "2026-09")["asr_reserved_seconds"] == 40


def test_provider_overrun_is_recorded_and_blocks_future_reservations(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(100, Decimal("1.00")))
    reservation = platform_store.reserve_asr(alice.id, "job-a", "2026-09", 80)

    settled = platform_store.settle_reservation(reservation.id, actual_amount=120)
    totals = platform_store.quota_usage(alice.id, "2026-09")

    assert settled.actual_amount == 120
    assert totals["asr_used_seconds"] == 120
    assert totals["asr_reserved_seconds"] == 0
    with pytest.raises(QuotaExceeded):
        platform_store.reserve_asr(alice.id, "job-b", "2026-09", 1)


def test_concurrent_reservations_cannot_consume_the_same_remaining_asr_allowance(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(10, Decimal("1.00")))

    def reserve(job_id):
        try:
            return platform_store.reserve_asr(alice.id, job_id, "2026-09", 10).id
        except QuotaExceeded:
            return "over-cap"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("job-a", "job-b")))
    assert outcomes.count("over-cap") == 1
    assert sum(value != "over-cap" for value in outcomes) == 1


def test_llm_guard_rejects_before_call_and_settles_provider_usage(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(3_600, Decimal("0.001")))
    guard = LLMQuotaGuard(platform_store, alice.id, "2026-09")
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise AssertionError("provider must not be called after quota rejection")

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    from podcast_article.summarize import _chat

    with pytest.raises(QuotaExceeded):
        _chat(FakeClient(), "deepseek-flash", "system" * 200, "user" * 200,
              max_tokens=2_000, quota_guard=guard)
    assert calls == []


def test_llm_guard_settles_actual_usage_and_releases_unused_quote(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(3_600, Decimal("0.01")))
    guard = LLMQuotaGuard(platform_store, alice.id, "2026-09")
    reservation = guard.reserve_llm_call("deepseek-flash", "system", "user", max_tokens=100)
    assert reservation.price_table_version == "deepseek-2026-09-24-usd-cny-7_5-v2"
    settled = guard.settle_llm_call(reservation, "deepseek-flash", {
        "prompt_tokens": 10, "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 10, "completion_tokens": 5,
    })
    totals = platform_store.quota_usage(alice.id, "2026-09")
    assert settled.status == "settled"
    assert totals["llm_reserved_cny"] == Decimal("0")
    assert totals["llm_used_cny"] > Decimal("0")


def test_llm_guard_quotes_all_conversation_history(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(3_600, Decimal("1.00")))
    guard = LLMQuotaGuard(platform_store, alice.id, "2026-09")
    history = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]

    reservation = guard.reserve_llm_call(
        "deepseek-flash", "系统", "当前问题", max_tokens=100, history=history,
    )

    prompt_bytes = sum(len(text.encode("utf-8")) for text in (
        "系统", "当前问题", "上一轮问题", "上一轮回答",
    )) + 64 * 4
    assert reservation.reserved_amount == quote_llm_upper_bound("deepseek-flash", prompt_bytes, 100)


def test_quota_guard_resolves_month_for_each_provider_reservation(platform_store, platform_admin, monkeypatch):
    import podcast_article.quota as quota

    alice = _member(platform_store, platform_admin, "alice", AccountQuota(7_200, Decimal("1.00")))
    months = iter(("2026-09", "2026-10"))
    monkeypatch.setattr(quota, "month_key", lambda: next(months))
    guard = LLMQuotaGuard(platform_store, alice.id)

    september = guard.reserve_asr("job-september", 10)
    october = guard.reserve_asr("job-october", 10)

    assert september.month_key == "2026-09"
    assert october.month_key == "2026-10"


def test_stale_reservations_are_conservatively_settled(platform_store, platform_admin):
    alice = _member(platform_store, platform_admin, "alice", AccountQuota(7_200, Decimal("1.00")))
    reservation = platform_store.reserve_asr(alice.id, "crashed-job", "2026-09", 900)

    now = time.time() + 3_600
    reconciled = platform_store.reconcile_stale_reservations(now=now, max_age_seconds=1_800)
    totals = platform_store.quota_usage(alice.id, "2026-09")

    assert reconciled == 1
    assert totals["asr_used_seconds"] == 900
    assert totals["asr_reserved_seconds"] == 0
    assert platform_store.reconcile_stale_reservations(now=now + 400, max_age_seconds=1_800) == 0


def test_cache_upload_and_queue_dimensions_fail_closed_when_over_limit():
    with pytest.raises(QuotaExceeded):
        check_cache_capacity(used_bytes=90, requested_bytes=11, limit_bytes=100)
    with pytest.raises(QuotaExceeded):
        check_upload_size(size_bytes=101, limit_bytes=100)
    assert check_cache_capacity(used_bytes=50, requested_bytes=50, limit_bytes=100) is None
