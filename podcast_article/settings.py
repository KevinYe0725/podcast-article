"""设置存储：个人信息与生成默认值存 settings.json，密钥与目标位置写回 .env。

设计约定（参考 DeepSeek Harness 的设置层）：
- 密钥只写不读：接口只回报「是否已配置」与打码值，明文永不返回前端
- 提交空字符串 = 保持原值，不会被清空
- 改 .env 时保留原有注释与顺序
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import PROJECT_ROOT

SETTINGS_PATH = PROJECT_ROOT / "settings.json"
ENV_PATH = PROJECT_ROOT / ".env"

# 设置页有权写入的 .env 键
MANAGED_KEYS = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "NOTION_TOKEN",
    "NOTION_DATABASE_ID",
    "NOTION_PARENT_PAGE_ID",
)

PROFILE_DEFAULTS: dict = {
    "name": "",
    "interests": "",
    "language": "zh",  # 文章输出语言
}

GENERATION_DEFAULTS: dict = {
    "language": "",      # 转写语言，空 = 自动检测
    "backend": "mlx",    # mlx / faster-whisper
    "asr_model": "",
    "llm_model": "",
    "max_chars": 75_000,
    "no_subs": False,
    "length_mode": "standard",  # concise（3-4 分钟精华）/ standard / deep
    "outline_mode": True,       # 大纲 + 逐节写作（篇幅可控）
    "auto_polish": False,       # 生成后再让模型自查一遍（可选，格式纪律已由确定性后处理保证）
}

SUBSCRIPTION_DEFAULTS: dict = {
    "enabled": True,           # 后台是否按间隔检查订阅
    "interval_minutes": 120,   # 多久检查一次（0 表示不自动检查）
    "auto_generate": True,     # 发现新单集时自动排队生成文章
    "auto_dest": "",           # 自动生成的文章归到哪个分类（空 = 未分类）
}

DEFAULTS: dict = {
    "profile": PROFILE_DEFAULTS,
    "generation": GENERATION_DEFAULTS,
    "subscriptions": SUBSCRIPTION_DEFAULTS,
}


def load() -> dict:
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    merged = {
        "profile": {**PROFILE_DEFAULTS, **(data.get("profile") or {})},
        "generation": {**GENERATION_DEFAULTS, **(data.get("generation") or {})},
        "subscriptions": {**SUBSCRIPTION_DEFAULTS, **(data.get("subscriptions") or {})},
    }
    return merged


def save(profile: dict | None = None, generation: dict | None = None,
         subscriptions: dict | None = None) -> dict:
    current = load()
    if profile:
        current["profile"].update({k: v for k, v in profile.items() if k in PROFILE_DEFAULTS})
    if generation:
        for k, v in generation.items():
            if k in GENERATION_DEFAULTS:
                current["generation"][k] = v
    if subscriptions:
        for k, v in subscriptions.items():
            if k in SUBSCRIPTION_DEFAULTS:
                current["subscriptions"][k] = v
    if isinstance(current["generation"].get("max_chars"), str):
        try:
            current["generation"]["max_chars"] = int(current["generation"]["max_chars"])
        except ValueError:
            current["generation"]["max_chars"] = GENERATION_DEFAULTS["max_chars"]
    for key in ("interval_minutes",):
        if isinstance(current["subscriptions"].get(key), str):
            try:
                current["subscriptions"][key] = int(current["subscriptions"][key] or 0)
            except ValueError:
                current["subscriptions"][key] = SUBSCRIPTION_DEFAULTS[key]
    SETTINGS_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return current


def profile_text() -> str:
    """把个人资料转成注入 prompt 的一段文字；没填则返回空串。"""
    p = load()["profile"]
    bits = []
    if p.get("name"):
        bits.append(f"读者称呼：{p['name']}")
    if p.get("interests"):
        bits.append(f"读者长期关注的方向：{p['interests']}")
    if not bits:
        return ""
    return (
        "【读者画像】" + "；".join(bits) + "。"
        "在不偏离节目原意的前提下，优先保留与读者关注方向相关的内容与细节，"
        "并在「编辑点评」里多从这些角度给出可迁移的启发。"
    )


# ------------------------------------------------------------------ .env

def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def update_env(updates: dict[str, str]) -> list[str]:
    """写入 .env：已存在的键就地替换，缺失的追加到文件末尾。返回实际改动的键。

    值传空字符串表示「保持原值」，不会清空已有配置。
    """
    changed = [k for k, v in updates.items() if k in MANAGED_KEYS and str(v).strip()]
    if not changed or not ENV_PATH.exists():
        return []
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    pending = {k: str(updates[k]).strip() for k in changed}
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k in pending:
            lines[i] = f"{k}={pending.pop(k)}"
    for k, v in pending.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def mask(value: str) -> str:
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "•••"


def secret_status() -> dict:
    env = read_env()
    out = {}
    for key, label in (
        ("DEEPSEEK_API_KEY", "DeepSeek API Key"),
        ("NOTION_TOKEN", "Notion Token"),
        ("NOTION_DATABASE_ID", "Notion 数据库 ID"),
        ("NOTION_PARENT_PAGE_ID", "Notion 父页面 ID"),
        ("DEEPSEEK_MODEL", "DeepSeek 模型"),
    ):
        value = env.get(key, "")
        out[key] = {"label": label, "configured": bool(value), "masked": mask(value)}
    return out


def storage_info() -> dict:
    # 与 webapp 一致：PA_OUTPUT_DIR 优先（测试与多实例部署都会用到）
    output = Path(os.environ.get("PA_OUTPUT_DIR") or (PROJECT_ROOT / "output"))
    episodes, size = 0, 0
    if output.exists():
        for d in output.iterdir():
            if d.is_dir():
                episodes += 1
                size += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    models_dir = Path.home() / ".cache" / "podcast-article" / "models"
    models = sorted(p.name for p in models_dir.iterdir()) if models_dir.exists() else []
    return {
        "output_dir": str(output),
        "episodes": episodes,
        "size_mb": round(size / 1048576, 1),
        "models": models,
        "settings_path": str(SETTINGS_PATH),
        "env_path": str(ENV_PATH),
    }
