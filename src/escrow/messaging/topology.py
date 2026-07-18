"""Explicit durable RabbitMQ topology from ADR 0005."""

from __future__ import annotations

from kombu import Exchange, Queue  # type: ignore[import-untyped]

COMMANDS_EXCHANGE = Exchange("escrow.commands", type="direct", durable=True)
EVENTS_EXCHANGE = Exchange("escrow.events", type="topic", durable=True)
DEAD_LETTER_EXCHANGE = Exchange("escrow.dlx", type="direct", durable=True)


def _critical_queue(name: str, exchange: Exchange) -> Queue:
    return Queue(
        name,
        exchange=exchange,
        routing_key=name,
        durable=True,
        queue_arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE.name,
            "x-dead-letter-routing-key": f"{name}.dlq",
        },
    )


def _dead_letter_queue(name: str) -> Queue:
    return Queue(
        f"{name}.dlq",
        exchange=DEAD_LETTER_EXCHANGE,
        routing_key=f"{name}.dlq",
        durable=True,
    )


OUTBOX_PUBLISHER_QUEUE = _critical_queue("messaging.outbox", COMMANDS_EXCHANGE)
RISK_FUNDING_QUEUE = _critical_queue("risk.funding", COMMANDS_EXCHANGE)
RISK_DISPUTE_QUEUE = _critical_queue("risk.dispute", COMMANDS_EXCHANGE)
LEDGER_FUNDING_QUEUE = _critical_queue("ledger.funding", COMMANDS_EXCHANGE)
LEDGER_RELEASE_QUEUE = _critical_queue("ledger.release", COMMANDS_EXCHANGE)
LEDGER_REFUND_QUEUE = _critical_queue("ledger.refund", COMMANDS_EXCHANGE)
NOTIFICATIONS_WEBHOOK_QUEUE = _critical_queue("notifications.webhook", EVENTS_EXCHANGE)
NOTIFICATIONS_REALTIME_QUEUE = _critical_queue("notifications.realtime", EVENTS_EXCHANGE)

PRIMARY_QUEUES = (
    OUTBOX_PUBLISHER_QUEUE,
    RISK_FUNDING_QUEUE,
    RISK_DISPUTE_QUEUE,
    LEDGER_FUNDING_QUEUE,
    LEDGER_RELEASE_QUEUE,
    LEDGER_REFUND_QUEUE,
    NOTIFICATIONS_WEBHOOK_QUEUE,
    NOTIFICATIONS_REALTIME_QUEUE,
)
DEAD_LETTER_QUEUES = tuple(_dead_letter_queue(queue.name) for queue in PRIMARY_QUEUES)
ALL_QUEUES = PRIMARY_QUEUES + DEAD_LETTER_QUEUES

_EXCHANGE_BY_ROUTING_KEY = {queue.name: queue.exchange for queue in PRIMARY_QUEUES}

_QUEUE_BY_ROUTING_KEY = {queue.name: queue for queue in PRIMARY_QUEUES}
_DEAD_LETTER_QUEUE_BY_ROUTING_KEY = {
    primary_queue.name: dead_letter_queue
    for primary_queue, dead_letter_queue in zip(PRIMARY_QUEUES, DEAD_LETTER_QUEUES, strict=True)
}
_TASK_NAME_BY_ROUTING_KEY = {
    OUTBOX_PUBLISHER_QUEUE.name: "escrow.messaging.publish_outbox_batch",
    RISK_FUNDING_QUEUE.name: "escrow.risk.evaluate_funding_risk",
    RISK_DISPUTE_QUEUE.name: "escrow.risk.evaluate_dispute_risk",
    LEDGER_FUNDING_QUEUE.name: "escrow.ledger.post_funding",
    LEDGER_RELEASE_QUEUE.name: "escrow.ledger.release_funds",
    LEDGER_REFUND_QUEUE.name: "escrow.ledger.refund_funds",
    NOTIFICATIONS_WEBHOOK_QUEUE.name: "escrow.notifications.deliver_webhook",
    NOTIFICATIONS_REALTIME_QUEUE.name: "escrow.notifications.publish_realtime",
}

CELERY_TASK_ROUTES = {
    task_name: {"queue": routing_key, "routing_key": routing_key}
    for routing_key, task_name in _TASK_NAME_BY_ROUTING_KEY.items()
}
CELERY_TASK_ROUTES["escrow.integrations.enqueue_due_webhook_deliveries"] = {
    "queue": OUTBOX_PUBLISHER_QUEUE.name,
    "routing_key": OUTBOX_PUBLISHER_QUEUE.name,
}


def exchange_for_routing_key(routing_key: str) -> Exchange:
    """Resolve only an ADR-declared route; arbitrary broker routes are rejected."""
    try:
        return _EXCHANGE_BY_ROUTING_KEY[routing_key]
    except KeyError as error:
        raise ValueError(f"unsupported messaging route: {routing_key}") from error


def queue_for_routing_key(routing_key: str) -> Queue:
    """Return the declared queue used to publish one explicitly routed task."""
    try:
        return _QUEUE_BY_ROUTING_KEY[routing_key]
    except KeyError as error:
        raise ValueError(f"unsupported messaging route: {routing_key}") from error


def dead_letter_queue_for_routing_key(routing_key: str) -> Queue:
    """Return the declared queue that receives rejected deliveries for one route."""
    try:
        return _DEAD_LETTER_QUEUE_BY_ROUTING_KEY[routing_key]
    except KeyError as error:
        raise ValueError(f"unsupported messaging route: {routing_key}") from error


def task_name_for_routing_key(routing_key: str) -> str:
    """Map a durable route to its Celery task protocol name."""
    try:
        return _TASK_NAME_BY_ROUTING_KEY[routing_key]
    except KeyError as error:
        raise ValueError(f"unsupported messaging route: {routing_key}") from error


def declare_topology(channel: object) -> None:
    """Declare exchanges and durable queues before a confirmed publication."""
    for exchange in (COMMANDS_EXCHANGE, EVENTS_EXCHANGE, DEAD_LETTER_EXCHANGE):
        exchange(channel).declare()
    for queue in ALL_QUEUES:
        queue(channel).declare()
