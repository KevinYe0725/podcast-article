"""音频下载的重试 / 退避 / 断点续传测试：requests 与 yt-dlp 全部打桩，绝不联网。"""
from __future__ import annotations

import ssl
import http.cookiejar
import io
import json
import urllib.request

import requests
import pytest
from yt_dlp.utils import DownloadError

from podcast_article import pipeline
from podcast_article.sources import ytdlp_src
from podcast_article.sources.base import Episode

AUDIO_URL = "https://cdn.example.com/ep1.mp3"
YT_URL = "https://www.youtube.com/watch?v=abcdefghijk"
BILI_URL = "https://www.bilibili.com/video/BV196tK67Ebb/"


# ---------------------------------------------------------------- 打桩工具

class FakeResponse:
    """最小可用的 requests.Response 替身：只实现 pipeline 用到的那些接口。"""

    def __init__(self, chunks, *, status_code: int = 200, headers: dict | None = None,
                 url: str = AUDIO_URL):
        self._chunks = chunks  # 传生成器就能模拟「下到一半被掐断」
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}", response=self)

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        for chunk in self._chunks:
            if chunk:
                yield chunk


def install_get(monkeypatch, script: list) -> list[dict]:
    """把 requests.get 换成按脚本行动的桩：遇到异常项就抛，否则返回该响应。"""
    queue = list(script)
    calls: list[dict] = []

    def fake_get(url, headers=None, stream=False, timeout=None, **kwargs):
        calls.append({
            "url": url,
            "headers": dict(headers or {}),
            "stream": stream,
            "timeout": timeout,
        })
        if not queue:
            raise AssertionError(f"requests.get 被多调了一次（第 {len(calls)} 次）")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(pipeline.requests, "get", fake_get)
    return calls


def fake_ytdlp(monkeypatch, results: list, filename: str = "/tmp/audio.m4a") -> list:
    """把 yt_dlp.YoutubeDL 换成伪实现：results 每项要么是 info dict，要么是要抛的异常。"""
    queue = list(results)
    made: list = []

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts
            self.calls = 0
            self.cookiejar = _FakeCookieJar()
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=False):
            self.calls += 1
            if not queue:
                raise AssertionError(f"extract_info 被多调了一次（第 {self.calls} 次）")
            item = queue.pop(0)
            if isinstance(item, Exception):
                if "HTTP Error 412" in str(item):
                    self.cookiejar.set_cookie(http.cookiejar.Cookie(
                        0, "X-BILI-SEC-TOKEN", "test-challenge", None, False,
                        ".bilibili.com", True, True, "/", True, True, None,
                        True, None, None, {},
                    ))
                raise item
            return item

        def prepare_filename(self, info):
            return filename

    monkeypatch.setattr(ytdlp_src.yt_dlp, "YoutubeDL", _FakeYDL)
    return made


class _FakeCookieJar(http.cookiejar.CookieJar):
    def get_cookie_header(self, url: str) -> str | None:
        request = urllib.request.Request(url)
        self.add_cookie_header(request)
        return request.get_header("Cookie")


