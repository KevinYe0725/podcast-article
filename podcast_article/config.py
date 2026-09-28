"""配置读取：环境变量 + 项目根目录 .env（不引入额外依赖）。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_LLM_MODEL = "deepseek-flash"  # 见 api-docs.deepseek.com/guides/thinking_mode
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

_ACCOUNT_LLM: ContextVar[dict | None] = ContextVar("podcast_account_llm", default=None)


@contextmanager
def account_llm_override(*, api_key: str, base_url: str, model: str):
    """Scope a user's own OpenAI-compatible API to the current request/thread."""
    token = _ACCOUNT_LLM.set({
        "api_key": str(api_key or "").strip(),
        "base_url": str(base_url or "").strip().rstrip("/"),
        "model": str(model or "").strip(),
    })
    try:
        yield
    finally:
        _ACCOUNT_LLM.reset(token)


def uses_platform_llm_api() -> bool:
    current = _ACCOUNT_LLM.get()
    return not bool(current and current.get("api_key"))


def llm_base_url() -> str:
    current = _ACCOUNT_LLM.get()
    if current and current.get("api_key"):
        return current["base_url"]
    return DEEPSEEK_BASE_URL


def llm_connection() -> tuple[str, str]:
    """Return the active API key and base URL without exposing them to the UI."""
    return deepseek_api_key(), llm_base_url()


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
    current = _ACCOUNT_LLM.get()
    if current and current.get("api_key"):
        return current["api_key"]
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key or key.startswith("sk-xxxx"):
        raise MissingKeyError(
            "未找到 DEEPSEEK_API_KEY。请在项目根目录复制 .env.example 为 .env 并填入 key，"
            "或 export DEEPSEEK_API_KEY=sk-...（https://platform.deepseek.com 创建）"
        )
    return key


def deepseek_model() -> str:
    current = _ACCOUNT_LLM.get()
    if current and current.get("api_key") and current.get("model"):
        return current["model"]
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
