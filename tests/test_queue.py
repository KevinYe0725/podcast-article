"""批量队列：链接解析、入队去重、状态流转、排序与清理。

每条测试都把 QUEUE_PATH 指到 tmp_path（monkeypatch.setattr），
绝不读写仓库根目录下真实的 queue.json。
"""
from __future__ import annotations

import pytest

from podcast_article import queue


@pytest.fixture(autouse=True)
def queue_file(tmp_path, monkeypatch):
    """把队列文件指到临时目录，任何测试都不会碰真实队列。"""
    path = tmp_path / "queue.json"
    monkeypatch.setattr(queue, "QUEUE_PATH", path)
    return path


def seed(count: int = 3) -> list[str]:
    """入队 count 条不同链接，按加入顺序返回它们的 id。"""
    urls = "\n".join(f"https://a.com/{n}" for n in range(1, count + 1))
    return [item["id"] for item in queue.add(urls)]


def ids_in_order() -> list[str]:
    return [item["id"] for item in queue.snapshot()["items"]]


# ----------------------------------------------------------------- parse_urls


def test_parse_urls_multiline():
    got = queue.parse_urls("https://a.com/1\nhttps://a.com/2\n\n  https://a.com/3  ")
    assert got == ["https://a.com/1", "https://a.com/2", "https://a.com/3"], f"多行解析错误：{got}"


def test_parse_urls_splits_on_space_and_comma():
    got = queue.parse_urls("https://a.com/1, https://a.com/2，https://a.com/3、https://a.com/4 https://a.com/5")
    assert got == [f"https://a.com/{n}" for n in range(1, 6)], f"一行内的多种分隔符应都能拆开：{got}"


def test_parse_urls_strips_bullet_prefixes():
    got = queue.parse_urls("1. https://a.com/1\n- https://a.com/2\n* https://a.com/3\n• https://a.com/4\n2、https://a.com/5")
    assert got == [f"https://a.com/{n}" for n in range(1, 6)], f"行首序号/项目符号应被去掉：{got}"


def test_parse_urls_keeps_local_paths():
    got = queue.parse_urls("/Users/me/录音.m4a\n~/Downloads/某播客.mp3")
    assert got == ["/Users/me/录音.m4a", "~/Downloads/某播客.mp3"], \
        f"本地文件路径也应被识别（转写本地音频）：{got}"


def test_parse_urls_drops_noise_and_dedupes():
    text = ("随便说点什么\n"
            "第一条 https://a.com/1 后面还有话\n"
            "ftp://a.com/x\n"
            "www.a.com/2\n"
            "https://a.com/1\n")
    got = queue.parse_urls(text)
    assert got == ["https://a.com/1"], f"非 http(s) 的噪音行应被丢掉、重复链接只保留一次：{got}"


def test_parse_urls_accepts_list_input():
    got = queue.parse_urls(["https://a.com/1", "https://a.com/2", "噪音"])
    assert got == ["https://a.com/1", "https://a.com/2"], f"列表输入应逐条解析：{got}"


def test_parse_urls_caps_per_call():
    parsed = queue.parse_urls("\n".join(f"https://a.com/{n}" for n in range(1, 61)))
    assert len(parsed) == queue.MAX_URLS_PER_CALL, \
        f"每次最多 {queue.MAX_URLS_PER_CALL} 条，实际 {len(parsed)}"
    assert parsed[0] == "https://a.com/1" and parsed[-1] == "https://a.com/50", "截断应保留前面的链接"


def test_parse_urls_empty_input():
    assert queue.parse_urls("") == [] and queue.parse_urls(None) == [] and queue.parse_urls([]) == []


# ----------------------------------------------------------------------- add


