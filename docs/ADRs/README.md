# Architecture Decision Records

Project: **Escrow** (working name)

These ADRs capture the initial product and architecture decisions for a realistic, portfolio-only escrow simulation. The system must never process real funds or real customer data.

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-product-scope-and-actors.md) | Product scope and actors | Accepted |
| [0002](0002-modular-monolith.md) | Django modular monolith | Accepted |
| [0003](0003-domain-lifecycle.md) | Escrow domain and lifecycle | Accepted |
| [0004](0004-double-entry-ledger.md) | Double-entry ledger and money model | Accepted |
| [0005](0005-asynchronous-messaging.md) | RabbitMQ, Celery, and delivery guarantees | Accepted |
| [0006](0006-risk-and-dispute-workflow.md) | Risk and dispute workflow | Accepted |
| [0007](0007-api-webhooks-and-realtime.md) | External API, webhooks, and realtime updates | Accepted |
| [0008](0008-identity-and-security.md) | Identity, PII, and security baseline | Accepted |
| [0009](0009-local-infrastructure-and-iac.md) | Local infrastructure and MiniStack IaC | Accepted |
| [0010](0010-frontend-and-design-system.md) | React frontend and visual direction | Accepted |
| [0011](0011-quality-and-observability.md) | Testing, CI, and observability | Accepted |
| [0012](0012-delivery-strategy.md) | Delivery sequence and deferred scope | Accepted |
| [0013](0013-mvp-operational-contract-clarifications.md) | MVP operational contract clarifications | Accepted |

## Conventions

- ADRs, source code, API fields, events, and queue names are written in English.
- The initial UI locale is `pt-BR`; the frontend must remain ready for later `en-US` localization.
- An accepted ADR is changed by a new ADR that supersedes it, not by silently rewriting the original decision.
- Technical versions are baselines, not permission to ignore security patch updates.
