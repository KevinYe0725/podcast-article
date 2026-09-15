"""用量与费用记账：分时段计价、usage 拆分、累加、落盘与汇总。

两条硬规矩：
1. 判断高峰/空闲一律用**注入的 datetime**，不依赖跑测试时的真实时间；
2. 落盘一律落在 tmp_path，绝不碰真实的 output/ 目录。
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from podcast_article import usage

# 北京时间（官方计价用的时段都是北京时间）
CN = timezone(timedelta(hours=8))


def cn(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """构造一个明确的北京时间。2026-01-07 是周三，2026-01-10 是周六。"""
    return datetime(year, month, day, hour, minute, tzinfo=CN)


def make_usage(model: str = "deepseek-flash", *, hit: int = 0, miss: int = 0,
               out: int = 0, peak: bool = False) -> dict:
    """造一份 usage 结构：把三类 token 累加进 peak 或 off 桶。"""
    u = usage.empty(model)
    if hit or miss or out:
        usage.add(u, {"hit": hit, "miss": miss, "out": out}, peak=peak)
    return u


# ------------------------------------------------------------------ is_peak


def test_is_peak_weekday_morning_window():
    assert usage.is_peak(cn(2026, 1, 7, 10, 0)) is True, "周三北京时间 10:00 应在高峰时段内"
    assert usage.is_peak(cn(2026, 1, 7, 9, 0)) is True, "9:00 是区间左端，算高峰"
    assert usage.is_peak(cn(2026, 1, 7, 11, 59)) is True, "11:59 仍在 9-12 内"
    assert usage.is_peak(cn(2026, 1, 7, 12, 0)) is False, "12:00 是右开端点，不算高峰"
    assert usage.is_peak(cn(2026, 1, 7, 8, 59)) is False, "8:59 还没到 9:00，不算高峰"


def test_is_peak_afternoon_window():
    assert usage.is_peak(cn(2026, 1, 7, 15, 0)) is True, "周三 15:00 在 14-18 高峰段内"
    assert usage.is_peak(cn(2026, 1, 7, 14, 0)) is True, "14:00 是区间左端，算高峰"
    assert usage.is_peak(cn(2026, 1, 7, 18, 0)) is False, "18:00 是右开端点，不算高峰"
    assert usage.is_peak(cn(2026, 1, 7, 13, 0)) is False, "13:00 夹在两段之间，算空闲"
    assert usage.is_peak(cn(2026, 1, 7, 23, 30)) is False, "深夜算空闲"


def test_is_peak_weekend_is_always_off():
    assert usage.is_peak(cn(2026, 1, 10, 10, 0)) is False, "周六 10:00 即使落在时段内也算空闲"
    assert usage.is_peak(cn(2026, 1, 10, 15, 0)) is False, "周六 15:00 也算空闲"
    assert usage.is_peak(cn(2026, 1, 11, 10, 0)) is False, "周日全天算空闲"


def test_is_peak_converts_other_timezones_to_beijing():
    # 传进去的 datetime 可以是任意时区，函数内部会转成北京时间
    assert usage.is_peak(datetime(2026, 1, 7, 2, 0, tzinfo=timezone.utc)) is True, \
        "UTC 02:00 = 北京周三 10:00，应为高峰"
    assert usage.is_peak(datetime(2026, 1, 6, 20, 0, tzinfo=timezone.utc)) is False, \
        "UTC 周二 20:00 = 北京周三 04:00，应为空闲"
    est = timezone(timedelta(hours=-5))
    assert usage.is_peak(datetime(2026, 1, 6, 21, 0, tzinfo=est)) is True, \
        "美东周二 21:00 = 北京周三 10:00，应为高峰"


# --------------------------------------------------------------- split_usage


def test_split_usage_standard_fields():
    got = usage.split_usage({
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 60,
    })
    assert got == {"hit": 40, "miss": 60, "out": 30}, f"标准响应字段拆分错误：{got}"


def test_split_usage_without_cache_fields_counts_all_as_miss():
    got = usage.split_usage({"prompt_tokens": 100, "completion_tokens": 30})
    assert got == {"hit": 0, "miss": 100, "out": 30}, \
        f"老响应没有缓存字段时 input 应全部算未命中（宁可高估费用）：{got}"


def test_split_usage_string_values_are_coerced():
    got = usage.split_usage({
        "prompt_tokens": "120",
        "completion_tokens": "30",
        "prompt_cache_hit_tokens": "20",
        "prompt_cache_miss_tokens": "100",
    })
    assert got == {"hit": 20, "miss": 100, "out": 30}, f"字符串数值应能转成 int：{got}"


def test_split_usage_object_like_response():
    raw = types.SimpleNamespace(prompt_tokens=10, completion_tokens=3)
    assert usage.split_usage(raw) == {"hit": 0, "miss": 10, "out": 3}, \
        "注意：官方 SDK 返回的对象（不是 dict）也要能读出来"


def test_split_usage_missing_or_none_is_zero_without_crash():
    zero = {"hit": 0, "miss": 0, "out": 0}
    assert usage.split_usage({}) == zero, "空 dict 应全部为 0"
    assert usage.split_usage(None) == zero, "None 不应炸，且全为 0"
    assert usage.split_usage({"prompt_tokens": None, "completion_tokens": None,
                              "prompt_cache_hit_tokens": None,
                              "prompt_cache_miss_tokens": None}) == zero, "显式 None 应为 0"
    assert usage.split_usage({"prompt_tokens": "不是数字", "completion_tokens": "??"}) == zero, \
        "无法转换的值应退化为 0，而不是抛异常"


def test_split_usage_zero_hit_with_miss_keeps_miss():
    got = usage.split_usage({"prompt_tokens": 100, "completion_tokens": 1,
                             "prompt_cache_hit_tokens": 0,
                             "prompt_cache_miss_tokens": 100})
    assert got == {"hit": 0, "miss": 100, "out": 1}, \
        f"显式给出未命中数时应原样采用，不要退回 prompt_tokens：{got}"


def test_split_usage_partial_cache_fields_should_count_remainder_as_miss():
    got = usage.split_usage({"prompt_tokens": 100, "completion_tokens": 5,
                             "prompt_cache_hit_tokens": 40})
    assert got["miss"] == 60, f"命中 40 / prompt 100 时未命中应是 60，实际 {got}"


# ------------------------------------------------------------------------ add


def test_add_accumulates_tokens_calls_and_both_buckets():
    u = usage.empty("deepseek-flash")
    usage.add(u, {"hit": 10, "miss": 90, "out": 5}, peak=False)
    usage.add(u, {"hit": 1, "miss": 2, "out": 3}, peak=True)

    assert u["calls"] == 2, f"两次调用应累计 calls=2，实际 {u['calls']}"
    assert u["hit_tokens"] == 11, f"hit_tokens 应为 10+1=11，实际 {u['hit_tokens']}"
    assert u["miss_tokens"] == 92, f"miss_tokens 应为 90+2=92，实际 {u['miss_tokens']}"
    assert u["out_tokens"] == 8, f"out_tokens 应为 5+3=8，实际 {u['out_tokens']}"
    assert u["off"] == {"hit": 10, "miss": 90, "out": 5}, f"空闲桶错误：{u['off']}"
    assert u["peak"] == {"hit": 1, "miss": 2, "out": 3}, f"高峰桶错误：{u['peak']}"


def test_add_creates_buckets_when_missing():
    u = {"model": "deepseek-flash", "calls": 0}
    usage.add(u, {"hit": 1, "miss": 2, "out": 3}, peak=True)
    assert u["peak"] == {"hit": 1, "miss": 2, "out": 3}, "缺桶时应自动补出 peak/off 结构"
    assert u["off"] == {"hit": 0, "miss": 0, "out": 0}
    assert u["calls"] == 1


# ------------------------------------------------------------------- cost_cny


def test_cost_cny_flash_off_peak_hand_checked():
    # flash 空闲档：未命中 1.0 元/百万，输出 4.0 元/百万
    u = make_usage("deepseek-flash", miss=1_000_000, out=1_000_000, peak=False)
    assert u["off"]["miss"] == 1_000_000
    assert usage.cost_cny(u) == pytest.approx(5.0), \
        f"空闲档 1M 未命中 + 1M 输出 应为 1 + 4 = 5.0 元，实际 {usage.cost_cny(u)}"


def test_cost_cny_flash_peak_is_double():
    # flash 高峰档：未命中 2.0 元/百万，输出 8.0 元/百万
    u = make_usage("deepseek-flash", miss=1_000_000, out=1_000_000, peak=True)
    assert u["peak"]["miss"] == 1_000_000
    assert usage.cost_cny(u) == pytest.approx(10.0), \
        f"高峰档同样的量应为 2 + 8 = 10.0 元，实际 {usage.cost_cny(u)}"


def test_cost_cny_pro_prices_hit_miss_and_out():
    # pro 空闲档：命中 0.15 / 未命中 4.5 / 输出 13.5（元每百万）
    u = make_usage("deepseek-v4-pro", hit=1_000_000, miss=1_000_000, out=1_000_000)
    assert usage.cost_cny(u) == pytest.approx(18.15), \
        f"pro 空闲档 0.15 + 4.5 + 13.5 应为 18.15 元，实际 {usage.cost_cny(u)}"


def test_cost_cny_unknown_model_falls_back_to_flash():
    unknown = make_usage("某用户自己填的模型", miss=1_000_000, out=1_000_000)
    cost = usage.cost_cny(unknown)
    assert cost == pytest.approx(5.0), \
        f"未知模型应退回 flash 价格（5.0 元），而不是抛异常或算成 0，实际 {cost}"
    assert usage.prices_for("某用户自己填的模型") == usage.MODEL_PRICES["deepseek-flash"], \
        "prices_for() 对未知模型也应返回 flash 价目表"


def test_cost_cny_empty_usage_is_zero():
    assert usage.cost_cny(usage.empty("deepseek-flash")) == 0.0, "没有任何 token 时费用应为 0"


def test_describe_adds_totals_and_cache_hit_rate():
    u = make_usage("deepseek-flash", hit=25, miss=75, out=10)
    d = usage.describe(u)
    assert d["input_tokens"] == 100, f"输入总量应为 hit+miss=100，实际 {d['input_tokens']}"
    assert d["total_tokens"] == 110, f"总量应为 110，实际 {d['total_tokens']}"
    assert d["cache_hit_rate"] == 25.0, f"缓存命中率应为 25.0%，实际 {d['cache_hit_rate']}"
    assert d["cost_cny"] == usage.cost_cny(u)
    assert usage.describe(usage.empty("deepseek-flash"))["cache_hit_rate"] == 0.0, \
        "没有输入 token 时命中率应为 0，不应除以 0"


# ------------------------------------------------------------------- Recorder


def test_recorder_note_calls_on_update(tmp_path):
    seen: list[dict] = []
    rec = usage.Recorder("deepseek-flash", tmp_path, on_update=seen.append)
    rec.note("deepseek-flash",
             {"prompt_tokens": 100, "completion_tokens": 10,
              "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60},
             when=cn(2026, 1, 7, 10, 0))

    assert len(seen) == 1, f"note() 应触发一次 on_update，实际 {len(seen)} 次"
    assert seen[0]["calls"] == 1 and seen[0]["total_tokens"] == 110
    assert seen[0]["cost_cny"] > 0, "回调里的摘要应带上算好的费用"
    assert rec.usage["peak"] == {"hit": 40, "miss": 60, "out": 10}, \
        "北京时间周三 10:00 的调用应落进高峰桶"


def test_recorder_note_ignores_none_usage(tmp_path):
    seen: list[dict] = []
    rec = usage.Recorder("deepseek-flash", tmp_path, on_update=seen.append)
    rec.note("deepseek-flash", None, when=cn(2026, 1, 7, 10, 0))
    assert rec.usage["calls"] == 0, "raw_usage 为 None 时不应计一次调用"
    assert seen == [], "没记到东西就不该回调界面"


def test_recorder_flush_skips_disk_when_no_calls(tmp_path):
    workdir = tmp_path / "20240101-测试台-测试单集"
    workdir.mkdir()
    rec = usage.Recorder("deepseek-flash", workdir)

    snapshot = rec.flush()
    assert snapshot["calls"] == 0
    assert not (workdir / "usage.json").exists(), "没跑过 LLM 就不该落盘"
    assert usage.load(workdir) is None


def test_recorder_flush_writes_file_when_called(tmp_path):
    workdir = tmp_path / "20240101-测试台-测试单集"
    workdir.mkdir()
    rec = usage.Recorder("deepseek-flash", workdir)
    rec.note("deepseek-flash",
             {"prompt_tokens": 200, "completion_tokens": 20,
              "prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 150},
             when=cn(2026, 1, 10, 10, 0))          # 周六 → 空闲档
    rec.flush()

    path = workdir / "usage.json"
    assert path.exists(), "有调用时 flush() 应写入 usage.json"
    back = usage.load(workdir)
    assert back is not None, "刚写下的 usage.json 应能被 load() 读回来"
    assert back["calls"] == 1 and back["model"] == "deepseek-flash"
    assert back["miss_tokens"] == 150 and back["out_tokens"] == 20
    assert back["off"] == {"hit": 50, "miss": 150, "out": 20}, "周六的调用应落进空闲桶"


def test_recorder_without_workdir_json_still_serializable(tmp_path):
    rec = usage.Recorder("deepseek-flash", None)
    rec.note("deepseek-flash", {"prompt_tokens": 1, "completion_tokens": 1},
             when=cn(2026, 1, 7, 10, 0))
    snapshot = rec.flush()
    assert snapshot["calls"] == 1, "没有 workdir 只影响落盘，不影响计数"
    json.dumps(snapshot)                              # 返回值必须还能被 json 序列化


def test_recorder_started_seed_is_kept():
    rec = usage.Recorder("deepseek-flash", None,
                         started={"calls": 3, "miss_tokens": 100, "elapsed_s": 12.0})
    assert rec.usage["calls"] == 3 and rec.usage["miss_tokens"] == 100, \
        "续跑时应接着已有用量继续累加"


# ---------------------------------------------------------------------- 落盘


def test_save_load_roundtrip_and_missing_file(tmp_path):
    workdir = tmp_path / "ep"
    workdir.mkdir()
    assert usage.load(workdir) is None, "没有 usage.json 时应返回 None"

    u = make_usage("deepseek-flash", miss=10, out=1)
    usage.save(workdir, u)
    assert usage.path_for(workdir) == workdir / "usage.json"
    assert usage.load(workdir)["miss_tokens"] == 10


def test_load_tolerates_broken_json(tmp_path):
    workdir = tmp_path / "ep"
    workdir.mkdir()
    (workdir / "usage.json").write_text("{oops", encoding="utf-8")
    assert usage.load(workdir) is None, "坏 JSON 应返回 None，而不是抛异常"


# ---------------------------------------------------------------------- merge


def test_merge_sums_calls_tokens_elapsed_and_buckets():
    a = make_usage("deepseek-flash", hit=10, miss=90, out=5, peak=False)
    a["elapsed_s"] = 1.2
    b = make_usage("deepseek-v4-pro", hit=1, miss=2, out=3, peak=True)
    b["elapsed_s"] = 2.3

    total = usage.merge([a, b])
    assert total["calls"] == 2, f"calls 应相加为 2，实际 {total['calls']}"
    assert total["elapsed_s"] == pytest.approx(3.5), \
        f"elapsed_s 应相加为 3.5，实际 {total['elapsed_s']}"
    assert total["hit_tokens"] == 11 and total["miss_tokens"] == 92 and total["out_tokens"] == 8
    assert total["off"] == {"hit": 10, "miss": 90, "out": 5}, f"空闲桶相加错误：{total['off']}"
    assert total["peak"] == {"hit": 1, "miss": 2, "out": 3}, f"高峰桶相加错误：{total['peak']}"


def test_merge_ignores_empty_entries():
    total = usage.merge([None, {}, make_usage("deepseek-flash", miss=5)])
    assert total["calls"] == 1 and total["miss_tokens"] == 5, \
        "空的/None 的用量应被跳过，不能把计数带偏"
    assert usage.merge([])["calls"] == 0 and usage.merge([])["miss_tokens"] == 0


# --------------------------------------------------------------- summary_over


def test_summary_over_counts_episodes_and_total_cost(tmp_path):
    root = tmp_path / "output"
    root.mkdir()

    ep1 = root / "ep1"; ep1.mkdir()
    u1 = make_usage("deepseek-flash", miss=1_000_000, out=1_000_000)               # 5.0 元
    u1["elapsed_s"] = 2.0
    usage.save(ep1, u1)
    ep2 = root / "ep2"; ep2.mkdir()
    u2 = make_usage("deepseek-flash", miss=1_000_000)                              # 1.0 元
    u2["elapsed_s"] = 0.5
    usage.save(ep2, u2)

    (root / "ep3-还没跑过").mkdir()                                   # 没有 usage.json，不算一集
    (root / "散文件.txt").write_text("x", encoding="utf-8")           # 非目录，忽略

    s = usage.summary_over(root)
    assert s["episodes"] == 2, f"只应统计有 usage.json 的两集，实际 {s['episodes']}"
    assert s["calls"] == 2, f"总调用次数应为 2，实际 {s['calls']}"
    assert s["elapsed_s"] == pytest.approx(2.5), f"耗时应按集相加，实际 {s['elapsed_s']}"
    assert s["cost_cny"] == pytest.approx(6.0), \
        f"总费用应为 5.0 + 1.0 = 6.0 元，实际 {s['cost_cny']}"
    assert s["miss_tokens"] == 2_000_000 and s["out_tokens"] == 1_000_000


def test_summary_over_splits_by_model(tmp_path):
    root = tmp_path / "output"
    root.mkdir()

    flash_dir = root / "用-flash-的一集"; flash_dir.mkdir()
    usage.save(flash_dir, make_usage("deepseek-flash", miss=1_000_000, out=1_000_000))
    pro_dir = root / "用-pro-的一集"; pro_dir.mkdir()
    usage.save(pro_dir, make_usage("deepseek-v4-pro", miss=1_000_000))

    s = usage.summary_over(root)
    assert set(s["by_model"]) == {"deepseek-flash", "deepseek-v4-pro"}, \
        f"应按模型分组，实际分组：{sorted(s['by_model'])}"
    flash = s["by_model"]["deepseek-flash"]
    assert flash["calls"] == 1 and flash["miss_tokens"] == 1_000_000 and flash["out_tokens"] == 1_000_000, \
        f"flash 分组应只含 flash 那一集的 token：{flash}"
    pro = s["by_model"]["deepseek-v4-pro"]
    assert pro["calls"] == 1 and pro["miss_tokens"] == 1_000_000 and pro["out_tokens"] == 0, \
        f"pro 分组不该混进 flash 的 token：{pro}"
    assert s["episodes"] == 2, f"两集都应被算进来，实际 {s['episodes']}"


def test_summary_over_by_model_uses_that_models_prices(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    pro_dir = root / "pro"; pro_dir.mkdir()
    usage.save(pro_dir, make_usage("deepseek-v4-pro", miss=1_000_000))   # pro 空闲档 = 4.5 元

    pro = usage.summary_over(root)["by_model"]["deepseek-v4-pro"]
    assert pro["model"] == "deepseek-v4-pro", f"分组里应保留模型名，实际 {pro['model']}"
    assert pro["cost_cny"] == pytest.approx(4.5), \
        f"pro 分组应按 pro 的价格算（4.5 元），实际 {pro['cost_cny']}"


def test_summary_over_mixed_models_total_equals_sum_of_parts(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    a = root / "flash"; a.mkdir()
    usage.save(a, make_usage("deepseek-flash", miss=1_000_000, out=1_000_000))     # 5.0 元
    b = root / "pro"; b.mkdir()
    usage.save(b, make_usage("deepseek-v4-pro", miss=1_000_000))                   # 4.5 元

    s = usage.summary_over(root)
    assert s["cost_cny"] == pytest.approx(9.5), \
        f"顶层总费用应等于各模型费用之和 5.0 + 4.5 = 9.5 元，实际 {s['cost_cny']}"


def test_summary_over_missing_root_is_zero(tmp_path):
    s = usage.summary_over(tmp_path / "根本没有这个目录")
    assert s["episodes"] == 0 and s["calls"] == 0, "目录不存在时应返回全 0，而不是抛异常"
    assert s["cost_cny"] == 0.0 and s["by_model"] == {}


def test_summary_over_skips_broken_usage_file(tmp_path):
    root = tmp_path / "output"
    (root / "好的一集").mkdir(parents=True)
    usage.save(root / "好的一集", make_usage("deepseek-flash", miss=1_000_000))
    broken = root / "坏的一集"
    broken.mkdir()
    (broken / "usage.json").write_text("{oops", encoding="utf-8")

    s = usage.summary_over(root)
    assert s["episodes"] == 1, f"读不出来的 usage.json 不应被算成一集，实际 {s['episodes']}"
    assert s["cost_cny"] == pytest.approx(1.0)


# ------------------------------------------------------- 模块级记录器生命周期


def test_module_level_start_note_stop_lifecycle(tmp_path, monkeypatch):
    workdir = tmp_path / "out"
    workdir.mkdir()
    monkeypatch.setattr(usage, "_active", None)      # 不继承别的测试留下的全局状态
    try:
        assert usage.current() is None
        rec = usage.start("deepseek-flash", workdir)
        assert usage.current() is rec, "start() 之后 current() 应返回新建的记录器"

        usage.note("deepseek-flash",
                   {"prompt_tokens": 100, "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60},
                   when=cn(2026, 1, 7, 10, 0))
        assert rec.usage["calls"] == 1, "start() 之后 note() 应能进账"
        assert rec.usage["miss_tokens"] == 60

        stopped = usage.stop()
        assert stopped["calls"] == 1, "stop() 应返回本次任务的用量快照"
        assert usage.current() is None, "stop() 之后没有活动记录器"
        assert (workdir / "usage.json").exists(), "stop() 应把结果落盘"

        usage.note("deepseek-flash", {"prompt_tokens": 1, "completion_tokens": 1},
                   when=cn(2026, 1, 7, 10, 0))       # 不应抛异常
        assert usage.current() is None, "stop() 之后再 note() 不应把记录器又激活"
    finally:
        usage.stop()                                 # 无论如何都不把全局状态留给其他测试


def test_module_level_note_without_start_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "_active", None)
    try:
        usage.note("deepseek-flash", {"prompt_tokens": 10, "completion_tokens": 1})
        assert usage.current() is None, "没有活动记录器时 note() 应静默忽略"
        assert usage.stop()["calls"] == 0, "没有活动记录器时 stop() 应返回空用量"
    finally:
        usage.stop()