def test_add_enqueues_pending_items(queue_file):
    added = queue.add("https://a.com/1\nhttps://a.com/2", title="标题", pick=2, source="feed:abc")
    assert len(added) == 2, f"两条链接应入队两条，实际 {len(added)}"
    item = added[0]
    assert item["id"].startswith("q") and item["url"] == "https://a.com/1"
    assert item["state"] == "pending" and item["error"] == "" and item["dir"] is None
    assert item["title"] == "标题" and item["pick"] == 2 and item["source"] == "feed:abc"
    assert isinstance(item["added_at"], float)
    assert queue_file.exists(), "入队后队列文件应落盘"
    assert ids_in_order() == [i["id"] for i in added]


def test_add_snapshots_opts():
    opts = {"length_mode": "concise", "auto_polish": True}
    item = queue.add("https://a.com/1", opts=opts)[0]
    assert item["opts"] == opts, "opts 应被快照保存"
    opts["length_mode"] = "改过了"
    assert queue.get(item["id"])["opts"] == {"length_mode": "concise", "auto_polish": True}, \
        "提交之后再改 opts 字典，不应影响已入队的快照"


def test_add_skips_duplicate_pending_url():
    queue.add("https://a.com/1")
    again = queue.add("https://a.com/1")
    assert again == [], "同一链接且上一条还在排队时应被跳过"
    assert len(queue.snapshot()["items"]) == 1, "重复提交不应产生第二条"


def test_add_same_url_with_other_pick_is_not_duplicate():
    queue.add("https://a.com/1", pick=1)
    assert len(queue.add("https://a.com/1", pick=2)) == 1, \
        "同链接不同集数（pick）是不同任务，不该当成重复"


def test_add_allows_same_url_after_previous_done():
    first = queue.add("https://a.com/1")[0]
    queue.finish(first["id"], "done", dir_name="20240101-测试台-测试单集")
    again = queue.add("https://a.com/1")
    assert len(again) == 1, "上一条已经完成，同一链接应能重新入队"
    assert again[0]["id"] != first["id"] and again[0]["state"] == "pending"
    assert len(queue.snapshot()["items"]) == 2


def test_add_raises_when_nothing_recognized():
    with pytest.raises(ValueError, match="没有识别到链接"):
        queue.add("今天天气不错\n明天也是")
    assert queue.snapshot()["items"] == [], "抛错时不应写入任何条目"


# ------------------------------------------------------- 取任务与状态流转


def test_next_pending_is_fifo():
    ids = seed(3)
    assert queue.next_pending()["id"] == ids[0], "应取最早入队的一条（FIFO）"
    queue.claim(ids[0])
    assert queue.next_pending()["id"] == ids[1], "已领走的（running）不应再被取到"


def test_next_pending_none_when_empty():
    assert queue.next_pending() is None and queue.has_running() is False


def test_claim_moves_pending_to_running():
    item = queue.add("https://a.com/1")[0]
    claimed = queue.claim(item["id"])
    assert claimed is not None and claimed["state"] == "running", "claim() 应把 pending 变 running"
    assert claimed["started_at"] is not None, "claim() 应记下开始时间"
    assert queue.get(item["id"])["state"] == "running", "变化必须落盘"
    assert queue.has_running() is True


def test_claim_returns_none_for_running_and_done():
    ids = seed(2)
    queue.claim(ids[0])
    assert queue.claim(ids[0]) is None, "已经 running 的不应被再次领走"
    queue.claim(ids[1])
    queue.finish(ids[1], "done")
    assert queue.claim(ids[1]) is None, "已完成的不能再被领走"
    assert queue.claim("qnope") is None, "不存在的 id 应返回 None"


def test_finish_records_state_dir_and_error():
    item = queue.add("https://a.com/1")[0]
    done = queue.finish(item["id"], "done", dir_name="20240101-测试台-测试单集")
    assert done["state"] == "done" and done["finished_at"] is not None
    assert done["dir"] == "20240101-测试台-测试单集", "跑完后应记下输出目录名"
    assert done["error"] == "", "成功完成时 error 应为空"

    failed = queue.finish(item["id"], "error", error="转写失败")
    assert failed["state"] == "error" and failed["error"] == "转写失败"
    assert queue.get(item["id"])["error"] == "转写失败", "错误信息应落盘"
    assert queue.get(item["id"])["dir"] == "20240101-测试台-测试单集", "再次 finish 不该抹掉已有的目录名"


