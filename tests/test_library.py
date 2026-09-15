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


# ---------------------------------------------------------------- 阅读状态
# 四种状态：unread 未读 / reading 在读 / read 已读 / later 稍后读，未记录时默认「未读」。


@pytest.fixture()
def store_file(tmp_path):
    """当前测试实际使用的 library.json（上面的 store 装置已把 STORE_PATH 指到 tmp_path）。"""
    return tmp_path / "library.json"


def test_set_status_roundtrip(store_file):
    import json

    assert library.set_status("dir-a", "read") == "read", "set_status() 应回显设置后的状态"
    assert library.status_of("dir-a") == "read", "写入后 status_of() 应读回同一个状态"
    assert library.statuses() == {"dir-a": "read"}, f"statuses() 应只含显式记录过的文章：{library.statuses()}"
    assert library.status_of("没记录过的目录") == "unread", "未记录的目录默认「未读」"

    stored = json.loads(store_file.read_text(encoding="utf-8"))["status"]["dir-a"]
    assert stored["s"] == "read", f"存储结构应为 {{'s': 状态, 'at': 时间}}，实际 {stored}"
    assert isinstance(stored["at"], (int, float)), "存储里应记下设置时间"


def test_set_status_accepts_all_four_states():
    for name, status in zip(("d1", "d2", "d3", "d4"), library.STATUSES):
        assert library.set_status(name, status) == status
    assert library.statuses() == {"d1": "unread", "d2": "reading", "d3": "read", "d4": "later"}


def test_set_status_rejects_unknown_value():
    with pytest.raises(ValueError, match="未知的阅读状态"):
        library.set_status("dir-a", "已读")
    assert library.statuses() == {}, "抛错时不应写入任何记录"


def test_set_status_rejects_empty_dir_name():
    with pytest.raises(ValueError, match="缺少文章目录"):
        library.set_status("   ", "read")
    with pytest.raises(ValueError, match="缺少文章目录"):
        library.set_status("", "read")


def test_set_status_none_or_blank_clears_record():
    library.set_status("dir-a", "later")
    assert library.set_status("dir-a", None) == "unread", "清除记录后应回到默认的「未读」"
    assert "dir-a" not in library.statuses(), "清除后不该再留下记录"
    assert library.status_of("dir-a") == "unread"

    library.set_status("dir-b", "reading")
    library.set_status("dir-b", "")
    assert library.status_of("dir-b") == "unread", "空字符串同样表示清除记录"


def test_snapshot_carries_status_counts_and_labels():
    library.set_status("dir-a", "read")
    library.set_status("dir-b", "read")
    library.set_status("dir-c", "later")

    snap = library.snapshot()
    assert snap["status"] == {"dir-a": "read", "dir-b": "read", "dir-c": "later"}
    assert set(snap["status_counts"]) == set(library.STATUSES), \
        f"四种状态都要有键，实际 {sorted(snap['status_counts'])}"
    assert snap["status_counts"] == {"unread": 0, "reading": 0, "read": 2, "later": 1}, \
        f"未记录的文章不该被计入任何状态：{snap['status_counts']}"
    assert snap["status_labels"] == library.STATUS_LABELS, "界面用的中文标签应一起返回"


def test_status_counts_only_counts_valid_statuses(store_file):
    import json

    library.set_status("dir-a", "read")
    library.set_status("dir-b", "later")

    raw = json.loads(store_file.read_text(encoding="utf-8"))
    raw["status"]["dir-c"] = "看完啦"                       # 非法状态值：字符串
    raw["status"]["dir-d"] = {"s": 3}                       # 非法状态值：数字
    raw["status"]["dir-e"] = "read"                         # 合法状态：直接写成字符串也算
    store_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    snap = library.snapshot()
    assert snap["status_counts"] == {"unread": 0, "reading": 0, "read": 2, "later": 1}, \
        f"status_counts 只应统计合法状态，实际 {snap['status_counts']}"
    assert "dir-c" not in snap["status"] and "dir-d" not in snap["status"], "非法状态应被忽略"
    assert snap["status"]["dir-e"] == "read"


def test_corrupt_status_entries_do_not_break_other_features(store_file):
    import json

    assert library.STORE_PATH == store_file, \
        "STORE_PATH 必须指向临时文件（测试隔离的前提），实际 %s" % library.STORE_PATH
    store_file.write_text(json.dumps({
        "categories": [{"id": "c1", "name": "AI 技术", "color": "#10a37f"}],
        "assignments": {"dir-a": "c1"},
        "status": {"dir-a": "不是状态", "dir-b": {"s": "乱写的值"}, "dir-c": {"s": "read"}},
    }, ensure_ascii=False), encoding="utf-8")

    snap = library.snapshot()                      # 不能抛异常
    assert snap["status"] == {"dir-c": "read"}, f"坏状态条目应被忽略：{snap['status']}"
    assert snap["status_counts"] == {"unread": 0, "reading": 0, "read": 1, "later": 0}
    assert snap["categories"][0]["count"] == 1, "坏状态数据不该影响分类统计"
    assert library.category_of("dir-a") == "c1", "坏状态数据不该影响分类归属"
    assert library.status_of("dir-a") == "unread", "读不出状态的目录退回「未读」"

    library.set_status("dir-a", "reading")         # 其他功能仍然可用
    assert library.status_of("dir-a") == "reading"
    assert library.snapshot()["status_counts"]["reading"] == 1


def test_status_stored_as_non_mapping_does_not_crash(store_file):
    import json

    store_file.write_text(json.dumps({"categories": [], "assignments": {},
                                      "status": ["乱写的"]}, ensure_ascii=False), encoding="utf-8")
    snap = library.snapshot()
    assert snap["status"] == {}, f"无法识别的 status 结构应被忽略，实际 {snap['status']}"


def test_forget_clears_category_and_status():
    c = library.create("一")
    library.assign("dir-a", c["id"])
    library.set_status("dir-a", "read")

    library.forget("dir-a")
    assert library.category_of("dir-a") is None, "forget 应清掉分类归属"
    assert library.status_of("dir-a") == "unread", "forget 应同时清掉阅读状态"
    assert library.statuses() == {}, f"记录应被彻底删除，实际 {library.statuses()}"
    assert library.snapshot()["status_counts"]["read"] == 0


def test_forget_clears_status_even_without_category():
    library.set_status("dir-a", "later")
    library.forget("dir-a")
    assert library.statuses() == {}, "即使这条没有分类归属，forget 也要把阅读状态清掉"
