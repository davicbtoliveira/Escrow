# ADR 0009: Local infrastructure and MiniStack IaC

- Status: Accepted
- Date: 2026-07-18

## Context

The portfolio should run locally with Docker Compose while demonstrating a credible cloud mapping. Local infrastructure must preserve the chosen RabbitMQ/PostgreSQL/Redis architecture instead of replacing it with AWS equivalents.

## Decision

### Docker Compose

Run these local services:

- React frontend
- Django API
- Celery worker processes and Beat
- outbox publisher
- PostgreSQL
- RabbitMQ with management UI and explicit DLX/DLQs
- Redis
- Ceph RADOS Gateway in a development/single-node configuration
- MiniStack

Use an optional `observability` Compose profile for Prometheus, Grafana, OpenTelemetry Collector, Jaeger, and Flower.

### Data responsibilities

- PostgreSQL is source of truth for business state, ledger, idempotency, outbox/inbox, audit, and task-visible state.
- Redis provides balance/read-model caches, Django Channels fan-out, rate limits, and non-financial short locks. It never owns ledger correctness or message idempotency.
- Ceph RGW is the private S3-compatible evidence object store.
- PostgreSQL stores evidence metadata, object key, size, MIME, SHA-256, owner, scan/status fields, and audit references.
- Files remain private and are downloaded through authorized, time-limited access.

### MiniStack

Use MiniStack to emulate only AWS APIs used locally:

- SES for OTP email
- Secrets Manager for application/webhook secrets
- KMS for PII envelope encryption

Do not replace RabbitMQ, PostgreSQL, Redis, or Ceph at runtime with MiniStack SQS, RDS, ElastiCache, or S3.

MiniStack emulates APIs rather than real AWS security/infrastructure. Its lack of IAM enforcement means it cannot validate production authorization or network isolation.

### Terraform

- Terraform targets MiniStack for local cloud bootstrap.
- Docker Compose starts MiniStack; Terraform creates/configures SES, Secrets Manager, and KMS resources.
- Keep provider endpoint and credentials configurable so a later AWS migration has a clear starting point.
- Use local Terraform state, ignore `*.tfstate`, and document remote state with locking as required future work.
- CI creates an ephemeral MiniStack environment, applies Terraform, tests it, and destroys the environment.
- Do not claim that AWS migration is only an endpoint change. Real AWS also requires IAM, networking, credentials, remote state, managed-service configuration, and emulator-gap validation.

### Cloud mapping documentation

Document the intended mapping without deploying it:

| Local | Potential AWS target |
| --- | --- |
| Django API/workers | ECS |
| RabbitMQ | SQS/SNS or Amazon MQ after a dedicated redesign decision |
| PostgreSQL | RDS for PostgreSQL |
| Redis | ElastiCache |
| Ceph RGW | S3 |
| MiniStack SES | SES |
| MiniStack Secrets Manager | Secrets Manager |
| MiniStack KMS | KMS |

## Consequences

- The default local stack is resource-heavy, especially Ceph; documented minimum host requirements and health checks are necessary.
- Ceph owns object storage; MiniStack S3 is deliberately unused, so the two services do not duplicate responsibility.
- Terraform proves reproducible local AWS-API configuration, not production infrastructure readiness.
- RabbitMQ-to-SQS is not a transparent rename and needs a future ADR if implemented.
