from pathlib import Path

from podcast_article.object_storage import ObjectStorage, ObjectRef


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.signed = []

    def put_object_from_file(self, key, filename, headers=None):
        self.objects[key] = {
            "bytes": Path(filename).read_bytes(),
            "headers": headers or {},
        }

    def object_exists(self, key):
        return key in self.objects

    def sign_url(self, method, key, expires):
        self.signed.append((method, key, expires))
        return f"https://oss.example/{key}?expires={expires}"


def test_object_key_is_safe_and_deterministic(tmp_path):
    audio = tmp_path / "A weird episode!.mp3"
    audio.write_bytes(b"audio")
    storage = ObjectStorage(bucket=FakeBucket(), prefix="podcast-article/audio")

    key = storage.object_key(audio, "测试 / Episode", sha256="abcdef123456")

    assert key == "podcast-article/audio/测试-episode/abcdef12-a-weird-episode.mp3"


def test_put_file_returns_metadata_and_uploads_content(tmp_path):
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"audio bytes")
    bucket = FakeBucket()
    storage = ObjectStorage(bucket=bucket, prefix="audio")

    ref = storage.put_file(audio, "audio/episode.mp3")

    assert isinstance(ref, ObjectRef)
    assert ref.key == "audio/episode.mp3"
    assert ref.size == 11
    assert ref.sha256 == "ef71589075ccf9332917b0d8d711d1a8d205560f96842f9221de70e6c29454e0"
    assert bucket.objects[ref.key]["bytes"] == b"audio bytes"
    assert bucket.objects[ref.key]["headers"]["Content-Type"] == "audio/mpeg"


def test_exists_and_signed_url_use_bucket(tmp_path):
    audio = tmp_path / "episode.mp3"
    audio.write_bytes(b"audio")
    bucket = FakeBucket()
    storage = ObjectStorage(bucket=bucket, prefix="audio")
    storage.put_file(audio, "audio/episode.mp3")

    assert storage.exists("audio/episode.mp3") is True
    assert storage.exists("audio/missing.mp3") is False
    assert storage.signed_url("audio/episode.mp3", expires=300) == (
        "https://oss.example/audio/episode.mp3?expires=300"
    )
    assert bucket.signed == [("GET", "audio/episode.mp3", 300)]
