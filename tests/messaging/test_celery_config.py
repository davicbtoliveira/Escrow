from __future__ import annotations

from django.test import SimpleTestCase

from escrow.celery import app
from escrow.delivery.tasks import enqueue_expired_delivery_refunds_task
from escrow.messaging.tasks import publish_outbox_batch
from escrow.messaging.topology import OUTBOX_PUBLISHER_QUEUE


class CeleryConfigurationTests(SimpleTestCase):
    def test_workers_use_only_json_late_acks_and_no_result_backend(self) -> None:
        assert app.conf.task_serializer == "json"
        assert app.conf.accept_content == ("json",)
        assert app.conf.result_backend is None
        assert app.conf.task_ignore_result
        assert app.conf.task_acks_late
        assert app.conf.task_reject_on_worker_lost
        assert app.conf.worker_prefetch_multiplier == 1
        assert app.conf.task_default_queue == OUTBOX_PUBLISHER_QUEUE.name

    def test_unknown_tasks_cannot_fall_back_to_the_default_queue(self) -> None:
        router = app.conf.task_routes[0]

        with self.assertRaisesRegex(ValueError, "explicit route"):
            router("escrow.unknown", (), {}, {})

    def test_outbox_publisher_is_a_shared_task_with_an_explicit_route(self) -> None:
        task = app.tasks["escrow.messaging.publish_outbox_batch"]

        assert task.name == "escrow.messaging.publish_outbox_batch"
        assert task.queue == OUTBOX_PUBLISHER_QUEUE.name
        assert task.routing_key == OUTBOX_PUBLISHER_QUEUE.name
        assert publish_outbox_batch.name == task.name

    def test_expired_delivery_refund_scan_is_a_routed_beat_task(self) -> None:
        from django.conf import settings

        task_name = "escrow.delivery.enqueue_expired_delivery_refunds"
        task = app.tasks[task_name]

        assert task.name == task_name
        assert enqueue_expired_delivery_refunds_task.name == task_name
        schedule = settings.CELERY_BEAT_SCHEDULE["enqueue-expired-delivery-refunds"]
        assert schedule["task"] == task_name
        assert schedule["schedule"] > 0
        router = app.conf.task_routes[0]
        route = router(task_name, (), {}, {})
        assert route["queue"] == OUTBOX_PUBLISHER_QUEUE.name
        assert route["routing_key"] == OUTBOX_PUBLISHER_QUEUE.name
