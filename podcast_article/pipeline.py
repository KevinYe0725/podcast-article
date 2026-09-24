"""全流程编排：抓取 → 音频 → 文字稿 → 文章，带目录级缓存（每步产物存在就跳过）。"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import ssl
import subprocess
import time
from pathlib import Path

import requests

from . import config, cover as cover_mod, object_storage, summarize, transcribe, usage
from . import subtitles as subs
from .platform_store import QuotaExceeded
from .sources import resolve
from .sources.ytdlp_src import download_audio
from .util import episode_slug, ts_clock

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------------------------------------------------------- 下载重试配置
# 环境变量（和 config.py 一样直接读 os.environ，调用时即时生效，方便单次覆盖/测试）：
#   PA_DOWNLOAD_RETRIES  下载总尝试次数，默认 3（= 最多重试 2 次）
#   PA_DOWNLOAD_BACKOFF  逗号分隔的退避秒数，默认 "2,5"；次数不够时重复最后一个值
DEFAULT_DOWNLOAD_RETRIES = 3
DEFAULT_DOWNLOAD_BACKOFF = "2,5"

# 跨国链路最先崩的是握手：连接超时给短一点，读超时（两次收到数据之间的最长等待）给宽一点
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 60.0


def _probe_audio_duration(audio_path: Path) -> float | None:
    """Read the downloaded media duration when ffprobe is installed."""
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(audio_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            return None
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读整数环境变量：缺省、非法或小于下限时用默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_backoff(name: str, default: str) -> list[float]:
    """读逗号分隔的退避秒数（如 "2,5"）：非法项忽略，全读不出来时回退默认值。"""
    raw = os.environ.get(name, "").strip() or default
    waits: list[float] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            waits.append(max(0.0, float(part)))
        except ValueError:
            continue
    if waits:
        return waits
    return [float(p) for p in default.split(",") if p.strip()]


def download_retries() -> int:
    """下载总尝试次数（PA_DOWNLOAD_RETRIES，默认 3）。"""
    return _env_int("PA_DOWNLOAD_RETRIES", DEFAULT_DOWNLOAD_RETRIES)


def download_backoff() -> list[float]:
    """每次重试前的等待秒数（PA_DOWNLOAD_BACKOFF，默认 2,5）。"""
    return _env_backoff("PA_DOWNLOAD_BACKOFF", DEFAULT_DOWNLOAD_BACKOFF)


def _backoff_seconds(retry_no: int, waits: list[float]) -> float:
    """第 retry_no 次重试（从 1 开始）该等多久；序列不够长就重复最后一个值。"""
    if not waits:
        return 0.0
    return waits[min(retry_no - 1, len(waits) - 1)]


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
        mode: str | None = None,
        polish: bool = False,
        outlined: bool = True,
        log=print,
        progress=None,
        on_usage=None,
        episode: dict | None = None,
        settings_path: Path | None = None,
        quota_guard=None,
        before_billable_asr=None,
        resize_billable_asr=None,
        settle_billable_asr=None,
        release_billable_asr=None,
        check_workspace_cache=None,
        object_storage_client=None,
        object_storage_prefix: str | None = None,
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
        self.mode = mode  # 篇幅档位：concise / standard / deep
        self.polish = polish  # 生成后是否做编辑复检
        self.outlined = outlined  # 是否用大纲 + 逐节写作
        self.log = log
        self.progress = progress  # progress(stage: str, data: dict)
        self.on_usage = on_usage  # on_usage(usage: dict)，每次 LLM 调用后回调（用于实时显示花费）
        self.episode = episode    # 预先解析好的单集快照（订阅发现时固定下来，见 webapp）
        self.settings_path = settings_path
        self.quota_guard = quota_guard
        self.before_billable_asr = before_billable_asr
        self.resize_billable_asr = resize_billable_asr
        self.settle_billable_asr = settle_billable_asr
        self.release_billable_asr = release_billable_asr
        self.check_workspace_cache = check_workspace_cache
        self._actual_audio_seconds: int | None = None
        self._billable_asr_used = False
        self.object_storage = object_storage_client
        self.object_storage_prefix = object_storage_prefix

    def _cloud_storage(self):
        if self.object_storage is None:
            self.object_storage = object_storage.ObjectStorage(prefix=self.object_storage_prefix)
        return self.object_storage

    def _update_meta(self, workdir: Path, values: dict) -> None:
        path = workdir / "meta.json"
        current = _read_json(path) if path.exists() else {}
        current.update(values)
        _write_json(path, current, check_workspace_cache=self.check_workspace_cache)

    # ------------------------------------------------------------ 阶段 1：元信息

    def run(self) -> Path:
        # 订阅场景下不重新解析链接，直接用入队时抓到的单集快照。
        # 原因：feed 每次更新，「第 N 集」这种相对定位都会整体前移，排队中的任务会跑到别的单集上。
        if self.episode:
            ep = Episode_from_dict(dict(self.episode))
            self.log(f"[meta] 使用订阅时抓取的单集信息：{ep.title}")
        else:
            ep = resolve(self.url, pick=self.pick)
        self.log(f"[meta] {ep.podcast or ep.source}｜{ep.title}")

        workdir = self.output_dir / episode_slug(ep.podcast, ep.title, ep.pub_date)
        workdir_existed = workdir.exists()
        workdir.mkdir(parents=True, exist_ok=True)
        preexisting_files = {path for path in workdir.rglob("*") if path.is_file() or path.is_symlink()}
        preexisting_sizes = {}
        for path in preexisting_files:
            try:
                preexisting_sizes[path] = path.stat().st_size
            except OSError:
                continue
        meta_path = workdir / "meta.json"

        if meta_path.exists():
            ep = Episode_from_dict(_read_json(meta_path))
            self.log(f"[meta] 复用已有元信息：{workdir.name}")
        else:
            meta_path.write_text(
                json.dumps(ep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )

        transcript_path = workdir / "transcript.json"
        reservation = None
        self._billable_asr_used = False
        self._asr_reservation = None
        self._actual_audio_seconds = None
        should_check_asr_quota = (
            self.backend == "cloud"
            and self.before_billable_asr
            and (self.force_transcript or not transcript_path.exists())
        )
        if should_check_asr_quota and (self.no_subs or not ep.subtitle_tracks):
            reservation = self.before_billable_asr(ep)
            self._asr_reservation = reservation
        try:
            self._stage_cover(ep, workdir)
            if self.check_workspace_cache:
                self.check_workspace_cache()
            audio_before = set(workdir.glob("audio.*"))
            audio_file = self._stage_audio(ep, workdir)
            if self.check_workspace_cache:
                try:
                    self.check_workspace_cache()
                except Exception as cache_exc:
                    if audio_file.parent == workdir and audio_file not in audio_before:
                        resumed_part = next((path for path in audio_before
                                             if path.name.endswith(".part")
                                             and path.with_suffix("") == audio_file), None)
                        if (isinstance(cache_exc, QuotaExceeded) and resumed_part is not None
                                and resumed_part in preexisting_sizes and audio_file.exists()):
                            try:
                                with audio_file.open("r+b") as stream:
                                    stream.truncate(preexisting_sizes[resumed_part])
                                audio_file.replace(resumed_part)
                            except OSError:
                                audio_file.unlink(missing_ok=True)
                        else:
                            audio_file.unlink(missing_ok=True)
                    raise
            segments = self._stage_transcript(ep, workdir, audio_file)
            if self.check_workspace_cache:
                self.check_workspace_cache()
        except Exception as exc:
            reservation = reservation or self._asr_reservation
            if reservation is not None:
                if self._billable_asr_used and self.settle_billable_asr:
                    self.settle_billable_asr(reservation, int(reservation.reserved_amount))
                elif self.release_billable_asr:
                    self.release_billable_asr(reservation)
            if isinstance(exc, QuotaExceeded) and exc.resource == "cache_bytes":
                if workdir_existed:
                    for path, original_size in preexisting_sizes.items():
                        if path.name.endswith(".part"):
                            try:
                                if path.is_file() and path.stat().st_size > original_size:
                                    with path.open("r+b") as stream:
                                        stream.truncate(original_size)
                            except OSError:
                                pass
                    for path in workdir.rglob("*"):
                        try:
                            if path.is_file() and path not in preexisting_files:
                                path.unlink(missing_ok=True)
                        except OSError:
                            pass
                else:
                    shutil.rmtree(workdir, ignore_errors=True)
            raise
        reservation = reservation or self._asr_reservation
        if reservation is not None:
            if self._billable_asr_used and self.settle_billable_asr:
                actual_seconds = self._actual_audio_seconds or 0
                duration = ep.duration
                if actual_seconds <= 0:
                    try:
                        actual_seconds = max(1, math.ceil(float(duration))) if duration else 0
                    except (TypeError, ValueError, OverflowError):
                        actual_seconds = 0
                if actual_seconds == 0:
                    try:
                        actual_seconds = math.ceil(max(float(segment.get("end") or 0) for segment in segments))
                    except (TypeError, ValueError, OverflowError):
                        actual_seconds = 0
                if actual_seconds <= 0:
                    actual_seconds = int(reservation.reserved_amount)
                self.settle_billable_asr(reservation, actual_seconds)
            elif self.release_billable_asr:
                self.release_billable_asr(reservation)
        article = self._stage_article(ep, workdir, segments)
        if self.check_workspace_cache:
            try:
                self.check_workspace_cache()
            except QuotaExceeded as exc:
                if exc.resource == "cache_bytes":
                    if workdir_existed:
                        for path in workdir.rglob("*"):
                            try:
                                if path.is_file() and path not in preexisting_files:
                                    path.unlink(missing_ok=True)
                            except OSError:
                                pass
                    else:
                        shutil.rmtree(workdir, ignore_errors=True)
                raise
        return article

    def _stage_cover(self, ep, workdir: Path) -> None:
        """把封面图落到这一集目录里（卡片要用）。拿不到就算了，不影响出文章。"""
        if cover_mod.find(workdir):
            return
        if not getattr(ep, "cover", None):
            self.log("[meta] 这个来源没有给封面图，卡片将不显示缩略图")
            return
        path = cover_mod.ensure(ep.cover, workdir)
        self.log(f"[meta] 封面：{path.name}" if path else "[meta] 封面下载失败（不影响出文章）")

    # ------------------------------------------------------------ 阶段 2：音频

    def _stage_audio(self, ep, workdir: Path) -> Path:
        existing = sorted(workdir.glob("audio.*"))
        existing = [p for p in existing if p.suffix != ".part"]
        if existing:
            self.log(f"[audio] 复用已下载音频：{existing[0].name}")
            path = existing[0]
            if self.backend == "cloud":
                self._archive_audio(path, workdir)
            return path

        # 上次断在中途时残留的 audio.mp3.part / audio.m4a.part 不会删：先告诉用户会续传
        partial = [p for p in sorted(workdir.glob("audio.*")) if p.suffix == ".part"]
        if partial:
            self.log(
                f"[audio] 发现未完成的临时文件 {partial[0].name}"
                f"（{partial[0].stat().st_size} 字节），将从断点继续下载…"
            )

        self.log("[audio] 开始下载音频…")
        try:
            if ep.source == "file":
                path = Path(ep.url)  # 本地文件本身就是音频
            elif ep.source == "bilibili" and ep.audio_url:
                from .sources import bilibili_api

                path = Path(bilibili_api.download(
                    ep.audio_url, str(workdir / "audio"),
                    progress=self.progress, log=self.log,
                ))
            elif ep.source in ("youtube", "bilibili"):
                path = Path(download_audio(
                    ep.url, str(workdir / "audio"), progress=self.progress, log=self.log,
                ))
            elif ep.audio_url:
                path = _download_url(
                    ep.audio_url, workdir, progress=self.progress, log=self.log,
                )
            else:
                raise RuntimeError("元信息里既没有音频直链也不支持 yt-dlp 下载")
        except Exception as exc:
            # .part 临时文件保留在原地，下次运行（或本次重试）就能接着下，不用从头再来
            self.log(f"[audio] 下载失败：{exc}")
            raise

        if path.suffix == ".part":
            raise RuntimeError(f"音频下载不完整：{path}")
        self.log(f"[audio] 完成：{path.name}")
        if self.backend == "cloud":
            self._archive_audio(path, workdir)
        return path

    def _archive_audio(self, path: Path, workdir: Path) -> None:
        storage = self._cloud_storage()
        existing_meta = _read_json(workdir / "meta.json") if (workdir / "meta.json").exists() else {}
        key = existing_meta.get("audio_object_key") or storage.object_key(path, workdir.name)
        if not storage.exists(key):
            ref = storage.put_file(path, key)
            self.log(f"[audio] OSS 归档：{ref.key}")
            values = {
                "audio_storage": "oss",
                "audio_object_key": ref.key,
                "audio_content_type": ref.content_type,
                "audio_size": ref.size,
                "audio_sha256": ref.sha256,
            }
        else:
            self.log(f"[audio] 复用 OSS 归档：{key}")
            values = {"audio_storage": "oss", "audio_object_key": key}
        self._update_meta(workdir, values)

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
            self.log("[text] 平台无可用字幕（或已禁用），进入语音转写…")
            audio_url = None
            if self.backend == "cloud":
                meta = _read_json(workdir / "meta.json")
                key = meta.get("audio_object_key")
                if not key:
                    raise RuntimeError("云端转写缺少 OSS 音频对象")
                try:
                    asr_url_expires = max(3600, int(os.environ.get("PA_ASR_URL_EXPIRES", "86400")))
                except ValueError:
                    asr_url_expires = 86400
                audio_url = self._cloud_storage().signed_url(key, expires=asr_url_expires)
            if self.backend == "cloud":
                if self.before_billable_asr:
                    from .quota import asr_reservation_seconds

                    measured_duration = _probe_audio_duration(audio_file)
                    if measured_duration is None:
                        raise RuntimeError("无法验证云端转写音频时长；请安装 ffprobe 后重试")
                    self._actual_audio_seconds = asr_reservation_seconds(measured_duration)
                    if self._asr_reservation is None:
                        self._asr_reservation = self.before_billable_asr(ep, self._actual_audio_seconds)
                    elif self.resize_billable_asr:
                        self._asr_reservation = self.resize_billable_asr(
                        self._asr_reservation, self._actual_audio_seconds
                        )
                    elif self._actual_audio_seconds > int(self._asr_reservation.reserved_amount):
                        raise RuntimeError("云端转写额度预留无法扩展到已验证的音频时长")
                    if self.check_workspace_cache:
                        reserve_bytes = min(4 * 1024 * 1024,
                                            64 * 1024 + self._actual_audio_seconds * 256)
                        self.check_workspace_cache(requested_bytes=reserve_bytes)
                if self.before_billable_asr and self._asr_reservation is None:
                    self._asr_reservation = self.before_billable_asr(ep)
                self._billable_asr_used = True
            segments = transcribe.transcribe(
                audio_file,
                language=self.language,
                backend=self.backend,
                model=self.asr_model,
                audio_url=audio_url,
                log=self.log,
                progress=self.progress,
            )
            self.log(f"[text] 转写完成：{len(segments)} 个片段")

        readable_path = workdir / "transcript.txt"
        readable_content = "\n".join(f"[{ts_clock(s['start'])}] {s['text']}" for s in segments)
        cache_write_guard = None
        if self.check_workspace_cache:
            json_content = json.dumps(segments, ensure_ascii=False, indent=1)
            cache_write_guard = self.check_workspace_cache(replacements={
                t_path: len(json_content.encode("utf-8")),
                readable_path: len(readable_content.encode("utf-8")),
            })
        if cache_write_guard is not None and hasattr(cache_write_guard, "__enter__"):
            with cache_write_guard:
                _write_json(t_path, segments)
                readable_path.write_text(readable_content, encoding="utf-8")
        else:
            _write_json(t_path, segments)
            readable_path.write_text(readable_content, encoding="utf-8")
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

    # ------------------------------------------------------------ 阶段 4：文章

    def _stage_article(self, ep, workdir: Path, segments: list[dict]) -> Path:
        a_path = workdir / "article.md"
        if a_path.exists() and not self.force_article:
            self.log("[write] 复用已有文章")
            return a_path

        if self.check_workspace_cache:
            estimate = max(128 * 1024, min(4 * 1024 * 1024, self.max_chars * 4))
            self.check_workspace_cache(requested_bytes=estimate)

        text_chars = sum(len(s["text"]) for s in segments)
        self.log(f"[write] 开始生成文章（文字稿约 {text_chars} 字）…")
        # 记账：重写文章时把上一次的花费一起带上（这一集真实花掉的钱是有意义的）
        model = self.llm_model or config.deepseek_model()
        meter = usage.Recorder(model, workdir, on_update=self.on_usage,
                               started=usage.load(workdir),
                               before_save=(
                                   lambda path, size: self.check_workspace_cache(
                                       replacing=path, projected_bytes=size
                                   )
                               ) if self.check_workspace_cache else None)
        if self.on_usage:
            self.on_usage(usage.describe(meter.usage))
        try:
            article = summarize.write_article(
                segments=segments,
                title=ep.title,
                podcast=ep.podcast,
                author=ep.author,
                shownotes_html=ep.shownotes_html,
                max_chars=self.max_chars,
                llm_model=self.llm_model,
                mode=self.mode,
                polish=self.polish,
                outlined=self.outlined,
                settings_path=self.settings_path,
                usage_recorder=meter,
                quota_guard=self.quota_guard,
                log=self.log,
                progress=self.progress,
            )
        finally:
            snapshot = meter.flush()
            if self.on_usage:
                self.on_usage(usage.describe(snapshot))
        if snapshot.get("calls"):
            self.log(f"[write] 本次用量：输入 {snapshot['hit_tokens'] + snapshot['miss_tokens']:,} "
                     f"tokens（缓存命中 {snapshot['hit_tokens']:,}）· "
                     f"输出 {snapshot['out_tokens']:,} tokens · "
                     f"约 {usage.cost_cny(snapshot):.3f} 元")
        if not article:
            raise RuntimeError("模型没有返回任何内容")
        content = article + "\n"
        if self.check_workspace_cache:
            guard = self.check_workspace_cache(replacing=a_path, projected_bytes=len(content.encode("utf-8")))
        else:
            guard = None
        if guard is not None and hasattr(guard, "__enter__"):
            with guard:
                a_path.write_text(content, encoding="utf-8")
        else:
            a_path.write_text(content, encoding="utf-8")
        self.log("[write] 文章完成")
        return a_path


# ---------------------------------------------------------------- 辅助

class DownloadIncompleteError(RuntimeError):
    """本次拿到的数据不完整（长度校验不过），属于可重试的瞬时问题。"""


def _http_status(exc: requests.HTTPError) -> int | None:
    """从 HTTPError 里取状态码：优先读 response，其次从消息里的三位数兜底。"""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b(\d{3})\b", str(exc))
    return int(m.group(1)) if m else None


def _is_retryable(exc: BaseException) -> bool:
    """判断这次失败值不值得重试：只认网络抖动与服务端瞬时故障。

    - 重试：连接错误、超时、分块传输被掐断（ChunkedEncodingError），以及 5xx 与 429；
    - 不重试：其他 4xx（404/403 等）是确定性错误，重试只会白等白耗流量；
    - 长度校验不过（DownloadIncompleteError）也算可重试：多半是被中途掐断。
    """
    if isinstance(exc, DownloadIncompleteError):
        return True
    if isinstance(exc, requests.HTTPError):
        status = _http_status(exc)
        return status is not None and (status >= 500 or status == 429)
    if isinstance(exc, (
        requests.ConnectionError,
        requests.Timeout,
        # 注意：ChunkedEncodingError 只在 requests.exceptions 下，requests 顶层没有这个别名
        requests.exceptions.ChunkedEncodingError,
    )):
        return True
    # urllib3 / 标准库偶尔会漏出没被 requests 包装的裸异常：
    # socket.timeout 就是 TimeoutError，ssl.SSLError 则是跨国链路上最常见的 TLS 断连
    return isinstance(exc, (ConnectionError, TimeoutError, ssl.SSLError))


def _int_header(value: str | None) -> int | None:
    """解析数字头；拿不到就返回 None（表示「未知」，不做校验）。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _content_range(header: str | None) -> tuple[int | None, int | None]:
    """解析 Content-Range：'bytes 100-999/1000' → (100, 1000)；'bytes */1000' → (None, 1000)。"""
    m = re.match(r"bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)", str(header or "").strip(), re.I)
    if not m:
        return None, None
    start = int(m.group(1)) if m.group(1) else None
    total = int(m.group(3)) if m.group(3) and m.group(3) != "*" else None
    return start, total


