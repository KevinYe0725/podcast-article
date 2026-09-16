"""单集封面图（cover.py）与小宇宙封面字段名的回归。

全部打桩、零联网；落盘只在 tmp_path。
"""
from __future__ import annotations

import pytest

from podcast_article import cover
from podcast_article.sources import xyz

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)          # 够长、有 PNG 魔数
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class FakeResp:
    def __init__(self, content: bytes = PNG, ctype: str = "image/png", status: int = 200):
        self.content = content
        self.status_code = status
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cover.requests.HTTPError(f"{self.status_code}")


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "20240101-测试台-测试单集"
    d.mkdir()
    return d


def stub_get(monkeypatch, resp=None, calls=None):
    def fake(url, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if isinstance(resp, Exception):
            raise resp
        return resp if resp is not None else FakeResp()

    monkeypatch.setattr(cover.requests, "get", fake)


# ------------------------------------------------------------------ find / mime


def test_find_returns_none_when_absent(workdir):
    assert cover.find(workdir) is None


def test_find_picks_existing_cover(workdir):
    (workdir / "cover.jpg").write_bytes(JPEG)
    assert cover.find(workdir).name == "cover.jpg"


def test_find_ignores_empty_file(workdir):
    (workdir / "cover.jpg").write_bytes(b"")
    assert cover.find(workdir) is None, "0 字节的封面等于没有（半截文件）"


def test_mimetype_by_extension(workdir):
    assert cover.mimetype(workdir / "cover.jpg") == "image/jpeg"
    assert cover.mimetype(workdir / "cover.png") == "image/png"
    assert cover.mimetype(workdir / "cover.webp") == "image/webp"
    assert cover.mimetype(workdir / "cover.unknown") == "application/octet-stream"


# ------------------------------------------------------------------ fetch


def test_fetch_saves_with_extension_from_content_type(workdir, monkeypatch):
    calls = []
    stub_get(monkeypatch, FakeResp(PNG, "image/png"), calls)
    path = cover.fetch("https://example.com/a.jpg", workdir)

    assert path == workdir / "cover.png", f"扩展名要跟 Content-Type（URL 写的 .jpg 不算）：{path}"
    assert path.read_bytes() == PNG
    assert calls[0]["headers"].get("User-Agent"), "要带 UA，很多图床没有就拒绝"
    assert not list(workdir.glob("*.part")), "原子写不该留下临时文件"


def test_fetch_uses_url_suffix_when_no_content_type(workdir, monkeypatch):
    stub_get(monkeypatch, FakeResp(JPEG, "application/octet-stream"))
    assert cover.fetch("https://example.com/a.jpeg", workdir).suffix == ".jpeg" or \
        cover.find(workdir).name in ("cover.jpeg", "cover.jpg")


def test_fetch_falls_back_to_jpg(workdir, monkeypatch):
    stub_get(monkeypatch, FakeResp(JPEG, "application/octet-stream"))
    path = cover.fetch("https://example.com/nosuffix", workdir)
    assert path.suffix == ".jpg", f"认不出来时按 jpg：{path}"


def test_fetch_replaces_stale_cover_with_other_extension(workdir, monkeypatch):
    """上次存的是 jpg，这次图床给 webp —— 旧的必须清掉，否则 find() 会拿到旧图。"""
    (workdir / "cover.jpg").write_bytes(JPEG)
    stub_get(monkeypatch, FakeResp(PNG, "image/webp"))
    cover.fetch("https://example.com/a", workdir)
    assert not (workdir / "cover.jpg").exists(), "换了扩展名就该删掉旧文件"
    assert (workdir / "cover.webp").exists()


def test_fetch_rejects_non_http(workdir, monkeypatch):
    stub_get(monkeypatch)
    assert cover.fetch("", workdir) is None
    assert cover.fetch("ftp://example.com/a.png", workdir) is None
    assert cover.find(workdir) is None


def test_fetch_returns_none_on_network_error(workdir, monkeypatch):
    stub_get(monkeypatch, cover.requests.ConnectionError("boom"))
    assert cover.fetch("https://example.com/a.png", workdir) is None, "拿不到封面绝不是致命错误"
    assert cover.find(workdir) is None


def test_fetch_returns_none_on_http_error(workdir, monkeypatch):
    stub_get(monkeypatch, FakeResp(PNG, "image/png", status=403))
    assert cover.fetch("https://example.com/a.png", workdir) is None
    assert cover.find(workdir) is None


def test_fetch_rejects_oversized(workdir, monkeypatch):
    big = PNG + b"\x00" * (cover.MAX_BYTES + 1)
    stub_get(monkeypatch, FakeResp(big, "image/png"))
    assert cover.fetch("https://example.com/a.png", workdir) is None
    assert cover.find(workdir) is None, "超限的不该落盘"


def test_fetch_rejects_empty_body(workdir, monkeypatch):
    stub_get(monkeypatch, FakeResp(b"", "image/png"))
    assert cover.fetch("https://example.com/a.png", workdir) is None


# ------------------------------------------------------------------ ensure


def test_ensure_reuses_existing_without_requesting(workdir, monkeypatch):
    (workdir / "cover.jpg").write_bytes(JPEG)
    calls = []
    stub_get(monkeypatch, FakeResp(), calls)
    path = cover.ensure("https://example.com/new.png", workdir)
    assert path.name == "cover.jpg"
    assert calls == [], "已经有封面就不该再发请求（重跑流水线时省时间也省流量）"


def test_ensure_downloads_when_missing(workdir, monkeypatch):
    stub_get(monkeypatch, FakeResp(PNG, "image/png"))
    assert cover.ensure("https://example.com/a.png", workdir).name == "cover.png"


def test_ensure_without_url(workdir, monkeypatch):
    calls = []
    stub_get(monkeypatch, FakeResp(), calls)
    assert cover.ensure(None, workdir) is None
    assert cover.ensure("", workdir) is None
    assert calls == [], "没有链接就不该发请求"


# ------------------------------------------------- 小宇宙封面字段名（回归）
#
# 小宇宙把字段名从 middlePic / smallPic 改成了 middlePicUrl / smallPicUrl / picUrl…
# 旧代码只认老名字，于是**静默返回 None** —— 卡片一直没有封面，而且不报错。


def test_xyz_cover_uses_new_field_names():
    assert xyz._podcast_cover({"image": {"picUrl": "https://x/p.jpg"}}) == "https://x/p.jpg"
    assert xyz._podcast_cover({"image": {"middlePicUrl": "https://x/m.jpg",
                                         "picUrl": "https://x/p.jpg"}}) == "https://x/m.jpg", \
        "中等尺寸优先（省流量）"


def test_xyz_cover_still_accepts_old_field_names():
    assert xyz._podcast_cover({"image": {"middlePic": "https://x/old-m.jpg"}}) == "https://x/old-m.jpg"
    assert xyz._podcast_cover({"image": {"smallPic": "https://x/old-s.jpg"}}) == "https://x/old-s.jpg"


def test_xyz_cover_tolerates_broken_shapes():
    for podcast in ({}, {"image": None}, {"image": {}}, {"image": {"picUrl": ""}},
                    {"image": {"picUrl": "   "}}, {"image": {"picUrl": 123}},
                    {"image": "不是字典"}):
        assert xyz._podcast_cover(podcast) is None, f"坏结构应当返回 None 而不是抛：{podcast}"
