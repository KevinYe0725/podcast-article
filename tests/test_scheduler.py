"""后台调度与队列执行循环：run_queue_once / _wait_job / 订阅入队 / start_scheduler。

三条硬规矩：

1. **绝不真的启动生成任务**（那会下载音频）—— 凡是会走到 Pipeline 的地方一律把
   `webapp._new_job` 换成记录参数的假函数；
2. **绝不联网** —— 订阅相关的 `feeds._fetch` / `feeds.check` 一律打桩；
3. **不留全局状态** —— queue.json / feeds.json / settings.json 全落在 tmp_path，
   `webapp._JOBS` / `webapp._scheduler_started` / `webapp._scheduler_stop` /
   `usage._active` 用完都由 pa fixture 还原。
"""
from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture()
def pa(tmp_output, tmp_path, monkeypatch):
    """把 webapp 指到临时目录，并把模块级全局状态清干净。

    tmp_output 已经隔离了 output/ 与 library.json；这里再补上队列 / 订阅 / 设置，
    并重置调度线程的开关，保证每条用例从同一起点开始。
    """
    import importlib

    import webapp
    from podcast_article import feeds, queue, usage
    from podcast_article import settings as st

    importlib.reload(webapp)                 # 让 OUTPUT_ROOT 读新的 PA_OUTPUT_DIR
    monkeypatch.setattr(queue, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(feeds, "FEEDS_PATH", tmp_path / "feeds.json")
    monkeypatch.setattr(st, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(usage, "_active", None)

    webapp._JOBS.clear()
    webapp._scheduler_started = False
    old_stop = webapp._scheduler_stop
    webapp._scheduler_stop = threading.Event()
    try:
        yield webapp
    finally:
        webapp._JOBS.clear()
        webapp._scheduler_started = False
        webapp._scheduler_stop.set()          # 万一真有线程在等，立刻放它走
        webapp._scheduler_stop = old_stop


def _feed_item(title: str, *, pick: int = 1, feed_id: str = "f1",
               feed_url: str = "https://example.com/feed.xml") -> dict:
    """一条 feeds.check() 风格的发现项（字段与 feeds.check 的返回值一致）。"""
    return {
        "feed_id": feed_id,
        "feed_title": "测试电台",
        "feed_url": feed_url,
        "episode_url": f"https://example.com/{title}",
        "audio_url": f"https://example.com/{title}.mp3",
        "title": title,
        "pub_date": "2024-03-01T00:00:00Z",
        "pick": pick,
    }


# ------------------------------------------------------------------ run_queue_once


def test_run_queue_once_returns_none_on_empty_queue(pa):
    assert pa.run_queue_once() is None, "队列为空时应返回 None"


def test_worker_lease_is_exclusive_and_released(pa):
    first = pa._acquire_worker_lease()
    assert first is not None
    assert pa._acquire_worker_lease() is None

    first.release()
    second = pa._acquire_worker_lease()
    assert second is not None
    second.release()


def test_worker_lease_releases_if_thread_start_and_queue_cleanup_both_fail(pa, monkeypatch):
    original_start = threading.Thread.start

    def fail_start(self):
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    monkeypatch.setattr(pa, "_finish_queue_item", lambda _job: (_ for _ in ()).throw(OSError("db failed")))
    lease = pa._acquire_worker_lease()
    assert lease is not None

    with pytest.raises(RuntimeError, match="thread start failed"):
        pa._new_job("https://example.test/episode", {}, worker_lease=lease)

    assert lease.fd is None
    second = pa._acquire_worker_lease()
    assert second is not None
    second.release()
    monkeypatch.setattr(threading.Thread, "start", original_start)


def test_run_queue_once_claims_pending_item_and_forwards_fields(pa, monkeypatch):
    from podcast_article import queue

    episode = {"source": "rss", "url": "https://example.com/第一集",
               "title": "第一集", "podcast": "测试电台",
               "audio_url": "https://example.com/第一集.mp3", "subtitle_tracks": []}
    added = queue.add("https://example.com/feed.xml",
                      opts={"mode": "deep", "episode": episode}, source="manual")

    calls = []

    def fake_new_job(url, opts, *, source="manual", queue_id=None, workspace=None, quota_guard=None,
                     worker_lease=None):
        calls.append({"url": url, "opts": opts, "source": source, "queue_id": queue_id})
        return "job-fake001"

    monkeypatch.setattr(pa, "_new_job", fake_new_job)

    job_id = pa.run_queue_once()
    assert job_id == "job-fake001", f"应返回 _new_job 给的 job_id，实际 {job_id!r}"
    assert len(calls) == 1, f"应只开一个任务，实际 {len(calls)}"
    assert calls[0]["url"] == "https://example.com/feed.xml", f"链接错误：{calls[0]['url']!r}"
    assert calls[0]["source"] == "manual", f"source 应透传，实际 {calls[0]['source']!r}"
    assert calls[0]["queue_id"] == added[0]["id"], \
        f"queue_id 应是这一条的 id，实际 {calls[0]['queue_id']!r}"
    assert calls[0]["opts"]["mode"] == "deep", f"opts 应透传，实际 {calls[0]['opts']}"
    assert calls[0]["opts"]["episode"] == episode, \
        f"episode 快照应原样透传，实际 {calls[0]['opts'].get('episode')}"

    assert queue.get(added[0]["id"])["state"] == "running", \
        f"取走的条目应变成 running，实际 {queue.get(added[0]['id'])['state']!r}"


def test_run_queue_once_returns_none_while_job_running(pa, monkeypatch):
    from podcast_article import queue

    added = queue.add("https://example.com/1")
    pa._JOBS["job-running"] = {"id": "job-running", "url": "https://example.com/busy",
                              "status": "running"}
    monkeypatch.setattr(pa, "_new_job", lambda *a, **k: pytest.fail("已有任务在跑，不该再开新任务"))

    assert pa.run_queue_once() is None, "已有 running 任务时应返回 None"
    assert queue.get(added[0]["id"])["state"] == "pending", \
        f"返回 None 时不该动队列状态，实际 {queue.get(added[0]['id'])['state']!r}"


# ------------------------------------------------------------------ _wait_job


def test_wait_job_returns_finished_job_immediately(pa):
    pa._JOBS["job-done"] = {"id": "job-done", "status": "done",
                            "queue_id": "q1", "workdir": "某目录"}
    started = time.time()
    job = pa._wait_job("job-done", timeout=5.0, poll=0.01)
    assert job is not None, "已结束的任务应立即返回，不该等超时"
    assert job["status"] == "done", f"状态应是 done，实际 {job['status']!r}"
    assert job["queue_id"] == "q1", f"应带回 queue_id，实际 {job.get('queue_id')!r}"
    assert time.time() - started < 1.0, "已结束的任务应立即返回，不该真的 sleep"


def test_wait_job_returns_none_for_unknown_id(pa):
    started = time.time()
    assert pa._wait_job("不存在", timeout=0.05, poll=0.01) is None, "不存在的 job_id 应返回 None"
    assert time.time() - started < 1.0, "找不到任务应立即返回，不该等满 timeout"


def test_wait_job_gives_up_after_timeout(pa):
    pa._JOBS["job-run"] = {"id": "job-run", "status": "running", "queue_id": "q1"}
    started = time.time()
    assert pa._wait_job("job-run", timeout=0.05, poll=0.01) is None, "一直没跑完应超时返回 None"
    assert time.time() - started >= 0.05, "超时返回至少要等满 timeout"


# ------------------------------------------------------------------ 订阅 → 队列


def test_episode_from_feed_builds_full_snapshot(pa):
    item = _feed_item("第一集")
    episode = pa._episode_from_feed(item)
    assert episode["source"] == "rss", f"来源应是 rss，实际 {episode['source']!r}"
    assert episode["audio_url"] == "https://example.com/第一集.mp3", \
        f"音频地址错误：{episode['audio_url']!r}"
    assert episode["title"] == "第一集", f"标题错误：{episode['title']!r}"
    assert episode["podcast"] == "测试电台", f"节目名应取 feed_title，实际 {episode['podcast']!r}"
    assert episode["url"] == "https://example.com/第一集", \
        f"url 应优先用单集页面链接，实际 {episode['url']!r}"
    assert episode["pub_date"] == "2024-03-01T00:00:00Z", f"发布时间错误：{episode['pub_date']!r}"
    assert episode["subtitle_tracks"] == [], \
        f"快照不该带字幕轨，实际 {episode['subtitle_tracks']!r}"


def test_episode_from_feed_tolerates_missing_fields(pa):
    episode = pa._episode_from_feed({})
    assert episode["title"] == "" and episode["podcast"] == "", \
        f"缺字段时文字应退化成空串，实际 {episode['title']!r} / {episode['podcast']!r}"
    assert episode["audio_url"] is None, f"缺音频地址应是 None，实际 {episode['audio_url']!r}"
    assert episode["subtitle_tracks"] == [], "缺字段时仍应有空字幕轨列表"

    only_feed = pa._episode_from_feed({"feed_url": "https://example.com/feed.xml"})
    assert only_feed["url"] == "https://example.com/feed.xml", \
        f"只有 feed 链接时应退化成它，实际 {only_feed['url']!r}"


def test_enqueue_feed_episodes_adds_snapshots_with_dest(pa):
    from podcast_article import queue

    items = [_feed_item("第二集", pick=2), _feed_item("第一集", pick=1)]
    added = pa._enqueue_feed_episodes(items, dest="c1")

    assert len(added) == 2, f"两条都应入队，实际 {len(added)}"
    snapshot = queue.snapshot()
    assert len(snapshot["items"]) == 2, f"队列里应多两条，实际 {len(snapshot['items'])}"
    assert {i["source"] for i in snapshot["items"]} == {"feed:f1"}, \
        f"来源应是 feed:<id>，实际 {[i['source'] for i in snapshot['items']]}"
    assert all(i["state"] == "pending" for i in snapshot["items"]), "新入队的都应是 pending"
    assert all(i["opts"]["auto_dest"] == "c1" for i in snapshot["items"]), \
        f"dest 应写进 opts.auto_dest：{[i['opts'] for i in snapshot['items']]}"

    episodes = [i["opts"]["episode"] for i in snapshot["items"]]
    assert [e["title"] for e in episodes] == ["第二集", "第一集"], \
        f"入队顺序应与传入一致：{[e['title'] for e in episodes]}"
    assert episodes[0]["audio_url"] == "https://example.com/第二集.mp3", \
        f"快照应带音频地址，实际 {episodes[0]['audio_url']!r}"
    assert episodes[0]["source"] == "rss", f"快照来源应是 rss，实际 {episodes[0]['source']!r}"


def test_enqueue_feed_episodes_without_dest_has_no_auto_dest(pa):
    from podcast_article import queue

    added = pa._enqueue_feed_episodes([_feed_item("第一集")])
    assert len(added) == 1, f"应入队 1 条，实际 {len(added)}"
    assert "auto_dest" not in queue.snapshot()["items"][0]["opts"], \
        f"没给 dest 时不该有 auto_dest：{queue.snapshot()['items'][0]['opts']}"


# ------------------------------------------------------------------ start_scheduler


def test_start_scheduler_disabled_by_env(pa, monkeypatch):
    monkeypatch.setenv("PA_SCHEDULER", "0")
    monkeypatch.setattr(pa, "_scheduler_loop", lambda: pytest.fail("PA_SCHEDULER=0 时不该起线程"))

    assert pa.start_scheduler() is False, "PA_SCHEDULER=0 时应返回 False"
    assert pa._scheduler_started is False, "PA_SCHEDULER=0 时不该把开关置位"


def test_start_scheduler_starts_thread_when_enabled(pa, monkeypatch):
    monkeypatch.setenv("PA_SCHEDULER", "1")
    monkeypatch.setattr(pa, "_scheduler_loop", lambda: None)     # 不让它真的循环

    assert pa.start_scheduler() is True, "PA_SCHEDULER 未设置时应返回 True"

    deadline = time.time() + 2.0
    while time.time() < deadline and not pa._scheduler_started:
        time.sleep(0.01)
    assert pa._scheduler_started is True, "启动后 _scheduler_started 应被置位"
    assert pa.start_scheduler() is False, "已在运行时应幂等返回 False"

    # monkeypatch 后的 _scheduler_loop 立刻返回，线程应很快退出（不会真的循环）
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not any(t.name == "pa-scheduler" and t.is_alive() for t in threading.enumerate()):
            break
        time.sleep(0.01)
    assert not any(t.name == "pa-scheduler" and t.is_alive() for t in threading.enumerate()), \
        "被替换成一进就返回的 _scheduler_loop 不该留下活着的调度线程"


# ------------------------------------------------------------------ check_feeds_now


@pytest.fixture()
def discovered(monkeypatch):
    """把 feeds.check 换成返回两条假条目（不联网、不写 feeds.json）。"""
    from podcast_article import feeds

    items = [_feed_item("第一集", pick=1), _feed_item("第二集", pick=2)]

    def fake_check(fid=None, *, timeout=25.0, feeds_path=None):
        return [dict(i) for i in items]

    monkeypatch.setattr(feeds, "check", fake_check)
    return items


def test_check_feeds_now_reports_and_enqueues(pa, discovered):
    from podcast_article import queue

    result = pa.check_feeds_now()
    assert result["found"] == 2, f"应发现 2 集，实际 {result['found']}"
    assert result["enqueued"] == 2, f"应入队 2 条，实际 {result['enqueued']}"
    assert [e["title"] for e in result["episodes"]] == ["第一集", "第二集"], \
        f"发现列表错误：{result['episodes']}"
    assert result["episodes"][0]["feed"] == "测试电台", \
        f"发现列表应带节目名：{result['episodes'][0]}"
    assert len(queue.snapshot()["items"]) == 2, \
        f"队列里应有 2 条，实际 {len(queue.snapshot()['items'])}"


def test_check_feeds_now_enqueue_false_only_reports(pa, discovered):
    from podcast_article import queue

    result = pa.check_feeds_now(enqueue=False)
    assert result["found"] == 2, f"仍应发现 2 集，实际 {result['found']}"
    assert result["enqueued"] == 0, f"enqueue=False 时不该入队，实际 {result['enqueued']}"
    assert queue.snapshot()["items"] == [], "队列不该有变化"


def test_check_feeds_now_respects_auto_generate_off(pa, discovered):
    from podcast_article import queue
    from podcast_article import settings as st

    st.save(subscriptions={"auto_generate": False})
    result = pa.check_feeds_now()
    assert result["found"] == 2, f"仍应发现 2 集，实际 {result['found']}"
    assert result["enqueued"] == 0, f"关掉自动生成后不该入队，实际 {result['enqueued']}"
    assert queue.snapshot()["items"] == [], "队列不该有变化"


def test_check_feeds_now_writes_auto_dest_from_settings(pa, discovered):
    from podcast_article import queue
    from podcast_article import settings as st

    st.save(subscriptions={"auto_dest": "c12345"})
    result = pa.check_feeds_now()
    assert result["enqueued"] == 2, f"应入队 2 条，实际 {result['enqueued']}"
    assert [i["opts"]["auto_dest"] for i in queue.snapshot()["items"]] == ["c12345", "c12345"], \
        f"设置里的 auto_dest 应写进每一条：{queue.snapshot()['items']}"


# ------------------------------------------------- 队列条目状态回写（_finish_queue_item）
#
# 这组是**实机测试抓到的回归**：原先条目状态的回写挂在后台调度循环里，
# 于是「手动点立即开始」或「--no-scheduler 启动」时，跑完的条目会永远停在 running，
# 而队列只挑 pending —— 一条失败任务就把整条队列堵死了。


def _claimed_item(pa, monkeypatch, *, url: str = "https://example.com/ep1"):
    """入队并 claim 一条，返回 (item, job 字典)。"""
    from podcast_article import queue

    queue.add(url)
    item = queue.next_pending()
    queue.claim(item["id"])
    return item, {"id": "job1", "status": "running", "queue_id": item["id"],
                  "workdir": None, "error": None}


def test_finish_queue_item_marks_done_with_workdir(pa, monkeypatch):
    from podcast_article import queue

    item, job = _claimed_item(pa, monkeypatch)
    job.update(status="done", workdir="20240101-测试台-测试单集")
    pa._finish_queue_item(job)

    after = queue.get(item["id"])
    assert after["state"] == "done", f"任务成功时条目应为 done，实际 {after['state']}"
    assert after["dir"] == "20240101-测试台-测试单集", f"应记下输出目录，实际 {after['dir']}"


def test_finish_queue_item_marks_error_with_message(pa, monkeypatch):
    from podcast_article import queue

    item, job = _claimed_item(pa, monkeypatch)
    job.update(status="error", error="404 Client Error")
    pa._finish_queue_item(job)

    after = queue.get(item["id"])
    assert after["state"] == "error", f"任务失败时条目应为 error，实际 {after['state']}"
    assert "404" in after["error"], f"错误信息应被记下来，实际 {after['error']}"
    assert after["finished_at"] is not None, "完成时间应被写入"


def test_finish_queue_item_does_not_block_queue_after_failure(pa, monkeypatch):
    """核心回归：一条失败之后，下一条必须还能被取到（不能卡在 running）。"""
    from podcast_article import queue

    first, job = _claimed_item(pa, monkeypatch)
    queue.add("https://example.com/ep2")
    job.update(status="error", error="boom")
    pa._finish_queue_item(job)

    nxt = queue.next_pending()
    assert nxt is not None, "第一条失败后，队列里第二条必须仍可被取到"
    assert nxt["id"] != first["id"], "不该又取到同一条"


def test_finish_queue_item_ignores_non_queue_jobs(pa):
    """首页直接提交的任务不属于任何队列条目 —— 回写时必须原样跳过，别误伤队列。"""
    from podcast_article import queue

    queue.add("https://example.com/别人的任务")
    pending_before = queue.snapshot()["items"][0]

    pa._finish_queue_item({"id": "job2", "status": "done", "queue_id": None, "workdir": "x"})

    after = queue.snapshot()["items"]
    assert len(after) == 1, f"队列不该被凭空改动，实际 {after}"
    assert after[0]["state"] == "pending" and after[0]["id"] == pending_before["id"], \
        f"没有 queue_id 的任务不该动到任何条目，实际 {after[0]}"


def test_finish_queue_item_tolerates_deleted_item(pa):
    """用户在任务跑的过程中把条目删了：不能因此让 worker 崩掉，也不能动到别的条目。"""
    from podcast_article import queue

    queue.add("https://example.com/留下的那一条")
    keep = queue.snapshot()["items"][0]["id"]

    # 不应抛异常（这正是这个用例要守的行为）
    pa._finish_queue_item({"id": "job3", "status": "done", "queue_id": "qgone",
                           "workdir": "x", "error": None})

    after = queue.snapshot()["items"]
    assert [i["id"] for i in after] == [keep], f"不该动到其他条目，实际 {after}"
    assert after[0]["state"] == "pending", f"留下的那条应保持 pending，实际 {after[0]['state']}"


def test_run_queue_once_then_finish_leaves_consistent_state(pa, monkeypatch):
    """走一遍真实路径：claim → 假任务失败 → 回写。"""
    from podcast_article import queue

    queue.add("https://example.com/ep1")
    monkeypatch.setattr(pa, "_new_job", lambda url, opts, **kw: "jobX")
    job_id = pa.run_queue_once()
    assert job_id == "jobX", f"应返回 job_id，实际 {job_id}"
    assert queue.snapshot()["counts"]["running"] == 1, "取走的条目应先标成 running"

    running = queue.snapshot()["items"][0]
    pa._finish_queue_item({"id": job_id, "status": "error", "queue_id": running["id"],
                           "workdir": None, "error": "下载失败"})
    assert queue.snapshot()["counts"]["running"] == 0, "回写后不该还有 running"
    assert queue.snapshot()["counts"]["error"] == 1, "失败应被记成 error"


def test_fair_queue_resolves_quota_store_without_flask_context(pa, monkeypatch):
    """The scheduler claims fair jobs outside Flask and still needs an account quota store."""
    from podcast_article import queue

    platform_store = pa._system_store()
    owner_id = platform_store.enabled_account_ids()[0]
    queued = queue.add("https://example.com/scheduled", owner_id=owner_id)
    calls = []
    monkeypatch.setattr(pa, "_new_job", lambda url, opts, **kw: calls.append(kw) or "job-scheduled")

    assert pa.run_queue_once() == "job-scheduled"
    assert len(calls) == 1
    guard = calls[0]["quota_guard"]
    assert guard.owner_id == owner_id
    assert guard.store.path == platform_store.path
    assert queue.get(queued[0]["id"], owner_id=owner_id)["state"] == "running"


# ------------------------------------------------- 重启后的条目回收（recover_running）


def test_recover_running_requeues_orphans(pa):
    """进程被杀留下的 running：启动时必须归位，否则队列被永久堵死。"""
    from podcast_article import queue

    queue.add("https://example.com/ep1")
    queue.claim(queue.next_pending()["id"])
    assert queue.snapshot()["counts"]["running"] == 1

    recovered = queue.recover_running()
    assert recovered == 1, f"应回收 1 条，实际 {recovered}"
    assert queue.snapshot()["counts"]["pending"] == 1, "回收后应回到 pending"
    assert queue.snapshot()["counts"]["running"] == 0
    assert queue.next_pending() is not None, "回收后必须能被再次取到"


def test_recover_running_gives_up_after_repeated_crashes(pa):
    """连续崩多次的条目要标成失败，而不是无限空转。"""
    from podcast_article import queue

    queue.add("https://example.com/ep1")
    item_id = queue.next_pending()["id"]
    for _ in range(queue.MAX_AUTO_RECOVER + 1):
        queue.claim(item_id)
        queue.recover_running()

    after = queue.get(item_id)
    assert after["state"] == "error", f"反复中断后应停止自动重试，实际 {after['state']}"
    assert "中断" in after["error"] or "没跑完" in after["error"], \
        f"应说明为什么停下来，实际 {after['error']}"


def test_recover_running_is_noop_when_nothing_running(pa):
    from podcast_article import queue

    queue.add("https://example.com/ep1")
    assert queue.recover_running() == 0, "没有 running 时不该做任何事"
    assert queue.snapshot()["counts"]["pending"] == 1


def test_start_scheduler_recovers_orphans(pa, monkeypatch):
    """start_scheduler 起来时顺手回收，避免重启后队列卡住。"""
    from podcast_article import queue

    queue.add("https://example.com/ep1")
    queue.claim(queue.next_pending()["id"])
    monkeypatch.setenv("PA_SCHEDULER", "1")
    monkeypatch.setattr(pa, "_scheduler_loop", lambda: None)   # 别真的起循环

    assert pa.start_scheduler() is True, "未禁用时应启动调度"
    assert queue.snapshot()["counts"]["pending"] == 1, "启动调度时应回收上次没跑完的条目"
