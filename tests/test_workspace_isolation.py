from __future__ import annotations

from decimal import Decimal
from threading import Barrier, Thread

import pytest

from podcast_article import qa_store, usage


def _write_episode(workspace, slug="same-episode", title="Private title"):
    episode = workspace.output_root / slug
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "meta.json").write_text('{"title": "' + title + '"}', encoding="utf-8")
    (episode / "article.md").write_text("# " + title + "\n\n" + "private content " * 40, encoding="utf-8")
    (episode / "transcript.txt").write_text("[00:00:01] private transcript\n", encoding="utf-8")
    (episode / "audio.m4a").write_bytes(title.encode("utf-8"))
    (episode / "cover.png").write_bytes(title.encode("utf-8"))
    return episode


def test_same_slug_files_exports_and_qa_are_confined_to_owner(two_user_clients):
    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]
    slug = "same-episode"
    alice_episode = _write_episode(alice["workspace"], slug, "Alice private title")
    bob_episode = _write_episode(bob["workspace"], slug, "Bob private title")
    qa_store.append(alice_episode, selection="alice selection", question="alice question",
                    answer="Alice QA secret", thread="alice-thread")
    qa_store.append(bob_episode, selection="bob selection", question="bob question",
                    answer="Bob QA secret", thread="bob-thread")

    assert b"Alice private title" in alice["client"].get(f"/api/file/{slug}/article.md").data
    assert b"Bob private title" in bob["client"].get(f"/api/file/{slug}/article.md").data
    assert alice["client"].get(f"/api/file/{slug}/article.md").status_code == 200
    assert bob["client"].get(f"/api/audio/{slug}").data == b"Bob private title"
    assert bob["client"].get(f"/api/cover/{slug}").data == b"Bob private title"
    bob_export = bob["client"].get(f"/api/export/{slug}?fmt=md")
    assert bob_export.status_code == 200 and b"Bob private title" in bob_export.data
    assert b"Alice private title" not in bob_export.data
    assert "Bob QA secret" in str(bob["client"].get(f"/api/qa?dir={slug}&thread=bob-thread").get_json())

    assert "Alice private title" not in str(bob["client"].get(f"/api/file/{slug}/article.md").get_json())
    assert "Alice QA secret" not in str(bob["client"].get(f"/api/qa?dir={slug}&thread=alice-thread").get_json())


def test_job_ask_tts_and_kb_status_are_owner_scoped(two_user_clients):
    import webapp

    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]
    slug = "same-episode"
    _write_episode(alice["workspace"], slug, "Alice title")
    _write_episode(bob["workspace"], slug, "Bob title")

    job_id = "owner-job-123"
    webapp._JOBS[job_id] = {
        "id": job_id, "owner_id": alice["account"].id, "url": "https://private.example/alice",
        "source": "manual", "status": "running", "logs": ["Alice private log"],
        "progress": None, "usage": {"cost_cny": 1.25, "marker": "Alice usage"},
        "article_html": "Alice body", "meta": {},
        "workdir": slug, "error": None,
    }
    ask_id = "owner-ask-123"
    webapp._ASKS[ask_id] = {
        "id": ask_id, "owner_id": alice["account"].id, "status": "done", "stage": "write",
        "answer": "Alice private answer", "sources": {"private": True}, "thread": "alice-thread",
        "error": "", "at": 1,
    }
    webapp._TTS_JOBS[(alice["account"].id, slug)] = {"state": "running", "note": "Alice private audio"}
    webapp._KB_JOBS[alice["account"].id] = {"state": "running", "note": "Alice private index"}

    assert alice["client"].get(f"/api/job/{job_id}").status_code == 200
    assert bob["client"].get(f"/api/job/{job_id}").status_code == 404
    assert bob["client"].get(f"/api/stream/{job_id}").status_code == 404
    assert alice["client"].get("/api/jobs/current").get_json()["url"] == "https://private.example/alice"
    assert bob["client"].get(f"/api/ask/{ask_id}").status_code == 404
    assert bob["client"].get(f"/api/ask/{ask_id}/stream").status_code == 404
    assert "private.example/alice" in str(alice["client"].get("/api/jobs/current").get_json())
    assert "Alice private" not in str(bob["client"].get("/api/jobs/current").get_json())
    bob_usage = bob["client"].get("/api/usage").get_json()
    assert bob_usage["live"] is None and bob_usage["busy"] is False
    assert "Alice usage" in str(alice["client"].get("/api/usage").get_json()["live"])
    assert "Alice private index" not in str(bob["client"].get("/api/kb/status").get_json())
    assert "Alice private audio" not in str(bob["client"].get(f"/api/tts/{slug}/status").get_json())
    assert bob["client"].get("/api/kb/status").get_json()["indexing"]["state"] == "idle"
    assert bob["client"].get(f"/api/tts/{slug}/status").get_json()["state"] == "idle"

    webapp._JOBS.pop(job_id, None)
    webapp._ASKS.pop(ask_id, None)
    webapp._TTS_JOBS.pop((alice["account"].id, slug), None)
    webapp._KB_JOBS.pop(alice["account"].id, None)


