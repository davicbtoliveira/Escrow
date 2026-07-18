from __future__ import annotations

import json
from unittest.mock import patch

from django.test import SimpleTestCase

from escrow.integrations.rate_limit import RateLimitDecision
from escrow.logging import JsonFormatter


class CorrelationLoggingTests(SimpleTestCase):
    def test_request_log_keeps_correlation_but_never_the_query_secret(self) -> None:
        secret = "esk_do-not-log-this-secret"
        with self.assertLogs("escrow.request", level="INFO") as captured:
            response = self.client.get(
                f"/api/v1/integrations/organization/?api_key={secret}",
                headers={"X-Correlation-ID": "portfolio-log-0001"},
            )

        payload = json.loads(JsonFormatter().format(captured.records[-1]))

        assert response.status_code == 401
        assert payload["correlation_id"] == "portfolio-log-0001"
        assert payload["http_path"] == "/api/v1/integrations/organization/"
        assert payload["event"] == "http_request_completed"
        assert secret not in json.dumps(payload)

    @patch(
        "escrow.agreements.views.check_public_checkout_rate_limit",
        return_value=RateLimitDecision(allowed=True, retry_after_seconds=0),
    )
    @patch("escrow.agreements.views.find_checkout_agreement", return_value=None)
    def test_checkout_capability_is_redacted_from_request_logs(
        self,
        _: object,
        __: object,
    ) -> None:
        checkout_token = "chk_do-not-log-this-checkout-capability"
        with self.assertLogs("django.request", level="WARNING") as django_captured:
            with self.assertLogs("escrow.request", level="INFO") as captured:
                response = self.client.get(
                    f"/api/v1/checkout/{checkout_token}/?token={checkout_token}"
                )

        payload = json.loads(JsonFormatter().format(captured.records[-1]))

        assert response.status_code == 404
        assert payload["http_path"] == "/api/v1/checkout/[redacted]/"
        assert checkout_token not in json.dumps(payload)
        assert checkout_token not in "\n".join(django_captured.output)