def fake_info() -> dict:
    """一份够用的 yt-dlp info dict。"""
    return {
        "title": "测试视频",
        "webpage_url": YT_URL,
        "uploader": "某频道",
        "duration": 120,
        "description": "简介",
        "thumbnail": "https://img.example.com/x.jpg",
        "subtitles": {"zh-Hans": [{"ext": "vtt", "url": "https://example.com/zh.vtt"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example.com/en.vtt"}]},
    }


# ---------------------------------------------------------------- 装置

@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """默认 3 次尝试 + 0 秒退避：让大多数用例与具体秒数无关（也不会真的等待）。"""
    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "3")
    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", "0")


@pytest.fixture()
def slept(monkeypatch) -> list[float]:
    """把两个模块里的 sleep 都换成记录器：测试绝不能真的睡 2 秒、5 秒。"""
    waits: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", waits.append)
    monkeypatch.setattr(ytdlp_src.time, "sleep", waits.append)
    return waits


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "20240101-测试台-测试单集"
    d.mkdir()
    return d


# ---------------------------------------------------------------- 直链下载：重试

def test_connection_error_then_success(monkeypatch, workdir, slept):
    """第一次连接错误、第二次成功：文件正确，日志与 progress 都报了重试。"""
    calls = install_get(monkeypatch, [
        requests.ConnectionError("connection reset by peer"),
        FakeResponse([b"hello"], headers={"Content-Length": "5"}),
    ])
    logs: list[str] = []
    events: list[tuple] = []

    dest = pipeline._download_url(
        AUDIO_URL, workdir, progress=lambda s, d: events.append((s, d)), log=logs.append
    )

    assert dest == workdir / "audio.mp3"
    assert dest.read_bytes() == b"hello"
    assert len(calls) == 2  # 第二次才成功
    assert not (workdir / "audio.mp3.part").exists()  # 下完就改名，不留临时文件

    assert any("第 1 次重试" in line and "ConnectionError" in line for line in logs)
    retries = [d for stage, d in events if stage == "download" and "retry" in d]
    assert len(retries) == 1
    assert set(retries[0]) == {"retry", "total_retries", "error"}  # 字段名不能改
    assert retries[0]["retry"] == 1
    assert retries[0]["total_retries"] == 3
    assert "connection reset" in retries[0]["error"]
    assert slept == [0.0]  # 退避环境变量是 0，但仍走了一次 sleep


def test_is_retryable_classification():
    """分类白名单：网络抖动/5xx/429 重试，其他 4xx 与业务错误不重试。"""
    def http(status: int) -> requests.HTTPError:
        return requests.HTTPError(f"{status} Error", response=FakeResponse([], status_code=status))

    assert pipeline._is_retryable(requests.ConnectionError("x"))
    assert pipeline._is_retryable(requests.Timeout("x"))
    assert pipeline._is_retryable(requests.exceptions.ChunkedEncodingError("x"))
    assert pipeline._is_retryable(TimeoutError("x"))
    assert pipeline._is_retryable(ssl.SSLError("EOF occurred in violation of protocol"))
    assert pipeline._is_retryable(pipeline.DownloadIncompleteError("短了"))
    assert pipeline._is_retryable(http(500))
    assert pipeline._is_retryable(http(503))
    assert pipeline._is_retryable(http(429))
    assert pipeline._is_retryable(requests.HTTPError("HTTP 503"))  # 没有 response 时从消息里取状态码
    assert not pipeline._is_retryable(http(404))
    assert not pipeline._is_retryable(http(403))
    assert not pipeline._is_retryable(ValueError("元信息里既没有音频直链"))


def test_404_is_not_retried(monkeypatch, workdir, slept):
    """4xx 是确定性错误：只请求一次。"""
    calls = install_get(monkeypatch, [FakeResponse([b"nope"], status_code=404)])
    logs: list[str] = []

    with pytest.raises(requests.HTTPError):
        pipeline._download_url(AUDIO_URL, workdir, log=logs.append)

    assert len(calls) == 1
    assert slept == []
    assert not any("重试" in line for line in logs)


def test_5xx_retries_until_exhausted(monkeypatch, workdir, slept):
    """5xx 一直重试到上限，最后抛 RuntimeError 并带上原始错误。"""
    calls = install_get(monkeypatch, [
        FakeResponse([b""], status_code=503),
        FakeResponse([b""], status_code=503),
        FakeResponse([b""], status_code=503),
    ])
    events: list[dict] = []

    with pytest.raises(RuntimeError) as err:
        pipeline._download_url(AUDIO_URL, workdir,
                               progress=lambda s, d: events.append(d), log=lambda *_: None)

    assert len(calls) == 3                      # 默认 3 次尝试
    assert "503" in str(err.value)              # 消息里带最后一条原始错误
    assert "已尝试 3 次" in str(err.value)
    assert [d["retry"] for d in events if "retry" in d] == [1, 2]
    assert len(slept) == 2                      # 只在两次尝试之间等待
    assert not (workdir / "audio.mp3").exists()


def test_content_length_mismatch_triggers_retry(monkeypatch, workdir, slept):
    """Content-Length 与实际收到的字节数不符 → 视为失败并重试。"""
    def short():
        return FakeResponse([b"x" * 10], headers={"Content-Length": "100"})

    calls = install_get(monkeypatch, [short(), short(), short()])

    with pytest.raises(RuntimeError) as err:
        pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert len(calls) == 3
    assert "Content-Length=100" in str(err.value)
    assert len(slept) == 2
    assert not (workdir / "audio.mp3").exists()  # 不完整的数据绝不落到最终文件名


# ---------------------------------------------------------------- 直链下载：断点续传

def test_resume_sends_range_and_appends(monkeypatch, workdir):
    """已有半截 .part：请求带 Range，206 时在断点后追加。"""
    (workdir / "audio.mp3.part").write_bytes(b"AAAA")
    calls = install_get(monkeypatch, [
        FakeResponse([b"BBBBBB"], status_code=206,
                     headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}),
    ])

    dest = pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert calls[0]["headers"]["Range"] == "bytes=4-"
    assert dest.read_bytes() == b"AAAABBBBBB"
    assert not (workdir / "audio.mp3.part").exists()


def test_server_ignoring_range_restarts_from_scratch(monkeypatch, workdir):
    """服务端不支持 Range（回 200）：必须清空临时文件，不能拼成 b'AAAAhello'。"""
    (workdir / "audio.mp3.part").write_bytes(b"AAAA")
    calls = install_get(monkeypatch, [
        FakeResponse([b"hello"], status_code=200, headers={"Content-Length": "5"}),
    ])
    logs: list[str] = []

    dest = pipeline._download_url(AUDIO_URL, workdir, log=logs.append)

    assert calls[0]["headers"]["Range"] == "bytes=4-"  # 先按续传试
    assert dest.read_bytes() == b"hello"               # 结果是第二次的完整响应
    assert any("不支持断点续传" in line for line in logs)


def test_midstream_break_keeps_part_and_retry_resumes(monkeypatch, workdir, slept):
    """下到一半断线：已收到的字节留在 .part 里，下一次尝试接着下。"""
    def broken():
        yield b"AAAA"
        raise requests.exceptions.ChunkedEncodingError("Connection broken: invalid chunked encoding")

    calls = install_get(monkeypatch, [
        FakeResponse(broken()),
        FakeResponse([b"BBBBBB"], status_code=206,
                     headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}),
    ])

    dest = pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert dest.read_bytes() == b"AAAABBBBBB"          # 半截 + 新增，拼对了
    assert "Range" not in calls[0]["headers"]          # 第一次是全新下载
    assert calls[1]["headers"]["Range"] == "bytes=4-"  # 第二次从断点继续
    assert len(slept) == 1


