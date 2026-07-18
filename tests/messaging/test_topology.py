from __future__ import annotations

from django.test import SimpleTestCase

from escrow.messaging.topology import (
    ALL_QUEUES,
    CELERY_TASK_ROUTES,
    COMMANDS_EXCHANGE,
    DEAD_LETTER_EXCHANGE,
    EVENTS_EXCHANGE,
    LEDGER_FUNDING_QUEUE,
    NOTIFICATIONS_REALTIME_QUEUE,
    RISK_FUNDING_QUEUE,
    exchange_for_routing_key,
)


class RabbitTopologyTests(SimpleTestCase):
    def test_critical_queues_are_durable_and_have_queue_specific_dlqs(self) -> None:
        for queue in (RISK_FUNDING_QUEUE, LEDGER_FUNDING_QUEUE, NOTIFICATIONS_REALTIME_QUEUE):
            assert queue.durable
            assert queue.queue_arguments == {
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE.name,
                "x-dead-letter-routing-key": f"{queue.name}.dlq",
            }
            assert any(candidate.name == f"{queue.name}.dlq" for candidate in ALL_QUEUES)

    def test_commands_and_events_are_bound_to_their_explicit_exchanges(self) -> None:
        assert COMMANDS_EXCHANGE.type == "direct"
        assert EVENTS_EXCHANGE.type == "topic"
        assert exchange_for_routing_key(RISK_FUNDING_QUEUE.name).name == COMMANDS_EXCHANGE.name
        assert exchange_for_routing_key(LEDGER_FUNDING_QUEUE.name).name == COMMANDS_EXCHANGE.name
        realtime_exchange = exchange_for_routing_key(NOTIFICATIONS_REALTIME_QUEUE.name)
        assert realtime_exchange.name == EVENTS_EXCHANGE.name
        assert CELERY_TASK_ROUTES["escrow.messaging.publish_outbox_batch"] == {
            "queue": "messaging.outbox",
            "routing_key": "messaging.outbox",
        }
