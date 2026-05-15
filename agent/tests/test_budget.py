from datetime import datetime, timezone

from app.budget import TokenUsage, estimate_deepseek_cost, month_key, today_key, ttl_epoch, usage_from_pydantic_ai


def test_stable_date_keys() -> None:
    now = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    assert today_key(now) == "2026-05-13"
    assert month_key(now) == "2026-05"


def test_ttl_epoch_adds_days() -> None:
    now = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
    assert ttl_epoch(1, now) == int(datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc).timestamp())


def test_deepseek_cost_accounts_for_cache_hits() -> None:
    usage = TokenUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=900)
    cost = estimate_deepseek_cost(
        usage,
        input_cache_miss_per_1m=0.14,
        input_cache_hit_per_1m=0.014,
        output_per_1m=0.28,
    )
    assert cost == cost.__class__("0.0001666")


def test_usage_from_generic_object() -> None:
    class Usage:
        input_tokens = 10
        output_tokens = 3
        cache_read_tokens = 4

    assert usage_from_pydantic_ai(Usage()) == TokenUsage(input_tokens=10, output_tokens=3, cache_read_tokens=4)
