from __future__ import annotations

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
