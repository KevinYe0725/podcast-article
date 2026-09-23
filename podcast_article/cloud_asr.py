"""Alibaba Cloud Model Studio asynchronous file transcription client."""
from __future__ import annotations

import os
import time
from collections.abc import Callable

import requests


class CloudASRError(RuntimeError):
    pass


class CloudASRTimeoutError(CloudASRError):
    pass


def _segments_from_result(payload: dict) -> list[dict]:
    transcripts = payload.get("transcripts") or payload.get("output", {}).get("transcripts")
    if not isinstance(transcripts, list):
        raise CloudASRError("转写结果缺少 transcripts")
    segments: list[dict] = []
    for transcript in transcripts:
        for sentence in transcript.get("sentences", []):
            start_ms = sentence.get("begin_time")
            end_ms = sentence.get("end_time")
            text = str(sentence.get("text") or "").strip()
            if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
                raise CloudASRError("转写结果缺少句子时间戳")
            if start_ms < 0 or end_ms < start_ms or not text:
                raise CloudASRError("转写结果包含无效句子时间戳")
            segments.append({
                "start": round(float(start_ms) / 1000, 3),
                "end": round(float(end_ms) / 1000, 3),
                "text": text,
            })
    if not segments:
        raise CloudASRError("转写结果没有句子")
    return segments


class CloudASRClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 session: requests.Session | None = None):
        self.api_key = (api_key or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        if not self.api_key:
            raise CloudASRError("未配置 DASHSCOPE_API_KEY")
        self.base_url = (base_url or os.environ.get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com"
        )).rstrip("/")
        self.session = session or requests.Session()

    def _headers(self, *, async_request: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if async_request:
            headers["X-DashScope-Async"] = "enable"
        return headers

    def submit(self, audio_url: str, language: str, model: str,
               diarization: bool = False) -> str:
        parameters: dict = {
            "timestamp_alignment_enabled": True,
            "diarization_enabled": bool(diarization),
        }
        if language and language != "auto":
            parameters["language_hints"] = [language]
        body = {
            "model": model,
            "input": {"file_urls": [audio_url]},
            "parameters": parameters,
        }
        try:
            response = self.session.post(
                f"{self.base_url}/api/v1/services/audio/asr/transcription",
                headers=self._headers(async_request=True), json=body, timeout=30,
            )
            response.raise_for_status()
            task_id = response.json().get("output", {}).get("task_id")
        except Exception as exc:
            raise CloudASRError(f"提交转写任务失败：{exc}") from exc
        if not task_id:
            raise CloudASRError("提交转写任务失败：响应中没有 task_id")
        return str(task_id)

    def wait(self, task_id: str, progress: Callable[[dict], None] | None = None,
             timeout_s: int = 43_200) -> list[dict]:
        deadline = time.monotonic() + timeout_s
        delay = 2.0
        while True:
            if time.monotonic() >= deadline:
                raise CloudASRTimeoutError(f"转写任务超时：{task_id}")
            try:
                response = self.session.get(
                    f"{self.base_url}/api/v1/tasks/{task_id}",
                    headers=self._headers(), timeout=30,
                )
                response.raise_for_status()
                output = response.json().get("output") or {}
            except Exception as exc:
                raise CloudASRError(f"查询转写任务失败：{exc}") from exc
            status = str(output.get("task_status") or "").upper()
            if progress:
                progress({"state": status or "UNKNOWN", "task_id": task_id})
            if status in {"FAILED", "CANCELED"}:
                raise CloudASRError(str(output.get("message") or f"转写任务失败：{status}"))
            if status == "SUCCEEDED":
                results = output.get("results") or []
                result_url = results[0].get("transcription_url") if results else output.get("transcription_url")
                if not result_url:
                    raise CloudASRError("转写成功但没有 transcription_url")
                try:
                    result_response = self.session.get(result_url, timeout=30)
                    result_response.raise_for_status()
                    return _segments_from_result(result_response.json())
                except CloudASRError:
                    raise
                except Exception as exc:
                    raise CloudASRError(f"下载转写结果失败：{exc}") from exc
            time.sleep(delay)
            delay = min(delay * 1.5, 30.0)

    def transcribe(self, audio_url: str, language: str, model: str,
                   progress: Callable[[dict], None] | None = None) -> list[dict]:
        task_id = self.submit(audio_url, language, model)
        if progress:
            progress({"state": "SUBMITTED", "task_id": task_id})
        return self.wait(task_id, progress=progress)
