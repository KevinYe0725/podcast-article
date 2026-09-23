import json
from pathlib import Path
from types import SimpleNamespace

from podcast_article.pipeline import Pipeline


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
