from __future__ import annotations

import json

from django.test import SimpleTestCase

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
