# ADR 0011: Testing, CI, and observability

- Status: Accepted
- Date: 2026-07-18

## Context

Financial correctness and asynchronous reliability cannot be demonstrated by a happy-path UI alone. The portfolio needs executable evidence for invariants, duplicate delivery, failure recovery, tenant authorization, and operational visibility.

## Decision

### Tests

- Unit-test domain state machines, accounting rules, risk policies, authorization, and serializers.
- Use Hypothesis property/stateful tests for at least:
  - every ledger transaction balances by currency;
  - reversals preserve history and restore expected balances;
  - duplicate/out-of-order messages cannot duplicate a financial effect;
  - invalid state transitions never post ledger entries.
- Run integration tests against containerized PostgreSQL, RabbitMQ, Redis, Ceph, and MiniStack.
- Test OpenAPI contracts, message schema versions, webhook payloads, and HMAC signatures.
- Use Playwright for end-to-end flows:
  - happy path from organization API to release;
  - funding manual review;
  - dispute through analyst and admin;
  - automatic release and automatic refund deadlines.
- Add resilience tests for duplicate messages, out-of-order messages, worker death, unavailable database, retry exhaustion, DLQ replay, and reconnecting WebSockets.
- Target at least 90% coverage in the financial domain packages. Do not impose a misleading global percentage target.

### CI

Use GitHub Actions to run:

- backend and frontend format/lint/type checks;
- unit and property tests on every pull request;
- integration and Playwright tests;
- migration consistency checks;
- Docker image builds;
- Terraform format/validate and ephemeral MiniStack apply/test;
- dependency, secret, source, and image scans.

Protect the main branch and require green checks. Do not deploy a real environment in the MVP.

### Observability

- Emit structured JSON logs to stdout with `correlation_id`, `causation_id`, `event_id`, and safe domain IDs. Never log passwords, OTPs, API keys, decrypted CPF/CNPJ, or evidence contents.
- Expose health and readiness checks for every process and dependency.
- Use Prometheus for HTTP latency/error rates, worker throughput/failures, queue age/depth, retries, DLQ count, outbox age, risk outcomes, ledger postings, WebSocket connections, and dispute SLA.
- Provide Grafana dashboards as code.
- Instrument API, Celery, broker, and database spans with OpenTelemetry and view traces in Jaeger.
- Use Flower for local Celery worker/task inspection.
- Keep observability services in an optional Compose profile.

## Consequences

- CI is slower and requires service containers, but provides evidence for portfolio claims.
- Correlation and causation IDs must cross HTTP, outbox, RabbitMQ, Celery, and webhook boundaries.
- Metrics must avoid high-cardinality labels such as customer, agreement, or transaction IDs.
- Optional observability keeps the default development path lighter.

