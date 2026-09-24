import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from podcast_article.pipeline import Pipeline
from podcast_article.sources.base import Episode


class FakeStorage:
    def __init__(self):
        self.uploads = []
        self.signs = []

    def object_key(self, path: Path, episode_slug: str, *, sha256=None):
        return f"audio/{episode_slug}/{path.name}"

    def put_file(self, path: Path, object_key: str):
        self.uploads.append((Path(path), object_key))
        return SimpleNamespace(
            key=object_key,
            content_type="audio/mpeg",
            size=Path(path).stat().st_size,
            sha256="abc123",
        )

    def exists(self, object_key):
        return False

    def signed_url(self, object_key, *, expires=900):
        self.signs.append((object_key, expires))
        return f"https://oss.example/{object_key}?expires={expires}"


def episode_for(path: Path):
    return SimpleNamespace(source="file", url=str(path), audio_url=None, subtitle_tracks=[])


def test_episode_metadata_can_be_reloaded_after_cloud_audio_archival(tmp_path):
    metadata = {
        "source": "xiaoyuzhou",
        "url": "https://www.xiaoyuzhoufm.com/episode/example",
        "title": "Episode",
        "podcast": "Show",
        "duration": 3221,
        "subtitle_tracks": [],
        "audio_storage": "oss",
        "audio_object_key": "audio/episode/audio.m4a",
        "audio_content_type": "audio/mp4",
        "audio_size": 1234,
        "audio_sha256": "abc123",
    }
    path = tmp_path / "meta.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")

    episode = Episode.from_dict(json.loads(path.read_text(encoding="utf-8")))

    assert episode.source == "xiaoyuzhou"
    assert episode.title == "Episode"
    assert episode.duration == 3221


