"""Atomic Redis token-bucket enforcement for API-key requests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from hashlib import sha256
from typing import cast

import redis
from django.conf import settings


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


TOKEN_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens = tonumber(redis.call("HGET", KEYS[1], "tokens"))
local updated_at = tonumber(redis.call("HGET", KEYS[1], "updated_at"))

if tokens == nil or updated_at == nil then
  tokens = capacity
  updated_at = now
end

local elapsed = math.max(0, now - updated_at)
tokens = math.min(capacity, tokens + (elapsed * refill_per_second))
local allowed = tokens >= 1
local retry_after = 0

if allowed then
  tokens = tokens - 1
else
  retry_after = math.ceil((1 - tokens) / refill_per_second)
end

redis.call("HSET", KEYS[1], "tokens", tokens, "updated_at", now)
redis.call("EXPIRE", KEYS[1], math.ceil(capacity / refill_per_second) + 1)
return {allowed and 1 or 0, retry_after}
"""


def check_api_key_rate_limit(
    key_id: str,
    *,
    now_timestamp: float | None = None,
) -> RateLimitDecision:
    """Apply the ADR's atomic 100/minute token bucket with a 20-request burst."""
    return _check_token_bucket(
        redis_key=f"rate-limit:api-key:{key_id}",
        max_requests=settings.API_KEY_RATE_LIMIT_MAX,
        window_seconds=settings.API_KEY_RATE_LIMIT_WINDOW_SECONDS,
        burst=settings.API_KEY_RATE_LIMIT_BURST,
        now_timestamp=now_timestamp,
    )


def check_public_checkout_rate_limit(
    ip_address: str,
    *,
    now_timestamp: float | None = None,
) -> RateLimitDecision:
    """Apply the public-checkout quota without placing a raw IP in the Redis key."""
    return _check_token_bucket(
        redis_key=f"rate-limit:checkout:{sha256(ip_address.encode()).hexdigest()}",
        max_requests=settings.PUBLIC_CHECKOUT_RATE_LIMIT_MAX,
        window_seconds=settings.PUBLIC_CHECKOUT_RATE_LIMIT_WINDOW_SECONDS,
        burst=0,
        now_timestamp=now_timestamp,
    )


def _check_token_bucket(
    *,
    redis_key: str,
    max_requests: int,
    window_seconds: int,
    burst: int,
    now_timestamp: float | None,
) -> RateLimitDecision:
    now = time.time() if now_timestamp is None else now_timestamp
    refill_per_second = max_requests / window_seconds
    capacity = max_requests + burst
    client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
    result = cast(
        list[int],
        client.eval(
            TOKEN_BUCKET_SCRIPT,
            1,
            redis_key,
            str(capacity),
            str(refill_per_second),
            str(now),
        ),
    )
    return RateLimitDecision(allowed=bool(int(result[0])), retry_after_seconds=int(result[1]))