def _download_once(audio_url: str, dest: Path, tmp: Path, progress=None, log=print) -> None:
    """尝试下载一次：断点续传 + 长度校验，全部下完才 rename 成最终文件名。"""
    offset = tmp.stat().st_size if tmp.exists() else 0
    headers = {"User-Agent": _UA}
    if offset:
        headers["Range"] = f"bytes={offset}-"  # 断点续传：只讨剩下那一段

    with requests.get(
        audio_url, headers=headers, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
    ) as r:
        if r.status_code == 416:
            # 本地残留比远端还大（或恰好已完整）时，服务端会拒绝这个 Range
            _, total = _content_range(r.headers.get("Content-Range"))
            if offset and total and offset == total:
                log(f"[audio] 临时文件已是完整长度（{offset} 字节），直接落盘")
                tmp.rename(dest)
                return
            tmp.unlink(missing_ok=True)  # 脏数据留着只会一直 416，清掉重下
            raise DownloadIncompleteError(
                f"服务端拒绝断点续传（HTTP 416）：本地临时文件 {offset} 字节不可用，已清空重下"
            )

        r.raise_for_status()

        if offset and r.status_code == 206:
            start, range_total = _content_range(r.headers.get("Content-Range"))
            if start is not None and start != offset:
                # 服务端给的区间起点和我们请求的对不上：这段数据接到断点后面就是错的，
                # 直接清空临时文件重来（下一次尝试 offset=0，不再带 Range）
                tmp.unlink(missing_ok=True)
                raise DownloadIncompleteError(
                    f"服务端返回的续传起点是 {start}（本地已有 {offset} 字节），已清空临时文件重下"
                )
            mode, done = "ab", offset  # 追加在断点之后
        else:
            if offset:
                # 服务端不支持 Range（返回 200 = 整个文件）：必须清空，否则新旧两段会拼成坏文件
                log(f"[audio] 服务端不支持断点续传（HTTP {r.status_code}），已清空本地临时文件从头下载")
            mode, done, range_total = "wb", 0, None

        remain = _int_header(r.headers.get("Content-Length"))  # 本次响应还会给多少字节
        total = range_total or (done + remain if remain is not None else 0)

        last_pct = -100.0
        last_progress_bytes = 0
        got = 0
        with open(tmp, mode) as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                done += len(chunk)
                if progress:
                    pct = done / total * 100 if total else None
                    if done - last_progress_bytes >= (1 << 20) or (pct is not None and pct - last_pct >= 2):
                        last_pct = pct
                        last_progress_bytes = done
                        progress("download", {
                            "pct": round(pct, 1) if pct is not None else None,
                            "downloaded": done,
                            "total": total or None,
                        })

    # 长度校验：Content-Length 与实际不符（被中途掐断 / 代理截断）按失败处理，交给外层重试
    if remain is not None and got != remain:
        raise DownloadIncompleteError(f"下载不完整：Content-Length={remain}，实际收到 {got} 字节")
    size = tmp.stat().st_size
    if total and size != total:
        raise DownloadIncompleteError(f"下载不完整：期望 {total} 字节，实际只有 {size} 字节")

    tmp.rename(dest)