def test_416_with_complete_part_finishes(monkeypatch, workdir):
    """临时文件恰好已完整：服务端回 416，直接落盘，不重下。"""
    (workdir / "audio.mp3.part").write_bytes(b"0123456789")
    calls = install_get(monkeypatch, [
        FakeResponse([], status_code=416, headers={"Content-Range": "bytes */10"}),
    ])
    logs: list[str] = []

    dest = pipeline._download_url(AUDIO_URL, workdir, log=logs.append)

    assert dest.read_bytes() == b"0123456789"
    assert len(calls) == 1
    assert any("完整长度" in line for line in logs)


def test_416_with_oversized_part_restarts(monkeypatch, workdir, slept):
    """本地残留比远端还大：清掉脏临时文件，下次尝试从头下（不再带 Range）。"""
    (workdir / "audio.mp3.part").write_bytes(b"x" * 20)
    calls = install_get(monkeypatch, [
        FakeResponse([], status_code=416, headers={"Content-Range": "bytes */10"}),
        FakeResponse([b"0123456789"], status_code=200, headers={"Content-Length": "10"}),
    ])

    dest = pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert calls[0]["headers"]["Range"] == "bytes=20-"
    assert "Range" not in calls[1]["headers"]
    assert dest.read_bytes() == b"0123456789"
    assert len(slept) == 1


