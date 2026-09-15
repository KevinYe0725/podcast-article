"""分类存储：增删改名、归属、删分类不删文章。"""
import pytest

from podcast_article import library


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "STORE_PATH", tmp_path / "library.json")


def test_create_and_snapshot():
    c = library.create("AI 技术")
    assert c["name"] == "AI 技术" and c["id"].startswith("c")
    assert c["color"].startswith("#")
    snap = library.snapshot()
    assert [x["name"] for x in snap["categories"]] == ["AI 技术"]
    assert snap["categories"][0]["count"] == 0


def test_create_rejects_empty_and_duplicate():
    library.create("读书")
    with pytest.raises(ValueError, match="不能为空"):
        library.create("   ")
    with pytest.raises(ValueError, match="同名"):
        library.create("读书")
    with pytest.raises(ValueError, match="20"):
        library.create("x" * 21)


def test_assign_and_counts():
    c1, c2 = library.create("一"), library.create("二")
    library.assign("dir-a", c1["id"])
    library.assign("dir-b", c1["id"])
    library.assign("dir-c", c2["id"])
    counts = {x["name"]: x["count"] for x in library.snapshot()["categories"]}
    assert counts == {"一": 2, "二": 1}
    assert library.category_of("dir-a") == c1["id"]


def test_assign_unknown_category_raises():
    with pytest.raises(ValueError, match="分类不存在"):
        library.assign("dir-a", "cnope")


def test_unassign_with_empty_value():
    c = library.create("一")
    library.assign("dir-a", c["id"])
    library.assign("dir-a", None)
    assert library.category_of("dir-a") is None


def test_delete_category_keeps_articles():
    c = library.create("临时")
    library.assign("dir-a", c["id"])
    library.delete(c["id"])
    snap = library.snapshot()
    assert snap["categories"] == []
    assert snap["assignments"] == {}          # 归属清空
    assert library.category_of("dir-a") is None


def test_rename():
    c = library.create("旧名")
    library.rename(c["id"], "新名")
    assert [x["name"] for x in library.snapshot()["categories"]] == ["新名"]
    with pytest.raises(ValueError, match="不存在"):
        library.rename("cnope", "x")


def test_forget_only_removes_one_entry():
    c = library.create("一")
    library.assign("dir-a", c["id"])
    library.assign("dir-b", c["id"])
    library.forget("dir-a")
    assert library.category_of("dir-a") is None
    assert library.category_of("dir-b") == c["id"]
    assert library.snapshot()["categories"][0]["count"] == 1
