import json
from pathlib import Path

import pytest

from podcast_article.cloud_asr import CloudASRClient, CloudASRError, CloudASRTimeoutError
from podcast_article import transcribe


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def test_submit_sends_async_request_with_timestamp_options():
    session = FakeSession([FakeResponse({"output": {"task_id": "task-1"}})])
    client = CloudASRClient(api_key="key", base_url="https://asr.example", session=session)

    assert client.submit("https://oss.example/audio.mp3", "zh", "paraformer-v2", diarization=True) == "task-1"
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/api/v1/services/audio/asr/transcription")
    assert kwargs["headers"]["X-DashScope-Async"] == "enable"
    assert kwargs["json"]["input"]["file_urls"] == ["https://oss.example/audio.mp3"]
    assert kwargs["json"]["parameters"]["timestamp_alignment_enabled"] is True
    assert kwargs["json"]["parameters"]["diarization_enabled"] is True


def test_wait_polls_and_parses_sentence_timestamps(monkeypatch):
    result_url = "https://asr.example/result.json"
    session = FakeSession([
        FakeResponse({"output": {"task_status": "RUNNING"}}),
        FakeResponse({"output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": result_url}]}}),
        FakeResponse({"transcripts": [{"sentences": [
            {"begin_time": 1200, "end_time": 3450, "text": "你好。"},
            {"begin_time": 3450, "end_time": 5000, "text": "世界。"},
        ]}]}),
    ])
    client = CloudASRClient(api_key="key", base_url="https://asr.example", session=session)
    monkeypatch.setattr("podcast_article.cloud_asr.time.sleep", lambda _seconds: None)
    progress = []

    segments = client.wait("task-1", progress=lambda data: progress.append(data), timeout_s=10)

    assert segments == [
        {"start": 1.2, "end": 3.45, "text": "你好。"},
        {"start": 3.45, "end": 5.0, "text": "世界。"},
    ]
    assert progress[0]["state"] == "RUNNING"
    assert progress[-1]["state"] == "SUCCEEDED"


def test_submit_http_error_is_cloud_error():
    session = FakeSession([FakeResponse({"message": "bad key"}, status=401)])
    client = CloudASRClient(api_key="key", session=session)

    with pytest.raises(CloudASRError, match="提交转写任务失败"):
        client.submit("https://oss.example/audio.mp3", "auto", "paraformer-v2")


def test_wait_timeout_is_distinct(monkeypatch):
    session = FakeSession([FakeResponse({"output": {"task_status": "RUNNING"}})])
    client = CloudASRClient(api_key="key", session=session)
    monkeypatch.setattr("podcast_article.cloud_asr.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("podcast_article.cloud_asr.time.sleep", lambda _seconds: None)

    with pytest.raises(CloudASRTimeoutError):
        client.wait("task-1", timeout_s=0)


def test_transcribe_cloud_requires_audio_url(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "key")
    with pytest.raises(ValueError, match="audio_url"):
        transcribe.transcribe(Path("audio.mp3"), backend="cloud")
