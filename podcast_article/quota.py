"""Race-safe account quota helpers for server-paid model and ASR work."""
from __future__ import annotations

import math
import os
import secrets
from datetime import datetime
from decimal import Decimal, ROUND_UP
from zoneinfo import ZoneInfo

from . import usage
from .platform_store import PlatformStore, QuotaExceeded, Reservation

PRICE_TABLE_VERSION = "deepseek-2026-09-v1"

class UnknownModelPrice(ValueError):
    def __init__(self, model: str):
        self.model = model
        super().__init__(f"no quota price configured for model {model!r}")


def month_key(now: datetime | None = None) -> str:
    """Return the current quota month in Asia/Shanghai as YYYY-MM."""
    zone = ZoneInfo("Asia/Shanghai")
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    else:
        current = current.astimezone(zone)
    return current.strftime("%Y-%m")


def quote_llm_upper_bound(model: str, prompt_utf8_bytes: int, max_tokens: int) -> Decimal:
    """Conservative CNY quote: every prompt byte is an input token at peak price."""
    prices = usage.MODEL_PRICES.get((model or "").strip())
    if prices is None:
        raise UnknownModelPrice(model or "")
    if not isinstance(prompt_utf8_bytes, int) or isinstance(prompt_utf8_bytes, bool) or prompt_utf8_bytes < 0:
        raise ValueError("prompt_utf8_bytes must be a non-negative integer")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 0:
        raise ValueError("max_tokens must be a non-negative integer")
    million = Decimal(1_000_000)
    peak_input = max(Decimal(str(v)) for v in prices["miss"])
    peak_output = max(Decimal(str(v)) for v in prices["out"])
    quote = (Decimal(prompt_utf8_bytes) * peak_input + Decimal(max_tokens) * peak_output) / million
    return quote.quantize(Decimal("0.00000001"), rounding=ROUND_UP)


def check_cache_capacity(*, used_bytes: int, requested_bytes: int, limit_bytes: int | None) -> None:
    if min(used_bytes, requested_bytes) < 0:
        raise ValueError("cache byte counts must be non-negative")
    if limit_bytes is not None and used_bytes + requested_bytes > limit_bytes:
        raise QuotaExceeded("cache_bytes", limit_bytes, used_bytes, 0, requested_bytes)


def check_upload_size(*, size_bytes: int, limit_bytes: int | None) -> None:
    if size_bytes < 0:
        raise ValueError("upload size must be non-negative")
    if limit_bytes is not None and size_bytes > limit_bytes:
        raise QuotaExceeded("max_upload_bytes", limit_bytes, 0, 0, size_bytes)


def asr_reservation_seconds(duration_seconds, *, unknown_duration_max: int | None = None) -> int:
    """Reserve source duration, or the configured maximum when metadata has no duration."""
    maximum = unknown_duration_max
    if maximum is None:
        try:
            maximum = int(os.environ.get("PA_ASR_MAX_JOB_SECONDS", "7200"))
        except ValueError:
            maximum = 7200
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        raise ValueError("unknown duration requires a positive PA_ASR_MAX_JOB_SECONDS")
    try:
        duration = float(duration_seconds) if duration_seconds is not None else 0.0
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if math.isfinite(duration) and duration > 0:
        seconds = math.ceil(duration)
        if seconds > maximum:
            raise QuotaExceeded("asr_job_seconds", maximum, 0, 0, seconds)
        return max(1, seconds)
    return maximum


class LLMQuotaGuard:
    """Reserve before a provider call and settle from returned token usage."""

    def __init__(self, store: PlatformStore, owner_id: str, month: str | None = None):
        self.store = store
        self.owner_id = owner_id
        self.month = month

    def _month(self) -> str:
        # Resolve at each provider call so long-running tasks crossing local midnight
        # on month end are attributed to the month in which the work is reserved.
        return self.month or month_key()

    def reserve_llm_call(self, model: str, system: str, user: str, *, max_tokens: int,
                         history: list[dict] | None = None) -> Reservation:
        prompt_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        message_count = 2
        for turn in history or []:
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                content = turn.get("content")
                if isinstance(content, str) and content.strip():
                    prompt_bytes += len(content.encode("utf-8"))
                    message_count += 1
        # Include conservative room for serialized message roles and framing tokens.
        prompt_bytes += 64 * message_count
        quote = quote_llm_upper_bound(model, prompt_bytes, max_tokens)
        return self.store.reserve_llm_call(
            self.owner_id, secrets.token_hex(16), self._month(), quote,
            price_table_version=PRICE_TABLE_VERSION,
        )

    def settle_llm_call(self, reservation: Reservation, model: str, raw_usage=None) -> Reservation:
        if raw_usage is None:
            # A provider may fail after accepting the request; charge the reserved upper bound.
            actual = Decimal(reservation.reserved_amount)
        else:
            part = usage.split_usage(raw_usage)
            peak = usage.is_peak()
            bucket_index = 1 if peak else 0
            prices = usage.MODEL_PRICES.get(model)
            if prices is None:
                raise UnknownModelPrice(model)
            actual = sum(
                Decimal(part[name]) * Decimal(str(prices[price_name][bucket_index])) / Decimal(1_000_000)
                for name, price_name in (("hit", "hit"), ("miss", "miss"), ("out", "out"))
            )
        return self.store.settle_reservation(reservation.id, actual)

    def reserve_asr(self, job_id: str, audio_seconds: int) -> Reservation:
        return self.store.reserve_asr(self.owner_id, job_id, self._month(), audio_seconds)

    def resize_asr(self, reservation: Reservation, audio_seconds: int) -> Reservation:
        return self.store.resize_asr_reservation(reservation.id, audio_seconds)

    def settle_asr(self, reservation: Reservation, actual_seconds: int) -> Reservation:
        return self.store.settle_reservation(reservation.id, actual_seconds)

    def release(self, reservation: Reservation) -> Reservation:
        return self.store.release_reservation(reservation.id)