def test_mismatched_content_range_start_restarts(monkeypatch, workdir, slept):
    """服务端给的续传起点和本地断点不一致：清空重下，绝不把错位的数据接上去。"""
    (workdir / "audio.mp3.part").write_bytes(b"AAAA")
    calls = install_get(monkeypatch, [
        # 我们要的是 bytes=4-，服务端却从第 10 字节开始给
        FakeResponse([b"YYYYYYYYYY"], status_code=206,
                     headers={"Content-Length": "10", "Content-Range": "bytes 10-19/20"}),
        FakeResponse([b"complete!"], status_code=200, headers={"Content-Length": "9"}),
    ])

    dest = pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert calls[0]["headers"]["Range"] == "bytes=4-"
    assert "Range" not in calls[1]["headers"]  # 错位数据已丢弃，第二次是全新请求
    assert dest.read_bytes() == b"complete!"
    assert len(slept) == 1


# ---------------------------------------------------------------- 直链下载：配置

def test_timeouts_are_split_connect_and_read(monkeypatch, workdir):
    """连接超时 15s、读超时 60s，不是写死的单个 60s。"""
    calls = install_get(monkeypatch, [FakeResponse([b"hi"], headers={"Content-Length": "2"})])

    pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert calls[0]["timeout"] == (15.0, 60.0)
    assert calls[0]["stream"] is True
    assert calls[0]["headers"]["User-Agent"] == pipeline._UA


def test_backoff_env_controls_waits(monkeypatch, workdir, slept):
    """PA_DOWNLOAD_BACKOFF 生效；sleep 已被替换，所以测试不会真的等。"""
    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "3")
    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", "0.5,1")
    install_get(monkeypatch, [requests.ConnectionError("x")] * 3)

    with pytest.raises(RuntimeError):
        pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert slept == [0.5, 1.0]


def test_backoff_repeats_last_value_when_shorter(monkeypatch, workdir, slept):
    """退避序列不够长时重复最后一个值：4 次尝试 + 单个 "7" → 7,7,7。"""
    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "4")
    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", "7")
    install_get(monkeypatch, [requests.ConnectionError("x")] * 4)

    with pytest.raises(RuntimeError):
        pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert slept == [7.0, 7.0, 7.0]


def test_retries_env_controls_attempt_count(monkeypatch, workdir):
    """PA_DOWNLOAD_RETRIES 改变总尝试次数。"""
    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "5")
    calls = install_get(monkeypatch, [requests.ConnectionError("x")] * 5)

    with pytest.raises(RuntimeError) as err:
        pipeline._download_url(AUDIO_URL, workdir, log=lambda *_: None)

    assert len(calls) == 5
    assert "已尝试 5 次" in str(err.value)


def test_env_defaults_and_bad_values(monkeypatch):
    """默认值 3 / "2,5"，非法值一律回退默认。"""
    monkeypatch.delenv("PA_DOWNLOAD_RETRIES", raising=False)
    monkeypatch.delenv("PA_DOWNLOAD_BACKOFF", raising=False)
    assert pipeline.download_retries() == 3
    assert pipeline.download_backoff() == [2.0, 5.0]

    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", "1,,3")
    assert pipeline.download_backoff() == [1.0, 3.0]
    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", " 0 , 2.5 ")
    assert pipeline.download_backoff() == [0.0, 2.5]
    monkeypatch.setenv("PA_DOWNLOAD_BACKOFF", "abc")
    assert pipeline.download_backoff() == [2.0, 5.0]

    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "0")
    assert pipeline.download_retries() == 3
    monkeypatch.setenv("PA_DOWNLOAD_RETRIES", "abc")
    assert pipeline.download_retries() == 3


def test_backoff_seconds_picks_and_repeats():
    assert pipeline._backoff_seconds(1, [2.0, 5.0]) == 2.0
    assert pipeline._backoff_seconds(2, [2.0, 5.0]) == 5.0
    assert pipeline._backoff_seconds(3, [2.0, 5.0]) == 5.0
    assert pipeline._backoff_seconds(9, [2.0, 5.0]) == 5.0
    assert pipeline._backoff_seconds(1, []) == 0.0


# ---------------------------------------------------------------- 阶段串接