def _download_url(audio_url: str, workdir: Path, progress=None, log=None) -> Path:
    """下载音频直链，带断点续传 + 失败退避重试，返回最终文件路径。

    重试策略：最多 PA_DOWNLOAD_RETRIES 次尝试（默认 3），两次尝试之间按 PA_DOWNLOAD_BACKOFF
    （默认等 2s、5s）退避；只对网络抖动类错误重试（见 _is_retryable），404 这类确定性错误
    直接抛出。中途失败时 audio.mp3.part 会留在原地，下次运行自动从断点继续。
    """
    logger = log or print
    suffix = Path(audio_url.split("?")[0]).suffix or ".mp3"
    dest = workdir / f"audio{suffix}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    attempts = download_retries()
    waits = download_backoff()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            _download_once(audio_url, dest, tmp, progress=progress, log=logger)
            return dest
        except Exception as exc:
            if not _is_retryable(exc):
                raise  # 确定性错误：再试也不会有别的结果，直接让调用方看到真实原因
            last_error = exc
            if attempt >= attempts:
                break
            wait = _backoff_seconds(attempt, waits)
            reason = f"{type(exc).__name__}: {exc}"
            logger(f"[audio] 第 {attempt} 次重试（共 {attempts} 次尝试）：{reason}；{wait:g} 秒后重试")
            if progress:
                # 字段名是前端/CLI 的约定，不要改：total_retries 表示总尝试次数
                progress("download", {
                    "retry": attempt,
                    "total_retries": attempts,
                    "error": reason,
                })
            time.sleep(wait)

    raise RuntimeError(f"音频下载失败（已尝试 {attempts} 次）：{last_error}") from last_error


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data, *, check_workspace_cache=None) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=1)
    if check_workspace_cache:
        guard = check_workspace_cache(replacing=path, projected_bytes=len(content.encode("utf-8")))
    else:
        guard = None
    if guard is not None and hasattr(guard, "__enter__"):
        with guard:
            path.write_text(content, encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")


def Episode_from_dict(d: dict):
    from .sources.base import Episode

    return Episode.from_dict(d)
