"""token 用量与费用记账。

为什么要单独记账：逐节写作会发起 8-12 次调用，用户最关心的两个问题是
「这一篇花了多少钱」和「钱花在哪一步」。DeepSeek 的流式响应在最后一个 chunk
里带 usage（需要显式 stream_options={"include_usage": True}），把它累加起来即可，
不需要任何估算。

DeepSeek 官方价目以 USD / 百万 tokens 公布：
https://api-docs.deepseek.com/zh-cn/quick_start/pricing
配额按 CNY 记账，使用固定的保守规划汇率 7.5 CNY/USD；这比 2026-09-23
人民币汇率中间价 6.7468 高约 11%，为汇率波动留出余量。该换算价不是供应商结算价，
发放生产邀请前应复核模型价格和换算假设。

    deepseek-flash    输入(缓存命中) 0.0225 / 输入(未命中) 1.125 / 输出 4.5
    deepseek-v4-pro   输入(缓存命中) 0.165  / 输入(未命中) 4.95  / 输出 14.85

注意官方是**分时段计价**：高峰时段（北京时间周一至周五 9:00-12:00、14:00-18:00）
是空闲时段的两倍，其余时间（含周末全天）算空闲。所以记账不是存一个单价，
而是把命中/未命中/输出三类 token 分别落进「高峰」与「空闲」两个桶，
费用在展示时再按价格表算 —— 这样以后调价或用户改单价，历史数据不用重算。

运行时状态：`summarize._chat` 每次拿到 usage 就调 `note()`。Web 请求显式传入自己的
`Recorder`；模块级活动记录器只保留给没有显式 recorder 的本地 CLI / 兼容调用。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 固定的保守规划换算率，不代表 DeepSeek 的实际结算汇率。
USD_CNY_PLANNING_RATE = 7.5

# 元 / 百万 tokens：(空闲时段, 高峰时段)。由官方 USD 价格乘规划换算率得出。
MODEL_PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek-flash": {
        "hit": (0.0225, 0.045),   # 输入·缓存命中
        "miss": (1.125, 2.25),    # 输入·缓存未命中
        "out": (4.5, 9.0),        # 输出
    },
    "deepseek-v4-pro": {
        "hit": (0.165, 0.33),
        "miss": (4.95, 9.9),
        "out": (14.85, 29.7),
    },
}
FALLBACK_MODEL = "deepseek-flash"
FALLBACK_PRICES = MODEL_PRICES[FALLBACK_MODEL]

_CN_TZ = timezone(timedelta(hours=8))   # 官方提到的时段都是北京时间
PEAK_HOURS = ((9, 12), (14, 18))
USAGE_FILE = "usage.json"

_BUCKETS = ("hit", "miss", "out")


def is_peak(when: datetime | None = None) -> bool:
    """是否处于高峰时段：北京时间周一至周五 9-12 点、14-18 点。"""
    now = (when or datetime.now(timezone.utc)).astimezone(_CN_TZ)
    if now.weekday() >= 5:          # 周末全天算空闲
        return False
    return any(start <= now.hour < end for start, end in PEAK_HOURS)


def prices_for(model: str | None) -> dict[str, tuple[float, float]]:
    """取某模型的价格；未知模型（例如用户自己填了别的名字）退回 flash 的价格。"""
    return MODEL_PRICES.get((model or "").strip(), FALLBACK_PRICES)


def empty(model: str | None = None) -> dict:
    return {
        "model": model or FALLBACK_MODEL,
        "calls": 0,
        "hit_tokens": 0,
        "miss_tokens": 0,
        "out_tokens": 0,
        "peak": {k: 0 for k in _BUCKETS},
        "off": {k: 0 for k in _BUCKETS},
        "elapsed_s": 0.0,
    }


# ------------------------------------------------------------------ 单次调用


def split_usage(raw) -> dict:
    """把 DeepSeek 的 usage 对象拆成命中 / 未命中 / 输出三类 token。

    官方字段：prompt_tokens、completion_tokens、prompt_cache_hit_tokens、
    prompt_cache_miss_tokens。老版本响应可能没有缓存字段，此时全部算未命中
    （宁可高估费用，也不要虚报便宜）。
    """
    def get(name: str) -> int:
        value = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt = get("prompt_tokens")
    hit = get("prompt_cache_hit_tokens")
    miss = get("prompt_cache_miss_tokens")
    if not hit and not miss:
        miss = prompt          # 完全没有缓存字段的旧响应：全部按未命中算
    elif not miss:
        # 只给了命中数：剩下的都算未命中（宁可高估费用，也不要虚报便宜）
        miss = max(0, prompt - hit)
    elif not hit and prompt:
        hit = max(0, prompt - miss)
    return {"hit": hit, "miss": miss, "out": get("completion_tokens")}


def add(target: dict, part: dict, peak: bool) -> None:
    """把一次调用的 token 累加进 usage 结构（原地修改）。"""
    bucket = "peak" if peak else "off"
    target.setdefault("peak", {k: 0 for k in _BUCKETS})
    target.setdefault("off", {k: 0 for k in _BUCKETS})
    for key in _BUCKETS:
        target[bucket][key] = target[bucket].get(key, 0) + int(part.get(key, 0))
        target[f"{key}_tokens"] = target.get(f"{key}_tokens", 0) + int(part.get(key, 0))
    target["calls"] = target.get("calls", 0) + 1


def cost_cny(usage: dict) -> float:
    """按价格表算费用（元）。三类 token 分别落在高峰/空闲桶里，所以能精确计价。"""
    price = prices_for(usage.get("model"))
    total = 0.0
    for bucket, idx in (("off", 0), ("peak", 1)):
        counts = usage.get(bucket) or {}
        for key in _BUCKETS:
            total += counts.get(key, 0) / 1_000_000 * price[key][idx]
    return round(total, 4)


def describe(usage: dict) -> dict:
    """给界面用的可读摘要。"""
    total_in = usage.get("hit_tokens", 0) + usage.get("miss_tokens", 0)
    return {
        **usage,
        "total_tokens": total_in + usage.get("out_tokens", 0),
        "input_tokens": total_in,
        "cost_cny": cost_cny(usage),
        "cache_hit_rate": round(usage.get("hit_tokens", 0) / total_in * 100, 1) if total_in else 0.0,
    }


# ------------------------------------------------------------------ 落盘


def path_for(workdir: Path) -> Path:
    return Path(workdir) / USAGE_FILE


def load(workdir: Path) -> dict | None:
    p = path_for(workdir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save(workdir: Path, usage: dict) -> None:
    path = path_for(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def merge(usages: list[dict], model: str | None = None) -> dict:
    """把多集（或多次运行）的用量合成一份总数。

    model 参数很重要：合并时若把 model 退回默认值，费用就会按 flash 的价格算，
    混用 pro 的历史数据会被严重低估。没显式给就用第一份里记着的模型。
    """
    total = empty(model)
    if not model:
        for u in usages:
            if u and u.get("model"):
                total["model"] = u["model"]
                break
    total["calls"] = 0
    total["elapsed_s"] = 0.0
    for u in usages:
        if not u:
            continue
        total["calls"] += u.get("calls", 0)
        total["elapsed_s"] = round(total["elapsed_s"] + u.get("elapsed_s", 0.0), 1)
        for key in _BUCKETS:
            total[f"{key}_tokens"] += u.get(f"{key}_tokens", 0)
        for bucket in ("peak", "off"):
            src = u.get(bucket) or {}
            for key in _BUCKETS:
                total[bucket][key] += src.get(key, 0)
    return total


# ------------------------------------------------------------------ 活动记录器


class Recorder:
    """一次任务的用量累加器；每次调用后回调 on_update，界面就能实时看到花费。"""

    def __init__(self, model: str, workdir: Path | None = None, on_update=None,
                 started: dict | None = None, before_save=None):
        self.usage = {**empty(model), **(started or {})}
        self.usage["model"] = model or FALLBACK_MODEL
        self.workdir = workdir
        self.on_update = on_update
        self.before_save = before_save
        self._t0 = time.time()
        self._lock = threading.Lock()

    def note(self, model: str | None, raw_usage, when: datetime | None = None) -> None:
        if raw_usage is None:
            return
        with self._lock:
            if model:
                self.usage["model"] = model
            add(self.usage, split_usage(raw_usage), is_peak(when))
            self.usage["elapsed_s"] = round(time.time() - self._t0, 1)
        if self.on_update:
            self.on_update(describe(self.usage))

    def flush(self) -> dict:
        """把结果写进 output/<episode>/usage.json。没跑过 LLM 就不落盘。"""
        with self._lock:
            self.usage["elapsed_s"] = round(time.time() - self._t0, 1)
            snapshot = json.loads(json.dumps(self.usage))
        if self.workdir and snapshot.get("calls"):
            guard = None
            if self.before_save:
                encoded = json.dumps(snapshot, ensure_ascii=False, indent=2)
                guard = self.before_save(path_for(self.workdir), len(encoded.encode("utf-8")))
            if guard is not None and hasattr(guard, "__enter__"):
                with guard:
                    save(self.workdir, snapshot)
            else:
                save(self.workdir, snapshot)
        return snapshot


_active_lock = threading.Lock()
_active: Recorder | None = None


def start(model: str, workdir: Path | None = None, on_update=None,
          started: dict | None = None) -> Recorder:
    """开始记录一次任务的用量（同一时刻只有一个活动记录器）。"""
    global _active
    with _active_lock:
        _active = Recorder(model, workdir, on_update, started)
        return _active


def current() -> Recorder | None:
    return _active


def note(model: str | None, raw_usage, when: datetime | None = None, *,
         recorder: Recorder | None = None) -> None:
    """记录到显式任务记录器；保留旧全局记录器供本地 CLI 兼容。"""
    rec = recorder if recorder is not None else _active
    if rec is not None:
        rec.note(model, raw_usage, when)


def stop() -> dict:
    global _active
    with _active_lock:
        rec, _active = _active, None
    return rec.flush() if rec else empty()


# ------------------------------------------------------------------ 汇总


def summary_over(root: Path) -> dict:
    """扫一遍输出目录，汇总所有文章的 token 与费用（供设置页「用量」展示）。"""
    usages: list[dict] = []
    episodes = 0
    if Path(root).exists():
        for d in Path(root).iterdir():
            if not d.is_dir():
                continue
            u = load(d)
            if u:
                episodes += 1
                usages.append(u)

    # 先按模型分组：一个 usage 结构只能有一个单价，混用 flash / pro 时
    # 必须分组算钱再加总，否则 pro 的 token 会被按 flash 的价格算（低估 4 倍以上）
    grouped: dict[str, list[dict]] = {}
    for u in usages:
        grouped.setdefault(u.get("model") or FALLBACK_MODEL, []).append(u)
    by_model = {m: describe(merge(v, model=m)) for m, v in sorted(grouped.items())}

    described = describe(merge(usages))
    described["model"] = next(iter(by_model)) if len(by_model) == 1 else "mixed"
    described["cost_cny"] = round(sum(d["cost_cny"] for d in by_model.values()), 4)
    described["episodes"] = episodes
    described["by_model"] = by_model
    return described
