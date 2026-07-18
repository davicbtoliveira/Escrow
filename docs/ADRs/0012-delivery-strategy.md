# ADR 0012: Delivery sequence and deferred scope

- Status: Accepted
- Date: 2026-07-18

## Context

The complete design contains many production-like concerns. Implementing horizontal layers first would delay visible end-to-end value and make integration risk accumulate.

## Decision

Deliver tracer-bullet vertical slices.

### Slice 1: complete happy path

Implement the smallest real path through every architectural boundary:

```text
organization/API key
  -> agreement
  -> hosted simulated PIX checkout
  -> approved funding risk
  -> double-entry custody posting
  -> delivery reported
  -> customer OTP acceptance
  -> release posting with platform fee
  -> outgoing webhook + WebSocket update
  -> minimal organization and operations dashboards
```

The slice includes foundational idempotency, outbox/inbox, authentication, tenant authorization, and automated tests; these are not cleanup tasks.

### Slice 2: exceptions and human operations

- funding `REVIEW_REQUIRED` path;
- dispute evidence and risk report;
- analyst validation/recommendation;
- admin release/refund decision;
- 72-hour SLA dashboards.

### Slice 3: resilience and operational depth

- deadlines and scheduled release/refund;
- retry/DLQ/replay exercises;
- observability dashboards/traces;
- expanded chaos/resilience tests;
- Terraform MiniStack validation;
- complete API/webhook operational screens.

### Deferred scope

- real funds or production support;
- credit/debit cards, installments, and interest;
- external organization payout;
- transactional FX and additional currencies;
- email verification at organization registration and 2FA;
- advanced organization review, trust score, complaint rankings, and analytics;
- visual risk-policy editor or machine learning;
- production antivirus scanning;
- remote Terraform state and real AWS deployment;
- validated legal/regulatory retention, KYC/AML, PCI, or compliance certification;
- Storybook and additional UI locales.

## Consequences

- The first milestone demonstrates the entire escrow value proposition instead of isolated infrastructure.
- Reliability primitives are introduced with the first financial action, preventing a later rewrite.
- Dashboard breadth follows executable workflows and read models.
- Deferred features remain explicit and cannot silently expand MVP scope.

