"""配置读取：环境变量 + 项目根目录 .env（不引入额外依赖）。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_LLM_MODEL = "deepseek-flash"  # 见 api-docs.deepseek.com/guides/thinking_mode
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def _load_dotenv() -> None:
    """把 PROJECT_ROOT/.env 里的 KEY=VALUE 注入 os.environ（已存在的优先）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key in {"PA_USER_SECRETS_KEY", "PA_USER_SECRETS_PREVIOUS_KEYS"}:
            continue  # supplied only by the host service environment, never project .env
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class MissingKeyError(RuntimeError):
    pass


def deepseek_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key or key.startswith("sk-xxxx"):
        raise MissingKeyError(
            "未找到 DEEPSEEK_API_KEY。请在项目根目录复制 .env.example 为 .env 并填入 key，"
            "或 export DEEPSEEK_API_KEY=sk-...（https://platform.deepseek.com 创建）"
        )
    return key


def deepseek_model() -> str:
    return os.environ.get("DEEPSEEK_MODEL", DEFAULT_LLM_MODEL)


# ---------------------------------------------------------------- Notion

def notion_token() -> str:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        raise MissingKeyError(
            "未找到 NOTION_TOKEN。请在 .env 中配置（Notion → Integrations → 创建 integration，"
            "复制 Internal API Secret，并把目标页面/数据库与该 integration 连接）"
        )
    return token


def notion_database_id() -> str | None:
    """给了数据库 id 就写入数据库（作为一行）；否则用父页面（作为子页面）。"""
    return os.environ.get("NOTION_DATABASE_ID", "").strip() or None


def notion_parent_page_id() -> str | None:
    return os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or None