def test_platform_queue_items_are_visible_only_to_their_owner(two_user_clients):
    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]
    added = alice["client"].post("/api/queue", json={"url": "https://example.test/alice-private"})
    assert added.status_code == 200
    item_id = added.get_json()["added"][0]["id"]
    assert item_id in str(alice["client"].get("/api/queue").get_json())
    assert item_id not in str(bob["client"].get("/api/queue").get_json())
    assert bob["client"].delete(f"/api/queue/{item_id}").status_code == 404


def test_queue_api_rejects_over_cap_batch_without_partial_enqueue(two_user_clients):
    alice = two_user_clients["alice"]
    urls = [f"https://example.test/episode-{index}" for index in range(6)]
    response = alice["client"].post("/api/queue", json={"urls": urls})
    assert response.status_code == 429
    assert response.get_json()["resource"] == "queue_items"
    assert alice["client"].get("/api/queue").get_json()["items"] == []


def test_global_worker_busy_response_does_not_expose_another_owners_url(two_user_clients, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]
    webapp._JOBS["alice-running-job"] = {
        "id": "alice-running-job", "owner_id": alice["account"].id,
        "url": "https://private.example/alice-secret", "status": "running",
    }
    monkeypatch.setattr(webapp, "_new_job", lambda *args, **kwargs: "unexpected-job")

    response = bob["client"].post("/api/run", json={"url": "https://example.test/bob"})

    assert response.status_code == 409
    assert response.get_json()["busy"] is True
    assert "private.example/alice-secret" not in response.get_data(as_text=True)
    assert "alice-running-job" not in response.get_data(as_text=True)
    webapp._JOBS.pop("alice-running-job", None)


def test_generation_returns_429_when_no_llm_quota_remains(two_user_clients, monkeypatch):
    import webapp
    from podcast_article.quota import month_key

    alice = two_user_clients["alice"]
    store = webapp.app.config["PLATFORM_STORE"]
    store.reserve_llm_call(alice["account"].id, "consume-month", month_key(), Decimal("5.00"))
    monkeypatch.setattr(webapp, "_new_job", lambda *args, **kwargs: pytest.fail("quota must reject before task creation"))

    response = alice["client"].post("/api/run", json={
        "url": "https://example.test/episode", "force_article": True,
    })

    assert response.status_code == 429
    assert response.get_json()["error"] == "quota_exceeded"
    assert response.get_json()["resource"] == "llm_month_cny"


def test_cloud_transcription_returns_429_when_asr_quota_is_exhausted(two_user_clients, monkeypatch):
    import webapp
    from podcast_article.quota import month_key

    alice = two_user_clients["alice"]
    store = webapp.app.config["PLATFORM_STORE"]
    store.reserve_asr(alice["account"].id, "consume-asr-month", month_key(), 3_600)
    monkeypatch.setattr(webapp, "_new_job", lambda *args, **kwargs: pytest.fail("ASR quota must reject before task creation"))

    response = alice["client"].post("/api/run", json={
        "url": "https://example.test/episode", "backend": "cloud", "no_subs": True,
        "force_transcript": True,
    })
    assert response.status_code == 429
    assert response.get_json()["resource"] == "asr_month_seconds"


