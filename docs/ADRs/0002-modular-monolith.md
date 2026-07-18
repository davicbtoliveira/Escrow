# ADR 0002: Django modular monolith

- Status: Accepted
- Date: 2026-07-18

## Context

The project needs asynchronous processing and independently scalable workers, but separate microservice repositories would add deployment and consistency complexity without improving the portfolio use case.

## Decision

- Build a modular monolith using Python 3.13, Django 5.2 LTS, Django REST Framework, and psycopg 3.
- Use Celery 5.6 for asynchronous work.
- Run the same codebase as separate processes/containers:
  - HTTP/ASGI API
  - risk worker
  - ledger worker
  - integration/notification worker
  - Celery Beat scheduler
  - transactional outbox publisher
- Organize Django apps by bounded context:
  - `identity`
  - `organizations`
  - `agreements`
  - `payments`
  - `ledger`
  - `risk`
  - `disputes`
  - `integrations`
  - `notifications`
  - `audit`
- Within each context, use explicit models, services, selectors, tasks, API endpoints, events, and tests.
- Use the flow `DRF view -> serializer -> application service -> Django ORM`.
- Application services own transactions and domain transitions. Selectors own non-trivial read queries.
- Keep critical financial rules in pure, testable domain functions or objects.
- Do not add generic repository/unit-of-work abstractions over Django ORM.
- Do not implement full CQRS or event sourcing. The append-only ledger is not an event store.
- Manage Python dependencies with `uv` and commit `uv.lock`.
- Use Ruff for lint/format/imports and mypy with Django stubs. Apply strict typing at least to domain, ledger, risk, and service code.

## Consequences

- Workers can scale independently while sharing domain behavior and migrations.
- Module dependency direction must be reviewed; convenience imports must not collapse contexts into a ball of mud.
- Cross-module side effects use application services or events, not model signals hidden across the project.
- A later service extraction remains possible, but is not a current goal.

