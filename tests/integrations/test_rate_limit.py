from __future__ import annotations

from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase, override_settings

from escrow.integrations.rate_limit import TOKEN_BUCKET_SCRIPT, check_api_key_rate_limit


class RedisRateLimitTests(SimpleTestCase):
    @override_settings(
        API_KEY_RATE_LIMIT_MAX=100,
        API_KEY_RATE_LIMIT_WINDOW_SECONDS=60,
        API_KEY_RATE_LIMIT_BURST=20,
    )
    @patch("escrow.integrations.rate_limit.redis.Redis.from_url")
    def test_atomic_token_bucket_uses_adr_rate_and_burst(
        self,
        from_url: MagicMock,
    ) -> None:
        client = MagicMock()
        client.eval.side_effect = [[1, 0], [0, 1]]
        from_url.return_value = client

        allowed = check_api_key_rate_limit("key-123", now_timestamp=120)
        denied = check_api_key_rate_limit("key-123", now_timestamp=121)

        assert allowed.allowed
        assert not denied.allowed
        assert denied.retry_after_seconds == 1
        assert client.eval.call_args_list == [
            call(
                TOKEN_BUCKET_SCRIPT,
                1,
                "rate-limit:api-key:key-123",
                "120",
                str(100 / 60),
                "120",
            ),
            call(
                TOKEN_BUCKET_SCRIPT,
                1,
                "rate-limit:api-key:key-123",
                "120",
                str(100 / 60),
                "121",
            ),
        ]
        client.incr.assert_not_called()