def test_stage_audio_resumes_and_reuses(monkeypatch, workdir):
    """_stage_audio：日志说明会续传，下载完给出最终文件；已存在音频时直接复用。"""
    (workdir / "audio.mp3.part").write_bytes(b"AAAA")
    install_get(monkeypatch, [
        FakeResponse([b"BBBBBB"], status_code=206,
                     headers={"Content-Length": "6", "Content-Range": "bytes 4-9/10"}),
    ])
    ep = Episode(source="rss", url="https://example.com/ep1", title="测试单集",
                 audio_url=AUDIO_URL)
    logs: list[str] = []
    pipe = pipeline.Pipeline(ep.url, workdir, log=logs.append)

    path = pipe._stage_audio(ep, workdir)

    assert path.name == "audio.mp3"
    assert path.read_bytes() == b"AAAABBBBBB"
    assert any("断点继续" in line for line in logs)
    assert any("完成：audio.mp3" in line for line in logs)

    # 第二次调用：已有音频，直接复用，不再发请求
    again = pipe._stage_audio(ep, workdir)
    assert again == path


# ---------------------------------------------------------------- yt-dlp

def test_ytdlp_retryable_real_messages():
    """真实错误消息样本：只有网络类才重试。"""
    assert ytdlp_src._retryable(Exception("HTTP Error 503: Service Unavailable")) is True
    assert ytdlp_src._retryable(Exception("ERROR: Unable to download webpage: timed out")) is True
    assert ytdlp_src._retryable(Exception("Video unavailable")) is False
    assert ytdlp_src._retryable(Exception("Private video")) is False


def test_ytdlp_retryable_extra_messages():
    """补充样本：429/5xx 重试，其他 4xx 与确定性提示不重试。"""
    assert ytdlp_src._retryable("HTTP Error 429: Too Many Requests") is True
    assert ytdlp_src._retryable("Temporary failure in name resolution") is True
    assert ytdlp_src._retryable("Connection reset by peer") is True
    assert ytdlp_src._retryable("SSL: UNEXPECTED_EOF_WHILE_READING") is True
    assert ytdlp_src._retryable("Unable to download webpage: Max retries exceeded") is True
    assert ytdlp_src._retryable(TimeoutError()) is True
    assert ytdlp_src._retryable("HTTP Error 404: Not Found") is False
    assert ytdlp_src._retryable("Unable to download webpage: HTTP Error 403: Forbidden") is False
    assert ytdlp_src._retryable("This video is not available in your country") is False
    assert ytdlp_src._retryable("Unsupported URL: https://example.com/x") is False
    assert ytdlp_src._retryable("Sign in to confirm your age") is False


def test_ytdlp_env_defaults(monkeypatch):
    """yt-dlp 这条路的默认退避是 3s / 8s（比直链那条更宽）。"""
    monkeypatch.delenv("PA_DOWNLOAD_RETRIES", raising=False)
    monkeypatch.delenv("PA_DOWNLOAD_BACKOFF", raising=False)
    assert ytdlp_src._retries() == 3
    assert ytdlp_src._waits() == [3.0, 8.0]


def test_probe_retries_network_error_then_succeeds(monkeypatch, slept):
    made = fake_ytdlp(monkeypatch, [
        DownloadError("ERROR: Unable to download webpage: timed out"),
        fake_info(),
    ])
    logs: list[str] = []

    ep = ytdlp_src.probe(YT_URL, log=logs.append)

    assert len(made) == 2                       # 每次尝试都新建一个 YoutubeDL
    assert sum(y.calls for y in made) == 2
    assert ep.title == "测试视频"
    assert ep.subtitle_tracks[0].lang == "zh-Hans"
    assert any("第 1 次重试" in line and "timed out" in line for line in logs)
    assert len(slept) == 1


def test_probe_does_not_retry_deterministic_error(monkeypatch, slept):
    """视频不存在这类错误只调一次，不浪费 8 秒。"""
    made = fake_ytdlp(monkeypatch, [DownloadError("ERROR: Video unavailable")])
    logs: list[str] = []

    with pytest.raises(DownloadError):
        ytdlp_src.probe(YT_URL, log=logs.append)

    assert made[0].calls == 1
    assert slept == []
    assert any("不重试" in line for line in logs)


def test_probe_opts_carry_retry_settings(monkeypatch, slept):
    made = fake_ytdlp(monkeypatch, [fake_info()])

    ytdlp_src.probe(YT_URL, log=lambda *_: None)

    opts = made[0].opts
    assert opts["retries"] >= 3
    assert opts["extractor_retries"] >= 3
    assert opts["socket_timeout"] > 0
    assert opts["skip_download"] is True


