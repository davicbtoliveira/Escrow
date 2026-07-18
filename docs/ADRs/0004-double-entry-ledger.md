# ADR 0004: Double-entry ledger and money model

- Status: Accepted
- Date: 2026-07-18

## Context

Mutable wallet balances and a check of `transaction_id` alone do not provide credible financial correctness. The project should demonstrate balanced, immutable accounting and database-enforced invariants.

## Decision

- Implement an append-only double-entry ledger with a real chart of accounts.
- Every ledger transaction must balance debits and credits independently per currency.
- Never edit or delete posted entries. Corrections use explicit reversing transactions.
- Store amounts as signed/unsigned integer minor units (`BIGINT`) as appropriate; never use floating point.
- Accept external API amounts as decimal strings plus an explicit ISO currency.
- Support `BRL` and `USD` in the MVP, with `BRL` as default.
- An agreement's accounting currency is immutable. Do not exchange currencies or combine balances across currencies.
- The BRL/USD toggle is presentation only. It uses a timestamped simulated `ExchangeRate`, displays an approximation marker, and never changes the ledger.
- Expose organization balances separately by currency:
  - held in escrow
  - scheduled releases with `release_at`
  - available balance
- Charge a configurable organization fee, default 2%, at release. Snapshot the fee terms on agreement creation.

Illustrative postings:

```text
PIX received, before risk decision
Dr PIX_CLEARING
Cr FUNDS_PENDING_RISK

Funding approved and taken into custody
Dr FUNDS_PENDING_RISK
Cr ESCROW_LIABILITY

Funding rejected
Dr FUNDS_PENDING_RISK
Cr PIX_CLEARING

Release
Dr ESCROW_LIABILITY
Cr ORGANIZATION_PAYABLE (net amount)
Cr PLATFORM_FEE_REVENUE

Refund
Dr ESCROW_LIABILITY
Cr PIX_CLEARING
```

Database protections:

- Unique constraints for idempotency and one-off financial intents.
- Foreign keys without cascading deletion for ledger and audit records.
- A deferred PostgreSQL constraint trigger rejects an unbalanced ledger transaction at commit.
- Application validation remains in place as the first defense.
- Balance read models may be cached in Redis, but PostgreSQL ledger entries remain authoritative.

## Consequences

- Queries and tests are more involved than updating a balance column.
- Posting logic becomes a small, protected subsystem with strong property-based tests.
- UI balances can be rebuilt from the ledger if a cache/read model is lost.
- Supporting currencies with different minor-unit scales later requires currency metadata; the initial two currencies both use two decimal places.
