"""朗读（把文章读出来）：文本清洗、分块、缓存键与整套接口。

macOS 的 `say` 是唯一一个**不需要密钥、不花钱、离线**的后端，所以它同时是测试里的
真实后端：本机跑 pytest 时会把真的音频生成出来（用一小段文章，几百毫秒），
Linux CI 上没有 `say`，就自动退回「错误路径」的断言（后端要给出可读的报错，而不是崩）。
"""
from __future__ import annotations

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