def test_bilibili_probe_refreshes_anonymous_fingerprint_once_after_page_412(monkeypatch):
    made = fake_ytdlp(monkeypatch, [
        DownloadError("ERROR: [BiliBili] BV196tK67Ebb: Unable to download webpage: HTTP Error 412: Precondition Failed"),
        fake_info(),
    ])
    finger_requests = []
    payload = {"code": 0, "data": {"b_3": "test-buvid3", "b_4": "test-buvid4"}}

    def fake_urlopen(request, timeout):
        finger_requests.append((request, timeout))
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    episode = ytdlp_src.probe(BILI_URL, log=lambda *_: None)

    assert episode.source == "bilibili"
    assert len(made) == 1 and made[0].calls == 2
    request, timeout = finger_requests[0]
    assert request.full_url == "https://api.bilibili.com/x/frontend/finger/spi"
    assert request.get_header("Referer") == "https://www.bilibili.com/"
    assert request.get_header("User-agent") == ytdlp_src.std_headers["User-Agent"]
    finger_cookies = dict(pair.strip().split("=", 1) for pair in request.get_header("Cookie").split(";"))
    assert finger_cookies["X-BILI-SEC-TOKEN"] == "test-challenge"
    assert len(finger_cookies["buvid_fp"]) == 32
    assert "SESSDATA" not in finger_cookies
    assert timeout > 0
    cookies = {cookie.name: cookie.value for cookie in made[0].cookiejar}
    assert cookies["buvid3"] == "test-buvid3"
    assert cookies["buvid4"] == "test-buvid4"
    assert len(cookies["buvid_fp"]) == 32
    assert made[0].opts["http_headers"]["Referer"] == "https://www.bilibili.com/"


def test_download_audio_opts_and_retry(monkeypatch, slept):
    """外层重试兜住 5xx；opts 里有断点续传相关配置。"""
    made = fake_ytdlp(monkeypatch, [
        DownloadError("ERROR: unable to download video data: HTTP Error 503: Service Unavailable"),
        fake_info(),
    ])
    events: list[tuple] = []
    logs: list[str] = []

    path = ytdlp_src.download_audio(
        YT_URL, "/tmp/audio", progress=lambda s, d: events.append((s, d)), log=logs.append
    )

    assert path == "/tmp/audio.m4a"
    assert len(made) == 1
    assert made[0].calls == 2
    assert len(slept) == 1

    opts = made[0].opts
    assert opts["retries"] >= 1
    assert opts["fragment_retries"] >= 1
    assert opts["socket_timeout"] > 0
    assert opts["continuedl"] is True   # 重试接着已下载的部分下
    assert opts["nopart"] is False      # 保留 .part 才能跨进程续传

    retries = [d for stage, d in events if "retry" in d]
    assert retries[0]["retry"] == 1
    assert retries[0]["total_retries"] == 3
    assert "503" in retries[0]["error"]
    assert any("第 1 次重试" in line for line in logs)


def test_download_audio_exhausts_retries(monkeypatch, slept):
    made = fake_ytdlp(monkeypatch, [DownloadError("ERROR: HTTP Error 503: x")] * 3)

    with pytest.raises(RuntimeError) as err:
        ytdlp_src.download_audio(YT_URL, "/tmp/audio", log=lambda *_: None)

    assert made[0].calls == 3
    assert "503" in str(err.value)
    assert "已尝试 3 次" in str(err.value)
    assert len(slept) == 2


def test_download_audio_progress_hook(monkeypatch, slept):
    made = fake_ytdlp(monkeypatch, [fake_info()])
    events: list[tuple] = []

    ytdlp_src.download_audio(YT_URL, "/tmp/audio",
                             progress=lambda s, d: events.append((s, d)), log=lambda *_: None)

    hook = made[0].opts["progress_hooks"][0]
    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
    hook({"status": "finished", "downloaded_bytes": 100, "total_bytes": 100})

    assert events == [("download", {"pct": 50.0, "downloaded": 50, "total": 100})]
