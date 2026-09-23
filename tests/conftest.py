"""pytest 公共装置：所有测试都跑在临时目录里，绝不触碰用户的真实数据。

这个隔离是**默认全局**的，不是各测试自己记得加：曾经有测试只把 `ENV_PATH` 打了桩、
忘了 `SETTINGS_PATH`，于是 `POST /api/settings` 直接把 `subscriptions` 段写进了用户
真实的 settings.json。所以凡是「会落盘的个人数据」，都在这里一次性指到 tmp_path：

    output/           → tmp_path/output          （PA_OUTPUT_DIR）
    library.json      → 分类 + 阅读状态
    settings.json     → 个人资料 / 生成默认值 / 订阅调度
    .env              → 密钥
    queue.json        → 批量队列
    feeds.json        → 订阅
    mcp_servers.json  → MCP 服务器配置（可能含密钥）

模块级常量是在 import 时读环境变量的，所以这里**必须 monkeypatch 模块属性**
（而不是只设环境变量）才能真正生效。
"""
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
    """把输出根目录与所有个人数据文件都指向临时目录。"""
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setenv("PA_OUTPUT_DIR", str(out))

    from podcast_article import feeds, kb, library, mcp_config, queue
    from podcast_article import settings as st

    monkeypatch.setattr(library, "STORE_PATH", tmp_path / "library.json")
    monkeypatch.setattr(st, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(st, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(queue, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(feeds, "FEEDS_PATH", tmp_path / "feeds.json")
    monkeypatch.setattr(kb, "DB_PATH", tmp_path / "kb.sqlite")
    if hasattr(mcp_config, "SERVERS_PATH"):
        monkeypatch.setattr(mcp_config, "SERVERS_PATH", tmp_path / "mcp_servers.json")
    return out


@pytest.fixture()
def platform_store(tmp_path):
    from podcast_article.platform_store import PlatformStore

    return PlatformStore(tmp_path / "platform.sqlite")


@pytest.fixture()
def platform_admin(platform_store):
    return platform_store.bootstrap_admin("admin", "$argon2id$test-admin", now=1_000)


@pytest.fixture()
def platform_password_hash():
    return "$argon2id$test-member"


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
        # 区间形态（引用跨了十几秒时模型就这么写），必须和单个时间点一样可点
        "> 这是跨了十几秒的一段原话 [00:20:00-00:20:15]\n\n"
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
