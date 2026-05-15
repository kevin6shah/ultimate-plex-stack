from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def today_key(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%d")


def month_key(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.strftime("%Y-%m")


def ttl_epoch(days: int, now: datetime | None = None) -> int:
    value = now or datetime.now(timezone.utc)
    return int((value + timedelta(days=days)).timestamp())


def estimate_deepseek_cost(usage: TokenUsage, *, input_cache_miss_per_1m: float, input_cache_hit_per_1m: float, output_per_1m: float) -> Decimal:
    uncached_input = max(usage.input_tokens - usage.cache_read_tokens, 0)
    input_cost = (Decimal(uncached_input) * Decimal(str(input_cache_miss_per_1m))) / Decimal(1_000_000)
    cache_hit_cost = (Decimal(usage.cache_read_tokens) * Decimal(str(input_cache_hit_per_1m))) / Decimal(1_000_000)
    output_cost = (Decimal(usage.output_tokens) * Decimal(str(output_per_1m))) / Decimal(1_000_000)
    return input_cost + cache_hit_cost + output_cost


def usage_from_pydantic_ai(raw_usage: Any) -> TokenUsage:
    if raw_usage is None:
        return TokenUsage()
    data = raw_usage if isinstance(raw_usage, dict) else vars(raw_usage)

    def value(*names: str) -> int:
        for name in names:
            if isinstance(data, dict) and data.get(name) is not None:
                return int(data[name])
            if hasattr(raw_usage, name):
                return int(getattr(raw_usage, name))
        return 0

    input_tokens = value("input_tokens", "request_tokens", "prompt_tokens")
    output_tokens = value("output_tokens", "response_tokens", "completion_tokens")
    cache_read_tokens = value("cache_read_tokens", "cached_tokens")
    cache_write_tokens = value("cache_write_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )
