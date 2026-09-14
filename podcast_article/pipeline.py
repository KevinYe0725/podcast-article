"""全流程编排：抓取 → 音频 → 文字稿 → 文章，带目录级缓存（每步产物存在就跳过）。"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from . import summarize, transcribe
from . import subtitles as subs
from .sources import resolve
from .sources.ytdlp_src import download_audio
from .util import episode_slug, ts_clock

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


class Pipeline:
    def __init__(
        self,
        url: str,
        output_dir: Path,
        language: str = "auto",
        backend: str = "auto",
        asr_model: str | None = None,
        llm_model: str | None = None,
        no_subs: bool = False,
        force_transcript: bool = False,
        force_article: bool = False,
        pick: int = 1,
        max_chars: int = 75_000,
        log=print,
        progress=None,
    ):
        self.url = url
        self.output_dir = output_dir
        self.language = language
        self.backend = backend
        self.asr_model = asr_model
        self.llm_model = llm_model
        self.no_subs = no_subs
        self.force_transcript = force_transcript
        self.force_article = force_article
        self.pick = pick
        self.max_chars = max_chars
        self.log = log
        self.progress = progress  # progress(stage: str, data: dict)

    # ------------------------------------------------------------ 阶段 1：元信息

    def run(self) -> Path:
        ep = resolve(self.url, pick=self.pick)
        self.log(f"[meta] {ep.podcast or ep.source}｜{ep.title}")

        workdir = self.output_dir / episode_slug(ep.podcast, ep.title, ep.pub_date)
        workdir.mkdir(parents=True, exist_ok=True)
        meta_path = workdir / "meta.json"

        if meta_path.exists():
            ep = Episode_from_dict(_read_json(meta_path))
            self.log(f"[meta] 复用已有元信息：{workdir.name}")
        else:
            meta_path.write_text(
                json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        audio_file = self._stage_audio(ep, workdir)
        segments = self._stage_transcript(ep, workdir, audio_file)
        article = self._stage_article(ep, workdir, segments)
        return article

    # ------------------------------------------------------------ 阶段 2：音频

    def _stage_audio(self, ep, workdir: Path) -> Path:
        existing = sorted(workdir.glob("audio.*"))
        existing = [p for p in existing if p.suffix != ".part"]
        if existing:
            self.log(f"[audio] 复用已下载音频：{existing[0].name}")
            return existing[0]

        self.log("[audio] 开始下载音频…")
        if ep.source == "file":
            path = Path(ep.url)  # 本地文件本身就是音频
        elif ep.source in ("youtube", "bilibili"):
            path = Path(download_audio(ep.url, str(workdir / "audio"), progress=self.progress))
        elif ep.audio_url:
            path = _download_url(ep.audio_url, workdir, progress=self.progress)
        else:
            raise RuntimeError("元信息里既没有音频直链也不支持 yt-dlp 下载")

        if path.suffix == ".part":
            raise RuntimeError(f"音频下载不完整：{path}")
        self.log(f"[audio] 完成：{path.name}")
        return path

    # ------------------------------------------------------------ 阶段 3：文字稿

    def _stage_transcript(self, ep, workdir: Path, audio_file: Path) -> list[dict]:
        t_path = workdir / "transcript.json"
        if t_path.exists() and not self.force_transcript:
            segments = _read_json(t_path)
            self.log(f"[text] 复用已有文字稿：{len(segments)} 个片段")
            return segments

        segments = None
        if ep.subtitle_tracks and not self.no_subs:
            segments = self._try_subtitles(ep)
            if segments:
                self.log(f"[text] 使用平台字幕：{len(segments)} 个片段（跳过 ASR）")

        if not segments:
            self.log("[text] 平台无可用字幕（或已禁用），进入本地语音转写…")
            segments = transcribe.transcribe(
                audio_file,
                language=self.language,
                backend=self.backend,
                model=self.asr_model,
                log=self.log,
                progress=self.progress,
            )
            self.log(f"[text] 转写完成：{len(segments)} 个片段")

        _write_json(t_path, segments)
        self._write_readable_transcript(workdir / "transcript.txt", segments)
        return segments

    def _try_subtitles(self, ep) -> list[dict] | None:
        for track in ep.subtitle_tracks:
            try:
                self.log(f"[text] 尝试平台字幕：{track.lang}（{'自动' if track.auto else '人工'}，{track.ext}）")
                segments = subs.download_track(track.url, track.ext)
                if len(segments) >= 5:
                    return segments
            except Exception as exc:
                self.log(f"[text] 字幕 {track.lang} 获取失败：{exc}")
        return None

    @staticmethod
    def _write_readable_transcript(path: Path, segments: list[dict]) -> None:
        lines = [f"[{ts_clock(s['start'])}] {s['text']}" for s in segments]
        path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------ 阶段 4：文章

    def _stage_article(self, ep, workdir: Path, segments: list[dict]) -> Path:
        a_path = workdir / "article.md"
        if a_path.exists() and not self.force_article:
            self.log("[write] 复用已有文章")
            return a_path

        text_chars = sum(len(s["text"]) for s in segments)
        self.log(f"[write] 开始生成文章（文字稿约 {text_chars} 字）…")
        article = summarize.write_article(
            segments=segments,
            title=ep.title,
            podcast=ep.podcast,
            author=ep.author,
            shownotes_html=ep.shownotes_html,
            max_chars=self.max_chars,
            llm_model=self.llm_model,
            log=self.log,
            progress=self.progress,
        )
        if not article:
            raise RuntimeError("模型没有返回任何内容")
        a_path.write_text(article + "\n", encoding="utf-8")
        self.log("[write] 文章完成")
        return a_path


# ---------------------------------------------------------------- 辅助

def _download_url(audio_url: str, workdir: Path, progress=None) -> Path:
    suffix = Path(audio_url.split("?")[0]).suffix or ".mp3"
    dest = workdir / f"audio{suffix}"
    with requests.get(audio_url, headers={"User-Agent": _UA}, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        tmp = dest.with_suffix(dest.suffix + ".part")
        done = 0
        last_pct = -100.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    pct = done / total * 100
                    if pct - last_pct >= 2:  # 每 2% 上报一次
                        last_pct = pct
                        progress("download", {
                            "pct": round(pct, 1),
                            "downloaded": done,
                            "total": total,
                        })
        tmp.rename(dest)
    return dest


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def Episode_from_dict(d: dict):
    from .sources.base import Episode

    return Episode.from_dict(d)
