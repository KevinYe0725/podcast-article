"""pytest 公共装置：所有测试都跑在临时目录里，绝不触碰真实的 output/ 与 library.json。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_output(tmp_path, monkeypatch):
    """把输出根目录指向临时目录，并让 library.json 也落在临时目录。"""
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setenv("PA_OUTPUT_DIR", str(out))

    from podcast_article import library

    monkeypatch.setattr(library, "STORE_PATH", tmp_path / "library.json")
    return out


@pytest.fixture()
def episode(tmp_output):
    """造一集假记录（文章 + 音频 + 文字稿 + meta）。"""
    d = tmp_output / "20240101-测试台-测试单集"
    d.mkdir()
    (d / "meta.json").write_text(
        json.dumps({"title": "测试单集", "podcast": "测试台", "author": "某人",
                    "url": "https://example.com/ep1", "pub_date": "2024-01-01T00:00:00Z",
                    "duration": 3661}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "article.md").write_text(
        "# 测试标题\n\n> 一句话引语\n\n"
        "## 第一节\n\n1906 年 4 月 18 日，旧金山发生里氏 7.9 级地震。\n\n"
        "> 这是原话 [00:10:07]\n\n"
        "## 读完你会带走什么\n\n- 一条可检验的判断\n- 一条可试的做法\n",
        encoding="utf-8",
    )
    (d / "transcript.txt").write_text("[00:00:01] 开头\n[00:10:07] 这是原话\n", encoding="utf-8")
    (d / "transcript.json").write_text("[]", encoding="utf-8")
    (d / "audio.m4a").write_bytes(b"\x00" * 4096)
    return d


@pytest.fixture()
def client(tmp_output, episode, monkeypatch):
    """指向临时输出目录的 Flask 测试客户端。"""
    import importlib

    import webapp

    importlib.reload(webapp)          # 让 OUTPUT_ROOT 读取新的 PA_OUTPUT_DIR
    webapp.app.config.update(TESTING=True)
    with webapp.app.test_client() as c:
        yield c
