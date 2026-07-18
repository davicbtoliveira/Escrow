from unittest.mock import Mock, patch

from django.test import TestCase


class HealthEndpointTests(TestCase):
    def test_liveness_reports_an_running_process(self) -> None:
        response = self.client.get("/health/live/")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @patch("escrow.health._database_is_available", return_value=True)
    @patch("escrow.health._rabbitmq_is_available", return_value=True)
    @patch("escrow.health._redis_is_available", return_value=True)
    def test_readiness_reports_each_healthy_dependency(
        self,
        redis_check: Mock,
        rabbitmq_check: Mock,
        database_check: Mock,
    ) -> None:
        response = self.client.get("/health/ready/")

        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "dependencies": {
                "database": "ok",
                "rabbitmq": "ok",
                "redis": "ok",
            },
        }
        database_check.assert_called_once_with()
        rabbitmq_check.assert_called_once_with()
        redis_check.assert_called_once_with()

    @patch("escrow.health._database_is_available", return_value=False)
    @patch("escrow.health._rabbitmq_is_available", return_value=True)
    @patch("escrow.health._redis_is_available", return_value=False)
    def test_readiness_reports_degraded_without_leaking_error_details(
        self,
        redis_check: Mock,
        rabbitmq_check: Mock,
        database_check: Mock,
    ) -> None:
        response = self.client.get("/health/ready/")

        assert response.status_code == 503
        assert response.json() == {
            "status": "degraded",
            "dependencies": {
                "database": "unavailable",
                "rabbitmq": "ok",
                "redis": "unavailable",
            },
        }
        database_check.assert_called_once_with()
        rabbitmq_check.assert_called_once_with()
        redis_check.assert_called_once_with()
