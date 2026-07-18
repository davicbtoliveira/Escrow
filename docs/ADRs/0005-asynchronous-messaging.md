# ADR 0005: RabbitMQ, Celery, and delivery guarantees

- Status: Accepted
- Date: 2026-07-18

## Context

Saving PostgreSQL state and then publishing directly to RabbitMQ creates a dual-write gap. RabbitMQ also provides at-least-once delivery, so redelivery, worker death, and out-of-order messages must be normal operating conditions.

## Decision

- Use RabbitMQ as broker and Celery as worker framework.
- Treat delivery as **at least once**. Do not claim exactly-once processing.
- Use a transactional outbox: write business state and `outbox_events` in one PostgreSQL transaction.
- Run an outbox publisher that:
  - selects batches with `FOR UPDATE SKIP LOCKED`;
  - uses RabbitMQ publisher confirms;
  - sets `published_at` only after broker confirmation;
  - leaves failed publications pending;
  - exposes age/backlog metrics.
- Use a consumer inbox table (`processed_messages`) with a unique `message_id`. Record the inbox row and business/ledger effect in the same database transaction. A duplicate is acknowledged without repeating the effect.
- Use JSON serialization only; prohibit pickle.
- Use this message envelope:
  - `message_id`
  - `type`
  - `version`
  - `occurred_at`
  - `correlation_id`
  - `causation_id`
  - `tenant_id`
  - `payload`
- Name commands imperatively (`EvaluateFundingRisk.v1`, `PostFunding.v1`, `ReleaseFunds.v1`) and events in past tense (`FundingApproved.v1`, `FundsHeld.v1`, `DisputeOpened.v1`). Breaking schema changes create a new message version.

RabbitMQ topology:

```text
Exchanges
escrow.commands
escrow.events
escrow.dlx

Queues
risk.funding
risk.dispute
ledger.funding
ledger.release
ledger.refund
notifications.webhook
notifications.realtime
<critical-queue>.dlq
```

- Route every Celery task explicitly; do not use the default queue.
- Use late acknowledgements, worker-loss rejection, and graceful shutdown.
- Retry transient failures five times with exponential backoff and jitter.
- Send permanent payload/domain failures directly to the queue-specific DLQ.
- Send exhausted transient failures to DLQ with payload, headers, error, and attempt metadata.
- Replay through an audited Django management command, retaining the original `message_id`; never auto-loop DLQs.
- Do not use a Celery result backend. Persist meaningful task/business state in PostgreSQL.
- Celery Beat periodically scans PostgreSQL for expired delivery/inspection deadlines; PostgreSQL remains the scheduler source of truth.

## Consequences

- Duplicate delivery is expected and testable.
- Message publication is eventually consistent with the committed API transaction.
- Operations need metrics and alerts for old outbox rows, retries, and non-empty DLQs.
- The design accepts temporary duplicate publication because consumers are idempotent.