def test_run_does_not_block_cache_eligible_work_on_exhausted_model_quota(two_user_clients, monkeypatch):
    import webapp
    from podcast_article.quota import month_key

    alice = two_user_clients["alice"]
    store = webapp.app.config["PLATFORM_STORE"]
    store.reserve_llm_call(alice["account"].id, "consume-month", month_key(), Decimal("5.00"))
    monkeypatch.setattr(webapp, "_new_job", lambda *args, **kwargs: "cached-eligible-job")

    response = alice["client"].post("/api/run", json={"url": "https://example.test/episode"})

    assert response.status_code == 200
    assert response.get_json()["job_id"] == "cached-eligible-job"


def test_deepdive_question_is_not_blocked_by_exhausted_asr_quota(two_user_clients, monkeypatch):
    import webapp
    from podcast_article import deepdive
    from podcast_article.platform_store import QuotaExceeded
    from podcast_article import settings as settings_mod
    from podcast_article.quota import month_key

    alice = two_user_clients["alice"]
    store = webapp.app.config["PLATFORM_STORE"]
    store.reserve_asr(alice["account"].id, "consume-asr-month", month_key(), 3_600)
    settings_mod.save(generation={"backend": "cloud", "no_subs": True},
                      settings_path=alice["workspace"].settings_path)
    (alice["workspace"].output_root / "episode").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(deepdive, "retrieve", lambda *args, **kwargs: [])
    def fail_for_llm_quota(**kwargs):
        raise QuotaExceeded("llm", Decimal("5.00"), Decimal("4.99"), Decimal("0"), Decimal("0.02"))
    monkeypatch.setattr(deepdive, "stream_answer", fail_for_llm_quota)
    monkeypatch.setattr(webapp.threading.Thread, "start", lambda self: self.run())

    response = alice["client"].post("/api/ask", json={
        "dir": "episode", "selection": "这一段", "question": "为什么？", "web": False,
    })

    assert response.status_code == 200
    ask_id = response.get_json().get("id")
    status = alice["client"].get(f"/api/ask/{ask_id}").get_json()
    assert status["error_code"] == "quota_exceeded"
    assert status["quota"] == {"resource": "llm", "remaining": "0.01"}


def test_deepdive_question_is_rejected_before_llm_when_cache_headroom_is_too_small(two_user_clients, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    episode = alice["workspace"].output_root / "episode"
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "meta.json").write_text('{"title":"episode"}', encoding="utf-8")
    (alice["workspace"].output_root / "large-cache.bin").write_bytes(b"x" * 950_000)
    monkeypatch.setattr(webapp.threading.Thread, "start", lambda self: pytest.fail("cache must preflight before LLM"))

    response = alice["client"].post("/api/ask", json={
        "dir": "episode", "selection": "这一段", "question": "为什么？", "web": False,
    })

    assert response.status_code == 429
    assert response.get_json()["resource"] == "cache_bytes"


def test_tts_is_rejected_before_generation_when_cache_headroom_is_too_small(two_user_clients, monkeypatch):
    import webapp
    from podcast_article import settings as settings_mod

    alice = two_user_clients["alice"]
    episode = alice["workspace"].output_root / "episode"
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "article.md").write_text("# episode\n\ncontent", encoding="utf-8")
    (alice["workspace"].output_root / "large-cache.bin").write_bytes(b"x" * 900_000)
    settings_mod.save(tts={"provider": "openai"}, settings_path=alice["workspace"].settings_path)
    monkeypatch.setattr(webapp.threading.Thread, "start", lambda self: pytest.fail("cache must preflight before TTS"))

    response = alice["client"].post("/api/tts/episode", json={})

    assert response.status_code == 429
    assert response.get_json()["resource"] == "cache_bytes"