def test_finish_truncates_long_error():
    item = queue.add("https://a.com/1")[0]
    failed = queue.finish(item["id"], "error", error="x" * 800)
    assert len(failed["error"]) == 500, f"错误信息应截断到 500 字，实际 {len(failed['error'])}"


def test_finish_rejects_unknown_state():
    item = queue.add("https://a.com/1")[0]
    with pytest.raises(ValueError, match="未知状态"):
        queue.finish(item["id"], "完成啦")
    assert queue.get(item["id"])["state"] == "pending", "拒绝之后状态不应被改动"


def test_finish_rejects_missing_id():
    with pytest.raises(ValueError, match="没有这一条"):
        queue.finish("qnope", "done")


# ---------------------------------------------------------------------- move


def test_move_earlier_by_one():
    ids = seed(3)
    assert queue.move(ids[2], -1) == 1, "第三条提前一位后应落在索引 1"
    assert ids_in_order() == [ids[0], ids[2], ids[1]], "顺序应为 1、3、2"


def test_move_at_head_does_not_move():
    ids = seed(3)
    assert queue.move(ids[0], -1) == 0, "已在队首时返回 0 且不动"
    assert ids_in_order() == ids, "队首再上移不应改变顺序"


def test_move_clamps_at_tail():
    ids = seed(3)
    assert queue.move(ids[0], 99) == 2, "越界时应夹到队尾"
    assert ids_in_order() == [ids[1], ids[2], ids[0]]


def test_move_ignores_non_pending_item():
    ids = seed(3)
    queue.claim(ids[1])
    assert queue.move(ids[1], -1) == 1, "非 pending 条目不动，返回它当前的位置"
    assert ids_in_order() == ids, "正在跑的任务不该被调整顺序"


def test_move_unknown_id_raises():
    seed(1)
    with pytest.raises(ValueError, match="没有这一条"):
        queue.move("qnope", -1)


# ------------------------------------------------------------------- reorder


def test_reorder_follows_given_id_order():
    ids = seed(3)
    queue.reorder([ids[2], ids[1]])
    assert ids_in_order() == [ids[2], ids[1], ids[0]], \
        "应按给定顺序重排，未出现的条目保持原有相对顺序跟在后面"


def test_reorder_with_full_list():
    ids = seed(3)
    queue.reorder(list(reversed(ids)))
    assert ids_in_order() == list(reversed(ids)), "给出完整顺序时应完全按它排"


# --------------------------------------------------------------- retry_failed


def test_retry_failed_returns_errors_to_pending():
    ids = seed(3)
    queue.finish(ids[0], "error", error="炸了")
    queue.finish(ids[1], "error", error="也炸了")
    queue.finish(ids[2], "done")

    assert queue.retry_failed() == 2, "应把两条失败的重排回队列"
    items = {i["id"]: i for i in queue.snapshot()["items"]}
    assert items[ids[0]]["state"] == "pending" and items[ids[1]]["state"] == "pending"
    assert items[ids[0]]["error"] == "" and items[ids[1]]["error"] == "", "重试时应清空错误信息"
    assert items[ids[0]]["finished_at"] is None, "重排回队列后不该再留着结束时间"
    assert items[ids[2]]["state"] == "done", "已完成的条目不该被重跑"
    assert queue.retry_failed() == 0, "没有失败条目时应返回 0"


# --------------------------------------------------------------------- clear


def seed_all_states() -> list[str]:
    """造 4 条：0 排队中、1 生成中、2 已完成、3 失败。"""
    ids = seed(4)
    queue.claim(ids[1])
    queue.finish(ids[2], "done", dir_name="d2")
    queue.finish(ids[3], "error", error="炸了")
    return ids


