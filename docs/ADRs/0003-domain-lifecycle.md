# ADR 0003: Escrow domain and lifecycle

- Status: Accepted
- Date: 2026-07-18

## Context

A single generic `Transaction` cannot accurately represent an agreement, its funding, a later release or refund, accounting entries, and a dispute. It also makes a completed funding transfer look like a completed escrow agreement.

## Decision

Use separate domain concepts:

- `EscrowAgreement`: parties, amount, currency, delivery deadline, inspection deadline, fees, and agreement state.
- `Transfer`: one asynchronous financial intent of type `FUNDING`, `RELEASE`, or `REFUND`.
- `LedgerTransaction` and `LedgerEntry`: immutable accounting records.
- `Dispute`: evidence, risk report, analyst recommendation, admin decision, and SLA.

Use these main state machines:

```text
EscrowAgreement
AWAITING_PAYMENT
  -> FUNDING_PROCESSING
  -> HELD
  -> INSPECTION
  -> RELEASE_PENDING
  -> RELEASED
```

Alternative agreement states are `REVIEW_REQUIRED`, `FUNDING_REJECTED`, `CANCELLED`, `DISPUTED`, `REFUND_PENDING`, and `REFUNDED`.

```text
Dispute
OPEN
  -> REPORT_GENERATING
  -> ANALYST_REVIEW
  -> ADMIN_REVIEW
  -> RESOLVED
```

```text
Transfer
PENDING -> PROCESSING -> COMPLETED
                         or FAILED
```

Lifecycle rules:

- An agreement requires `delivery_due_at`, between 1 and 90 days after payment.
- A confirmed PIX remains in a pending-risk liability until the funding decision. Risk rejection enqueues an automatic return to the customer; it cannot leave received funds stranded.
- If the organization misses the delivery deadline, enqueue an automatic refund.
- When the organization reports delivery, start a seven-day inspection window.
- Customer acceptance ends inspection and enqueues release immediately.
- No customer action by the deadline enqueues automatic release.
- A dispute opened during inspection keeps funds held.
- A post-release guaranteed refund does not exist in the MVP; that would require reserve, clawback, or negative-balance behavior closer to a marketplace payments product.
- Invalid state transitions return HTTP `409`; code cannot set states arbitrarily.
- Race conditions between acceptance, dispute, and scheduled release use a PostgreSQL row lock plus an optimistic `version` field. The first valid transition wins.
- Release and refund transfers have uniqueness constraints per agreement, and ledger posting rechecks current state.

## Consequences

- Funding may be complete while the agreement remains held.
- Scheduled work must query PostgreSQL as the source of truth instead of relying on a seven-day in-memory task timer.
- UI labels must distinguish “payment processed,” “held in escrow,” and “released.”
- Reversals create new transitions and ledger entries; they do not rewrite history.
