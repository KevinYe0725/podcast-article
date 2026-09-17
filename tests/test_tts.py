"""朗读（把文章读出来）：文本清洗、分块、缓存键与整套接口。

macOS 的 `say` 是唯一一个**不需要密钥、不花钱、离线**的后端，所以它同时是测试里的
真实后端：本机跑 pytest 时会把真的音频生成出来（用一小段文章，几百毫秒），
Linux CI 上没有 `say`，就自动退回「错误路径」的断言（后端要给出可读的报错，而不是崩）。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from podcast_article import settings as settings_mod
from podcast_article import tts

HAS_SAY = bool(shutil.which("say"))
DIR = "20240101-测试台-测试单集"


@pytest.fixture()
def tts_macos(tmp_output, monkeypatch):
    """把朗读后端设成 macOS 本地（不碰用户真实 settings.json）。"""
    settings_mod.save(tts={"provider": "macos", "voice": "", "speed": 1.0, "chunk_chars": 400})
    return tmp_output


# ------------------------------------------------------------ 文本清洗

def test_prepare_text_strips_markdown():
    md = """# 标题

> 引用一句 [带链接的话](https://example.com/x)。

正文里有 **加粗**、*斜体*、`行内代码`，还有一个时间戳 [00:10:07]。

| 表头 | 值 |
| --- | --- |
| a | b |

```python
print("代码块不念")
```
"""
    text = tts.prepare_text(md)
    assert "标题" in text and "带链接的话" in text
    assert "加粗" in text and "斜体" in text and "行内代码" in text
    for junk in ("#", "**", "*", "`", "[", "]", "|", "https://", "print"):
        assert junk not in text, f"{junk!r} 应该被清掉：{text}"
    assert "00:10:07" not in text, "时间戳不用念出来"


def test_prepare_text_handles_unpaired_markers():
    """模型偶尔写出配不成对的 `**`（实测有），不能留在念稿里。"""
    text = tts.prepare_text("第一段。\n**四、只有开头没有结尾的加粗\n第二段。")
    assert "**" not in text and "四、只有开头没有结尾的加粗" in text


def test_prepare_text_keeps_underscores():
    """单个下划线不处理：不然 foo_bar 这类词会被误伤。"""
    assert "foo_bar" in tts.prepare_text("变量名是 foo_bar，别念错。")


# -------------------------------------------------------------- 分块

def test_chunk_text_respects_limit_and_keeps_order():
    text = "\n".join(f"这是第 {i} 段。" + "内容" * 30 for i in range(1, 8))
    chunks = tts.chunk_text(text, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks), [len(c) for c in chunks]
    assert "这是第 1 段" in chunks[0] and "这是第 7 段" in chunks[-1]


def test_chunk_text_splits_long_paragraph_by_sentence():
    long_para = "第一句话。" * 60          # 单段远超上限
    chunks = tts.chunk_text(long_para, 200)
    assert len(chunks) > 1 and all(len(c) <= 200 for c in chunks)
    assert all(c.endswith("。") for c in chunks[:-1]), "应按句号切，不该把句子劈开"


def test_chunk_text_handles_one_giant_sentence():
    """整段就一句话、且超过上限（少见）时按逗号硬切，不能超限也不能丢字。"""
    text = "、".join("很长的一段话" * 20 for _ in range(6))
    chunks = tts.chunk_text(text, 200)
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks).replace("\n", "").count("很长") == text.count("很长")


def test_chunk_text_empty_input():
    assert tts.chunk_text("", 100) == []
    assert tts.chunk_text("   \n  ", 100) == []


# ------------------------------------------------------------ 缓存键

def test_text_sha_changes_with_voice_and_text():
    cfg = {"provider": "macos", "voice": "Tingting", "speed": 1.0}
    base = tts.text_sha("正文", cfg)
    assert tts.text_sha("正文", cfg) == base, "同样的输入必须得到同样的指纹"
    assert tts.text_sha("正文。", cfg) != base
    assert tts.text_sha("正文", {**cfg, "voice": "Sinji"}) != base, "换音色要重读"
    assert tts.text_sha("正文", {**cfg, "speed": 1.5}) != base, "换语速要重读"


def test_ext_for():
    assert tts.ext_for({"provider": "macos"}) == "m4a"
    assert tts.ext_for({"provider": "openai", "format": "mp3"}) == "mp3"
    assert tts.ext_for({"provider": "openai", "format": "wav"}) == "wav"


# ------------------------------------------------------------ 接口

def test_status_when_idle(client, tmp_output, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tmp_output)
    d = client.get(f"/api/tts/{DIR}/status").get_json()
    assert d["state"] == "idle" and d["chunks"] == []
    assert d["enabled"] is False, "默认没启用，前端据此提示去设置页"


def test_start_requires_provider(client, tmp_output, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tmp_output)
    settings_mod.save(tts={"provider": "off"})
    r = client.post(f"/api/tts/{DIR}", json={})
    assert r.status_code == 400
    assert "朗读" in r.get_json()["error"] and "设置" in r.get_json()["error"]


def test_unknown_dir_404(client, tmp_output, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tmp_output)
    assert client.get("/api/tts/不存在的目录/status").status_code == 404
    assert client.post("/api/tts/不存在的目录", json={}).status_code == 404


@pytest.mark.skipif(not HAS_SAY, reason="需要 macOS 的 say 命令")
def test_real_generation_end_to_end(client, tts_macos, monkeypatch):
    """真跑一遍：用系统 say 把一小段文章读出来，然后按块取音频。"""
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tts_macos)

    r = client.post(f"/api/tts/{DIR}", json={})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["state"] == "running"

    deadline = time.time() + 60
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/tts/{DIR}/status").get_json()
        if status["state"] in ("ready", "error"):
            break
        time.sleep(0.4)
    assert status["state"] == "ready", f"生成没成功：{status}"
    assert status["total"] >= 1 and status["fresh"] is True
    assert status["provider"] == "macos" and status["chars"] > 0

    # 音频真的能取到；有 ffmpeg 时整篇拼好（status.merged 非空），没有就按块取
    audio_url = status["merged"] or status["chunks"][0]["url"]
    resp = client.get(audio_url)
    assert resp.status_code == 200, audio_url
    data = resp.data
    assert len(data) > 2000, f"音频太小了：{len(data)} 字节"
    assert data[4:8] == b"ftyp" or data[:2] == b"\xff\xfb" or data[:3] == b"ID3", "看起来不是音频文件"
    if status["merged"]:
        # 整篇文件支持 Range（前端据此刻拖进度条）
        rng = client.get(audio_url, headers={"Range": "bytes=0-1023"})
        assert rng.status_code == 206 and len(rng.data) == 1024

    # 内容没变 → 再来一次是复用（不再调用 TTS，也就不再花钱）
    again = client.post(f"/api/tts/{DIR}", json={}).get_json()
    deadline = time.time() + 30
    while time.time() < deadline:
        st2 = client.get(f"/api/tts/{DIR}/status").get_json()
        if st2["state"] in ("ready", "error"):
            break
        time.sleep(0.3)
    assert st2["state"] == "ready"
    assert again["state"] in ("running", "ready")


def test_delete_removes_files(client, tts_macos, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tts_macos)
    d = tts.tts_dir(tts_macos / DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / "000.m4a").write_bytes(b"x" * 100)
    assert client.delete(f"/api/tts/{DIR}").status_code == 200
    assert not d.exists()


@pytest.mark.skipif(HAS_SAY, reason="只在没有 say 的机器上验证报错可读（CI 是 Linux）")
def test_macos_provider_reports_missing_say(client, tts_macos, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tts_macos)
    client.post(f"/api/tts/{DIR}", json={})
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get(f"/api/tts/{DIR}/status").get_json()
        if st["state"] == "error":
            break
        time.sleep(0.3)
    assert st["state"] == "error"
    assert "say" in st["error"], f"报错要说清是缺 say：{st['error']}"


# ------------------------------------------------------- 只读镜像要拦住

def test_readonly_blocks_tts(client, tmp_output, monkeypatch):
    import webapp
    monkeypatch.setattr(webapp, "OUTPUT_ROOT", tmp_output)
    monkeypatch.setattr(webapp, "READONLY", True)
    assert client.post(f"/api/tts/{DIR}", json={}).status_code == 503
    assert client.delete(f"/api/tts/{DIR}").status_code == 503
    assert client.post("/api/tts/preview", json={}).status_code == 503
    # 读状态、取音频这些是只读操作，不能被拦（否则镜像上连听都不行）
    assert client.get(f"/api/tts/{DIR}/status").status_code == 200


def test_settings_carries_tts_section(client):
    d = client.get("/api/settings").get_json()
    assert "tts" in d and d["tts"]["provider"] == "off"
    assert "tts_voices" in d, "设置页要能给出 macOS 可用音色"


# ---------------------------------------------------- OpenAI 兼容后端（用户最可能用这条）

class _FakeTtsServer:
    """最小的假 TTS 服务：按 OpenAI /v1/audio/speech 的契约回一段假音频。

    为什么值得这么测：用户最可能填的就是「OpenAI 兼容接口」，而这条路之前只测了
    macOS 本地后端 —— 请求路径、鉴权头、payload 字段、把响应字节落盘，任何一处错了
    都会让用户对着一个看不懂的 400/404 发愁。
    """

    def __init__(self):
        import http.server
        import threading

        self.requests: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                      # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization") or "",
                    "ctype": self.headers.get("Content-Type") or "",
                    "body": json.loads(body) if body else {},
                })
                payload = b"ID3\x03\x00\x00\x00" + b"\x00" * 500      # 看起来像 mp3 的假数据
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):            # 别把测试日志刷满
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def fake_tts():
    srv = _FakeTtsServer()
    yield srv
    srv.close()


def test_openai_compatible_request_shape(fake_tts, tmp_path):
    """请求要打对路径、带 Bearer 鉴权、payload 字段符合 OpenAI 契约。"""
    out = tmp_path / "000.mp3"
    cfg = {"provider": "openai", "base_url": fake_tts.url, "model": "cosyvoice",
           "voice": "FunAudioLLM/CosyVoice2-0.5B:alex", "speed": 1.25, "format": "mp3"}
    tts.synthesize_chunk("你好，这是正文。", cfg, "sk-test-key", out)

    assert out.exists() and out.stat().st_size > 500, "响应字节要落盘"
    req = fake_tts.requests[0]
    assert req["path"] == "/v1/audio/speech", f"路径不对：{req['path']}"
    assert req["auth"] == "Bearer sk-test-key", "鉴权头不对"
    assert req["ctype"].startswith("application/json")
    assert req["body"]["input"] == "你好，这是正文。"
    assert req["body"]["model"] == "cosyvoice"
    assert req["body"]["voice"] == "FunAudioLLM/CosyVoice2-0.5B:alex"
    assert req["body"]["response_format"] == "mp3"
    assert req["body"]["speed"] == 1.25, "语速要透传给接口"


def test_openai_base_url_normalisation(fake_tts, tmp_path):
    """base_url 填到 /v1 为止、或者只填域名，都要能拼对。"""
    for raw in (fake_tts.url, f"{fake_tts.url}/", f"{fake_tts.url}/v1", f"{fake_tts.url}/v1/"):
        fake_tts.requests.clear()
        tts.synthesize_chunk("测", {"provider": "openai", "base_url": raw}, "k", tmp_path / "x.mp3")
        assert fake_tts.requests[-1]["path"] == "/v1/audio/speech", f"{raw} 拼错了路径"


def test_openai_error_is_readable():
    """key 不对 / 路径不对时，报错要说清是接口返回了什么，而不是一个光秃秃的异常。"""
    cfg = {"provider": "openai", "base_url": "http://127.0.0.1:1", "model": "m", "voice": "v"}
    with pytest.raises(Exception) as exc:
        tts.synthesize_chunk("测", cfg, "k", Path("/tmp/should-not-exist.mp3"))
    assert "语音接口" in str(exc.value) or "Connection" in str(exc.value)


def test_openai_requires_key(tmp_path):
    cfg = {"provider": "openai", "base_url": "http://127.0.0.1:1", "model": "m", "voice": "v"}
    with pytest.raises(RuntimeError) as exc:
        tts.synthesize_chunk("测", cfg, "", tmp_path / "y.mp3")
    assert "API key" in str(exc.value) or "密钥" in str(exc.value)


def test_full_openai_run_writes_index_and_merges(fake_tts, tmp_path):
    """整篇生成：多块 → index.json 落盘 → 块文件都在（拼接看本机有没有 ffmpeg）。"""
    workdir = tmp_path / "ep"
    workdir.mkdir()
    (workdir / "article.md").write_text("# 标题\n\n" + "这是一段正文。" * 200, encoding="utf-8")
    cfg = {"provider": "openai", "base_url": fake_tts.url, "model": "m", "voice": "v",
           "chunk_chars": 300, "format": "mp3"}
    idx = tts.synthesize(workdir, (workdir / "article.md").read_text(encoding="utf-8"), cfg,
                         api_key="k", log=lambda *_a: None)

    assert len(idx["chunks"]) > 1, "这篇应该被切成多块"
    assert len(fake_tts.requests) == len(idx["chunks"]), "每块一次请求，不多不少"
    assert (tts.tts_dir(workdir) / "index.json").exists()
    for c in idx["chunks"]:
        assert (tts.tts_dir(workdir) / c["file"]).exists()
    # 内容没变 → 再来一次不重复请求（省钱的关键）
    fake_tts.requests.clear()
    idx2 = tts.synthesize(workdir, (workdir / "article.md").read_text(encoding="utf-8"), cfg,
                          api_key="k", log=lambda *_a: None)
    assert fake_tts.requests == [], "内容与音色都没变，不该再调接口"
    assert idx2["text_sha"] == idx["text_sha"]