def test_cloud_audio_is_uploaded_and_meta_records_object(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    output = tmp_path / "output"
    workdir = output / "episode"
    workdir.mkdir(parents=True)
    storage = FakeStorage()
    pipeline = Pipeline(url="", output_dir=output, backend="cloud", object_storage_client=storage)

    result = pipeline._stage_audio(episode_for(source), workdir)

    assert result == source
    assert storage.uploads == [(source, "audio/episode/source.mp3")]
    meta = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["audio_storage"] == "oss"
    assert meta["audio_object_key"] == "audio/episode/source.mp3"
    assert meta["audio_sha256"] == "abc123"


def test_cloud_transcript_uses_signed_object_url(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    output = tmp_path / "output"
    workdir = output / "episode"
    workdir.mkdir(parents=True)
    (workdir / "meta.json").write_text(
        json.dumps({"audio_storage": "oss", "audio_object_key": "audio/episode/audio.mp3"}),
        encoding="utf-8",
    )
    storage = FakeStorage()
    pipeline = Pipeline(url="", output_dir=output, backend="cloud", object_storage_client=storage)
    seen = {}

    def fake_transcribe(*args, **kwargs):
        seen["audio_url"] = kwargs["audio_url"]
        return [{"start": 0.0, "end": 1.0, "text": "你好"}]

    monkeypatch.setattr("podcast_article.pipeline.transcribe.transcribe", fake_transcribe)
    segments = pipeline._stage_transcript(episode_for(audio), workdir, audio)

    assert segments == [{"start": 0.0, "end": 1.0, "text": "你好"}]
    assert seen["audio_url"] == "https://oss.example/audio/episode/audio.mp3?expires=86400"
    assert storage.signs == [("audio/episode/audio.mp3", 86400)]


def test_cloud_quota_reservation_happens_after_metadata_before_audio_download(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    episode = SimpleNamespace(source="file", url=str(source), audio_url=None, subtitle_tracks=[],
                              duration=600, podcast="Test show", title="Test episode", pub_date=None,
                              author="", cover=None, shownotes_html=None,
                              to_dict=lambda: {"source": "file", "url": str(source), "title": "Test episode",
                                               "podcast": "Test show", "duration": 600})
    monkeypatch.setattr("podcast_article.pipeline.resolve", lambda *_args, **_kwargs: episode)
    seen = []

    def before_billable_asr(resolved_episode):
        seen.append(("reserve", resolved_episode.duration))
        return object()

    pipeline = Pipeline(url="", output_dir=tmp_path / "output", backend="cloud",
                         before_billable_asr=before_billable_asr)

    def no_download(*_args, **_kwargs):
        assert seen == [("reserve", 600)]
        raise RuntimeError("stop after reservation-order check")

    monkeypatch.setattr(pipeline, "_stage_audio", no_download)
    with pytest.raises(RuntimeError, match="reservation-order"):
        pipeline.run()


def test_cloud_asr_quota_rejection_stops_before_audio_download(tmp_path, monkeypatch):
    from podcast_article.platform_store import QuotaExceeded

    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    episode = SimpleNamespace(source="file", url=str(source), audio_url=None, subtitle_tracks=[],
                              duration=600, podcast="Test show", title="Test episode", pub_date=None,
                              author="", cover=None, shownotes_html=None,
                              to_dict=lambda: {"source": "file", "url": str(source), "title": "Test episode",
                                               "podcast": "Test show", "duration": 600})
    monkeypatch.setattr("podcast_article.pipeline.resolve", lambda *_args, **_kwargs: episode)
    pipeline = Pipeline(url="", output_dir=tmp_path / "output", backend="cloud",
                        before_billable_asr=lambda _ep: (_ for _ in ()).throw(
                            QuotaExceeded("asr", 10, 10, 0, 600)))
    monkeypatch.setattr(pipeline, "_stage_audio", lambda *_args, **_kwargs: pytest.fail("audio must not download"))

    with pytest.raises(QuotaExceeded):
        pipeline.run()


def test_cloud_asr_audio_over_job_limit_is_rejected_before_provider_call(tmp_path, monkeypatch):
    from podcast_article.platform_store import QuotaExceeded

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    workdir = tmp_path / "output" / "episode"
    workdir.mkdir(parents=True)
    (workdir / "meta.json").write_text(json.dumps({"audio_object_key": "audio/episode/audio.mp3"}),
                                         encoding="utf-8")
    episode = SimpleNamespace(source="file", url=str(audio), audio_url=None, subtitle_tracks=[], duration=600)
    pipeline = Pipeline(url="", output_dir=tmp_path / "output", backend="cloud",
                        object_storage_client=FakeStorage(), before_billable_asr=lambda *_: object(),
                        resize_billable_asr=lambda *_: pytest.fail(
                            "duration beyond the configured per-job cap must fail before reservation resize"))
    pipeline._asr_reservation = object()
    pipeline._billable_asr_used = False
    monkeypatch.setenv("PA_ASR_MAX_JOB_SECONDS", "7200")
    monkeypatch.setattr("podcast_article.pipeline._probe_audio_duration", lambda _path: 7_201)
    monkeypatch.setattr("podcast_article.pipeline.transcribe.transcribe",
                        lambda *_args, **_kwargs: pytest.fail("oversized audio must not reach ASR"))

    with pytest.raises(QuotaExceeded) as error:
        pipeline._stage_transcript(episode, workdir, audio)

    assert error.value.resource == "asr_job_seconds"
    assert pipeline._billable_asr_used is False


def test_cloud_asr_fails_closed_without_verified_duration(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    workdir = tmp_path / "output" / "episode"
    workdir.mkdir(parents=True)
    (workdir / "meta.json").write_text(json.dumps({"audio_object_key": "audio/episode/audio.mp3"}),
                                         encoding="utf-8")
    episode = SimpleNamespace(source="file", url=str(audio), audio_url=None, subtitle_tracks=[], duration=600)
    pipeline = Pipeline(url="", output_dir=tmp_path / "output", backend="cloud",
                        object_storage_client=FakeStorage(), before_billable_asr=lambda *_: object())
    pipeline._asr_reservation = object()
    pipeline._billable_asr_used = False
    monkeypatch.setattr("podcast_article.pipeline._probe_audio_duration", lambda _path: None)
    monkeypatch.setattr("podcast_article.pipeline.transcribe.transcribe",
                        lambda *_args, **_kwargs: pytest.fail("unverified audio must not reach ASR"))

    with pytest.raises(RuntimeError, match="ffprobe"):
        pipeline._stage_transcript(episode, workdir, audio)

    assert pipeline._billable_asr_used is False


def test_pipeline_cache_quota_stops_and_removes_new_oversized_workdir(tmp_path, monkeypatch):
    from podcast_article.platform_store import QuotaExceeded

    episode = SimpleNamespace(source="rss", url="https://example.test/episode", audio_url=None,
                              subtitle_tracks=[], duration=600, podcast="Test show",
                              title="Test episode", pub_date=None, author="", cover=None,
                              shownotes_html=None,
                              to_dict=lambda: {"source": "rss", "url": "https://example.test/episode",
                                               "title": "Test episode", "podcast": "Test show",
                                               "duration": 600})
    output = tmp_path / "output"
    monkeypatch.setattr("podcast_article.pipeline.resolve", lambda *_args, **_kwargs: episode)
    checks = [0]
    workdirs = []

    def check_cache():
        checks[0] += 1
        if checks[0] == 2:
            raise QuotaExceeded("cache_bytes", 10, 11, 0, 0)

    pipeline = Pipeline(url=episode.url, output_dir=output, backend="auto",
                        check_workspace_cache=check_cache)

    def write_audio(_episode, workdir):
        workdirs.append(workdir)
        audio = workdir / "audio.mp3"
        audio.write_bytes(b"oversized")
        return audio

    monkeypatch.setattr(pipeline, "_stage_audio", write_audio)

    with pytest.raises(QuotaExceeded):
        pipeline.run()

    assert not workdirs[0].exists()


def test_cache_quota_preserves_existing_article_when_forced_rewrite_would_exceed(tmp_path, monkeypatch):
    from podcast_article.platform_store import QuotaExceeded

    workdir = tmp_path / "output" / "episode"
    workdir.mkdir(parents=True)
    article_path = workdir / "article.md"
    article_path.write_text("previous article", encoding="utf-8")
    episode = SimpleNamespace(title="T", podcast="P", author="", shownotes_html=None)

    def reject_replacement(*, replacing=None, projected_bytes=None, requested_bytes=0):
        if replacing is None:
            assert requested_bytes > 0
            return
        if replacing != article_path:
            return
        assert replacing == article_path
        assert projected_bytes > 0
        raise QuotaExceeded("cache_bytes", 100, 99, 0, projected_bytes)

    pipeline = Pipeline(url="", output_dir=tmp_path / "output", check_workspace_cache=reject_replacement)
    pipeline.force_article = True
    monkeypatch.setattr("podcast_article.pipeline.summarize.write_article", lambda **kwargs: "new article")

    with pytest.raises(QuotaExceeded):
        pipeline._stage_article(episode, workdir, [{"text": "source", "start": 0, "end": 1}])

    assert article_path.read_text(encoding="utf-8") == "previous article"


def test_cache_quota_truncates_resumed_partial_audio_to_original_size(tmp_path, monkeypatch):
    from podcast_article.platform_store import QuotaExceeded

    episode = SimpleNamespace(source="rss", url="https://example.test/episode", audio_url=None,
                              subtitle_tracks=[], duration=600, podcast="Test show",
                              title="Test episode", pub_date=None, author="", cover=None,
                              shownotes_html=None,
                              to_dict=lambda: {"source": "rss", "url": "https://example.test/episode",
                                               "title": "Test episode", "podcast": "Test show",
                                               "duration": 600})
    output = tmp_path / "output"
    from podcast_article.util import episode_slug
    workdir = output / episode_slug("Test show", "Test episode", None)
    workdir.mkdir(parents=True)
    partial = workdir / "audio.mp3.part"
    partial.write_bytes(b"old-prefix")
    monkeypatch.setattr("podcast_article.pipeline.resolve", lambda *_args, **_kwargs: episode)
    checks = [0]

    def check_cache():
        checks[0] += 1
        if checks[0] == 2:
            raise QuotaExceeded("cache_bytes", 100, 101, 0, 0)

    pipeline = Pipeline(url=episode.url, output_dir=output, check_workspace_cache=check_cache)

    def resume_audio(_episode, _workdir):
        partial.write_bytes(b"old-prefix-extra-over-limit")
        return _workdir / "audio.mp3"

    monkeypatch.setattr(pipeline, "_stage_audio", resume_audio)
    with pytest.raises(QuotaExceeded):
        pipeline.run()

    assert partial.read_bytes() == b"old-prefix"