def test_clear_removes_finished_but_keeps_pending_and_running():
    ids = seed_all_states()
    assert queue.clear() == 2, "应清掉 done 与 error 共两条"
    assert ids_in_order() == [ids[0], ids[1]], "排队中/生成中的条目必须保留"
    assert queue.snapshot()["counts"]["done"] == 0 and queue.snapshot()["counts"]["error"] == 0


def test_clear_keep_failed_keeps_errors():
    ids = seed_all_states()
    assert queue.clear(keep_failed=True) == 1, "keep_failed=True 时只清 done"
    states = {i["id"]: i["state"] for i in queue.snapshot()["items"]}
    assert states[ids[3]] == "error", "失败的条目应被保留以便重试"
    assert ids[2] not in states, "done 仍然要被清掉"
    assert ids[0] in states and ids[1] in states


def test_clear_returns_zero_when_nothing_finished():
    seed(2)
    assert queue.clear() == 0, "没有已结束的条目时应返回 0"
    assert len(queue.snapshot()["items"]) == 2


# ------------------------------------------------------------------ snapshot


def test_snapshot_counts_and_active():
    ids = seed_all_states()
    snap = queue.snapshot()
    assert snap["counts"] == {"pending": 1, "running": 1, "done": 1, "error": 1, "skipped": 0}, \
        f"计数错误：{snap['counts']}"
    assert snap["active"] == 2, f"active 应等于 pending + running = 2，实际 {snap['active']}"
    assert snap["labels"] == queue.STATE_LABELS
    assert [i["id"] for i in snap["items"]] == ids, "快照应按加入顺序返回"


def test_snapshot_empty_queue():
    snap = queue.snapshot()
    assert snap["items"] == [] and snap["active"] == 0
    assert snap["counts"] == {s: 0 for s in queue.STATES}, "空队列时每种状态都应出现且为 0"


# -------------------------------------------------------------------- update


def test_update_only_touches_whitelisted_fields():
    item = queue.add("https://a.com/1")[0]
    updated = queue.update(item["id"], title="新标题", pick=3, state="done", dir="偷偷改的")
    assert updated["title"] == "新标题" and updated["pick"] == 3, "白名单字段应能改"
    assert updated["state"] == "pending", "state 不在白名单里，改它应无效"
    assert updated["dir"] is None, "dir 不在白名单里，改它应无效"
    assert queue.get(item["id"])["title"] == "新标题", "改动应落盘"


def test_update_unknown_id_raises():
    with pytest.raises(ValueError, match="没有这一条"):
        queue.update("qnope", title="x")


# ---------------------------------------------------------------- 坏文件容错


def test_snapshot_survives_corrupt_queue_file(queue_file):
    queue_file.write_text("{oops", encoding="utf-8")
    snap = queue.snapshot()
    assert snap["items"] == [], "坏 JSON 应被当成空队列，而不是抛异常"
    assert snap["active"] == 0 and snap["counts"] == {s: 0 for s in queue.STATES}

    assert len(queue.add("https://a.com/1")) == 1, "坏文件之后仍应能重新入队"
    assert len(queue.snapshot()["items"]) == 1


def test_load_tolerates_items_not_a_list(queue_file):
    queue_file.write_text('{"items": "乱写的"}', encoding="utf-8")
    assert queue.snapshot()["items"] == [], "items 不是列表时应退回空队列"


def test_get_and_remove():
    ids = seed(2)
    assert queue.get(ids[0])["url"] == "https://a.com/1"
    assert queue.get("qnope") is None
    assert queue.remove(ids[0]) is True, "移除已存在的条目应返回 True"
    assert ids_in_order() == [ids[1]]
    assert queue.remove(ids[0]) is False, "移除不存在的条目应返回 False"
