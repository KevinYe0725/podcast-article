"""本地语音转写：优先 mlx-whisper（Apple Silicon GPU），回退 faster-whisper（CPU）。"""
from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

from .util import human_time

MLX_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_DEFAULT_MODEL = "large-v3-turbo"

# 手动下载模型的存放目录（见 README「网络问题」一节）：
# ~/.cache/podcast-article/models/<模型名>/
LOCAL_MODEL_DIR = "models"


def _has_mlx() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _has_faster() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _has_cloud() -> bool:
    return bool(os.environ.get("DASHSCOPE_API_KEY", "").strip())


def available_backends() -> list[str]:
    # Keep local backends first for developer machines, then use cloud ASR when
    # it is the only configured option (or when callers select it explicitly).
    order = ["mlx", "faster", "cloud"]
    checks = {"mlx": _has_mlx, "faster": _has_faster, "cloud": _has_cloud}
    return [b for b in order if checks[b]()]


def transcribe(
    audio_path: Path,
    language: str = "auto",
    backend: str = "auto",
    model: str | None = None,
    audio_url: str | None = None,
    log=print,
    progress=None,
) -> list[dict]:
    """返回 [{start, end, text}]（秒）。

    progress(stage, data)：可选的进度回调，如 ("asr", {"pct": 42.0, "eta_s": 95})。
    """
    if backend == "auto":
        candidates = available_backends()
        if not candidates:
            raise RuntimeError(
                "没有可用的转写后端。安装其一：\n"
                "  uv add mlx-whisper        (Apple Silicon 推荐)\n"
                "  uv add --optional faster faster-whisper   (CPU 通用)\n"
                "  配置 DASHSCOPE_API_KEY                    (云端 ASR)"
            )
    else:
        candidates = [backend]

    if backend == "cloud" and not audio_url:
        raise ValueError("cloud 转写需要 audio_url")

    errors: list[str] = []
    for cand in candidates:
        try:
            if cand == "mlx":
                return _transcribe_mlx(audio_path, language, model, log, progress)
            if cand == "faster":
                return _transcribe_faster(audio_path, language, model, log, progress)
            if cand == "cloud":
                if not audio_url:
                    raise ValueError("cloud 转写需要 audio_url")
                return _transcribe_cloud(audio_url, language, model, log, progress)
        except Exception as exc:  # 后端失败则尝试下一个
            errors.append(f"{cand}: {exc}")
            log(f"[warn] 转写后端 {cand} 失败：{exc}，尝试下一个…")
    raise RuntimeError("所有转写后端都失败了：\n" + "\n".join(errors))


def _transcribe_cloud(audio_url: str, language: str, model: str | None, log, progress=None) -> list[dict]:
    from .cloud_asr import CloudASRClient

    selected_model = model or os.environ.get("PA_ASR_MODEL", "paraformer-v2")
    log(f"[asr] 云端模型：{selected_model}")
    client = CloudASRClient()
    return client.transcribe(
        audio_url, language, selected_model,
        progress=(lambda data: progress("asr", data)) if progress else None,
    )


def _norm_language(language: str) -> str | None:
    return None if (not language or language == "auto") else language


def _resolve_mlx_model(model: str | None) -> str:
    """支持：默认 / hf repo id / 本地目录 / 缓存目录名 / '4bit'·'8bit' 简写。"""
    shorthand = {"4bit": "whisper-large-v3-turbo-4bit", "8bit": "whisper-large-v3-turbo-8bit"}
    if not model:
        # 未指定时优先用本地已缓存的模型，避免首次联网下载大文件
        cache = Path.home() / ".cache/podcast-article" / LOCAL_MODEL_DIR
        if cache.is_dir():
            for name in list(shorthand.values()) + sorted(
                p.name for p in cache.iterdir() if p.is_dir()
            ):
                if (cache / name).is_dir():
                    return str(cache / name)
        return MLX_DEFAULT_MODEL
    candidate = Path(model).expanduser()
    if candidate.exists():
        return str(candidate)
    name = shorthand.get(model, model)
    local = Path.home() / ".cache/podcast-article" / LOCAL_MODEL_DIR / name
    if local.is_dir():
        return str(local)
    return name


# mlx-whisper 用 tqdm 在 stderr 上打帧进度（\r 刷新），这里解析成结构化进度
_TQDM_RE = re.compile(r"(\d{1,3})%\|[^|]*\|\s*(\d+)/(\d+)\s*\[([^<]*)<\s*([^\],]*)")


def _eta_to_sec(text: str) -> float | None:
    parts = [p for p in text.strip().split(":") if p != ""]
    try:
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    except ValueError:
        return None


class _TqdmCapture(io.TextIOBase):
    """透传 stderr 内容的同时，从 tqdm 输出里解析进度并回调。"""

    def __init__(self, original, on_progress):
        self._orig = original
        self._on = on_progress
        self._buf = ""

    def write(self, s):
        self._orig.write(s)
        self._buf += s
        while True:
            cut = min(
                (i for i in (self._buf.find("\r"), self._buf.find("\n")) if i != -1),
                default=-1,
            )
            if cut == -1:
                break
            seg, self._buf = self._buf[:cut], self._buf[cut + 1 :]
            m = _TQDM_RE.search(seg)
            if m:
                eta = _eta_to_sec(m.group(5))
                self._on(
                    {
                        "pct": float(m.group(1)),
                        "done": int(m.group(2)),
                        "total": int(m.group(3)),
                        "eta_s": round(eta) if eta is not None else None,
                    }
                )
        return len(s)

    def flush(self):
        self._orig.flush()


def _transcribe_mlx(audio_path: Path, language: str, model: str | None, log, progress=None) -> list[dict]:
    import mlx_whisper

    repo = _resolve_mlx_model(model)
    log(f"[asr] mlx-whisper 模型：{repo}")
    kwargs = dict(
        path_or_hf_repo=repo,
        language=_norm_language(language),
        verbose=False,
    )
    if progress is None:
        result = mlx_whisper.transcribe(str(audio_path), **kwargs)
    else:
        orig = sys.stderr
        sys.stderr = _TqdmCapture(orig, lambda d: progress("asr", d))
        try:
            result = mlx_whisper.transcribe(str(audio_path), **kwargs)
        finally:
            sys.stderr = orig
    segments = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
        for s in result.get("segments", [])
        if s.get("text", "").strip()
    ]
    return segments


def _transcribe_faster(audio_path: Path, language: str, model: str | None, log, progress=None) -> list[dict]:
    from faster_whisper import WhisperModel

    size = model or FASTER_DEFAULT_MODEL
    log(f"[asr] faster-whisper 模型：{size}（CPU int8）")
    wmodel = WhisperModel(size, device="auto", compute_type="int8")
    seg_iter, info = wmodel.transcribe(
        str(audio_path),
        language=_norm_language(language),
        vad_filter=True,
    )
    total = info.duration or 0
    segments: list[dict] = []
    last_logged = -30.0
    last_prog = -5.0
    for s in seg_iter:
        text = s.text.strip()
        if not text:
            continue
        segments.append({"start": float(s.start), "end": float(s.end), "text": text})
        if total:
            if progress and s.end / total * 100 - last_prog >= 2:
                last_prog = s.end / total * 100
                progress("asr", {"pct": round(last_prog, 1)})
            if s.end - last_logged >= 60:
                last_logged = s.end
                log(f"[asr] 进度 {human_time(s.end)} / {human_time(total)}")
    return segments
