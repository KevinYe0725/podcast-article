"""Resolve per-account external API overrides without exposing secret values."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass(frozen=True)
class AccountServices:
    account_id: str
    llm_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_model: str = ""
    asr_key: str = field(default="", repr=False)
    asr_base_url: str = ""
    asr_model: str = ""
    search_provider: str = "default"
    search_keys: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def uses_personal_llm(self) -> bool:
        return bool(self.llm_key)

    @property
    def uses_personal_asr(self) -> bool:
        return bool(self.asr_key)

    def search_key_map(self) -> dict[str, str]:
        return dict(self.search_keys)


def resolve(account_id: str, settings: dict, secrets: dict[str, str]) -> AccountServices:
    services = settings.get("services") or {}
    assistant = settings.get("assistant") or {}
    llm_key = str(secrets.get("LLM_API_KEY") or "").strip()
    asr_key = str(secrets.get("ASR_API_KEY") or "").strip()
    if services.get("llm_mode") != "personal":
        llm_key = ""
    if services.get("asr_mode") != "personal":
        asr_key = ""
    search_keys = tuple(
        (provider, str(secrets.get(secret_name) or "").strip())
        for provider, secret_name in (("tavily", "TAVILY_API_KEY"), ("serper", "SERPER_API_KEY"))
        if secrets.get(secret_name)
    )
    provider = str(assistant.get("search_provider") or "default").strip().lower()
    if provider not in {"default", "bing", "brave", "tavily", "serper"}:
        provider = "default"
    return AccountServices(
        account_id=str(account_id),
        llm_key=llm_key,
        llm_base_url=str(services.get("llm_base_url") or "https://api.openai.com/v1").strip(),
        llm_model=str(services.get("llm_model") or "gpt-4o-mini").strip(),
        asr_key=asr_key,
        asr_base_url=str(services.get("asr_base_url") or "https://dashscope.aliyuncs.com").strip(),
        asr_model=str(services.get("asr_model") or "paraformer-v2").strip(),
        search_provider=provider,
        search_keys=search_keys,
    )


@contextmanager
def activate(services: AccountServices) -> Iterator[AccountServices]:
    """Apply account-specific credentials only within the current request/thread."""
    from . import cloud_asr, config, websearch

    with ExitStack() as stack:
        if services.uses_personal_llm:
            stack.enter_context(config.account_llm_override(
                api_key=services.llm_key,
                base_url=services.llm_base_url,
                model=services.llm_model,
            ))
        if services.uses_personal_asr:
            stack.enter_context(cloud_asr.account_asr_override(
                api_key=services.asr_key,
                base_url=services.asr_base_url,
                model=services.asr_model,
            ))
        stack.enter_context(websearch.account_search_override(
            account_id=services.account_id,
            provider=services.search_provider,
            keys=services.search_key_map(),
        ))
        yield services