def test_cover_backfill_removes_cover_that_would_exceed_cache_limit(two_user_clients, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    episode = alice["workspace"].output_root / "episode"
    episode.mkdir(parents=True, exist_ok=True)
    (episode / "meta.json").write_text(
        '{"cover":"https://images.example/cover.jpg"}', encoding="utf-8",
    )
    (alice["workspace"].output_root / "large-cache.bin").write_bytes(b"x" * 999_000)

    def fake_cover(_url, workdir):
        path = workdir / "cover.jpg"
        path.write_bytes(b"x" * 5_000)
        return path

    monkeypatch.setattr(webapp.cover_mod, "fetch", fake_cover)
    response = alice["client"].post("/api/covers/backfill")

    assert response.status_code == 200
    assert response.get_json()["filled"] == []
    assert response.get_json()["failed"] == [{"dir": "episode", "why": "缓存空间不足"}]
    assert not (episode / "cover.jpg").exists()


def test_generation_checks_account_cache_and_local_upload_limits(two_user_clients, tmp_path, monkeypatch):
    import webapp

    alice = two_user_clients["alice"]
    monkeypatch.setattr(webapp, "_new_job", lambda *args, **kwargs: "cache-eligible-job")
    oversized_file = tmp_path / "oversized.mp3"
    oversized_file.write_bytes(b"x" * 2_000_001)
    upload = alice["client"].post("/api/run", json={"url": str(oversized_file)})
    assert upload.status_code == 429
    assert upload.get_json()["resource"] == "max_upload_bytes"

    full_cache = alice["workspace"].output_root / "cache-limit.bin"
    full_cache.parent.mkdir(parents=True, exist_ok=True)
    full_cache.write_bytes(b"x" * 1_100_000)
    cache = alice["client"].post("/api/run", json={"url": "https://example.test/episode"})
    assert cache.status_code == 200
    assert cache.get_json()["job_id"] == "cache-eligible-job"

    forced = alice["client"].post("/api/run", json={
        "url": "https://example.test/episode", "force_article": True,
    })
    assert forced.status_code == 429
    assert forced.get_json()["resource"] == "cache_bytes"


def test_monthly_usage_quota_summary_is_independent_by_account(two_user_clients):
    import webapp
    from podcast_article.quota import month_key

    alice = two_user_clients["alice"]
    bob = two_user_clients["bob"]
    store = webapp.app.config["PLATFORM_STORE"]
    month = month_key()
    asr = store.reserve_asr(alice["account"].id, "usage-asr", month, 100)
    store.settle_reservation(asr.id, actual_amount=60)
    llm = store.reserve_llm_call(alice["account"].id, "usage-llm", month, Decimal("0.50"))
    store.settle_reservation(llm.id, actual_amount=Decimal("0.20"))

    alice_quota = alice["client"].get("/api/usage").get_json()["quota"]
    bob_quota = bob["client"].get("/api/usage").get_json()["quota"]
    assert alice_quota["asr"]["used_seconds"] == 60
    assert alice_quota["asr"]["remaining_seconds"] == 3_540
    assert alice_quota["llm"]["used_cny"] == "0.20"
    assert alice_quota["llm"]["remaining_cny"] == "4.80"
    assert bob_quota["asr"]["used_seconds"] == 0
    assert bob_quota["llm"]["used_cny"] == "0"


def test_usage_recorders_are_independent_under_interleaved_calls(tmp_path):
    alice = usage.Recorder("deepseek-flash", tmp_path / "alice")
    bob = usage.Recorder("deepseek-v4-pro", tmp_path / "bob")
    barrier = Barrier(2)

    def record(recorder, tokens):
        barrier.wait()
        usage.note("deepseek-flash", {"prompt_tokens": tokens, "completion_tokens": 1}, recorder=recorder)

    first = Thread(target=record, args=(alice, 11))
    second = Thread(target=record, args=(bob, 29))
    first.start(); second.start(); first.join(); second.join()

    assert alice.usage["miss_tokens"] == 11
    assert bob.usage["miss_tokens"] == 29
    assert alice.flush()["calls"] == bob.flush()["calls"] == 1
