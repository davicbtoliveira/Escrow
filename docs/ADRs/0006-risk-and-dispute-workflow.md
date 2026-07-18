# ADR 0006: Risk and dispute workflow

- Status: Accepted
- Date: 2026-07-18

## Context

The business case requires fraud analysis before custody. Disputes also need explainable evidence gathering before a human decision. A fake ML model would be difficult to justify, reproduce, and test.

## Decision

### Funding risk

- Run funding risk before ledger custody.
- After PIX confirmation and before the decision, account for received value as `FUNDS_PENDING_RISK`, not escrow custody.
- Produce one of `APPROVED`, `REVIEW_REQUIRED`, or `REJECTED`.
- Approval moves the pending liability into escrow; rejection enqueues an automatic customer return; manual review keeps it pending.
- Use deterministic, explainable, versioned policies stored in PostgreSQL.
- The MVP has no visual policy editor; policies are created by seeds/migrations.
- Persist policy version, input snapshot, score, triggered rules, and decision.

Initial configurable policy:

| Rule | Effect |
| --- | --- |
| Amount at least BRL 50,000 or USD 10,000 | +25 |
| Three or more customer payments in 60 seconds | +40 |
| Organization younger than seven days | +15 |
| Organization dispute rate above 10% over 30 days | +30 |
| Blocked organization | Immediate rejection |

Score bands: below 40 approves, 40–69 requires review, and 70 or more rejects.

### Dispute risk and human workflow

- Opening a dispute enqueues a report-generation task.
- The report supports the human decision; it never moves funds automatically.
- Always generate a report. If no indicators exist, return an explicit `NO_SUSPICION` result.
- Report at least:
  - executive summary;
  - suspicion result and flags;
  - agreement/payment/delivery timeline;
  - customer history;
  - organization history and dispute rate;
  - evidence hashes, duplicate detection, and metadata;
  - policy version, score, inputs, and generation timestamp.
- `RISK_DISPUTE_ANALYST` validates the report and submits a recommendation.
- `PLATFORM_ADMIN` alone makes the final `RELEASE_TO_ORGANIZATION` or `REFUND_TO_CUSTOMER` decision.
- Preserve an immutable audit trail. The analyst and admin separation implements a four-eyes workflow.

### SLA

- Start a 72-hour calendar SLA when the dispute opens; do not pause it.
- `ON_TRACK`: under 48 hours.
- `AT_RISK`: from 48 through 72 hours.
- `OVERDUE`: beyond 72 hours.
- Stop the SLA on the admin's final decision.
- Persist timestamps in UTC and convert only for display.

## Consequences

- Risk decisions are reproducible and explainable.
- Changing policy creates a new version rather than changing history.
- Dashboards need separate analyst and admin queues, with shared SLA/read-model components.
- The initial thresholds are simulation defaults, not claims about a real fraud program.
