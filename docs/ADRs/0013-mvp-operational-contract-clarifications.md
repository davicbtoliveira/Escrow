# ADR 0013: MVP operational contract clarifications

- Status: Accepted
- Date: 2026-07-18

## Context

The accepted ADRs define the escrow lifecycle, money model, risk workflow, and external contracts. Before implementing the first financial slices, a few operational details need one unambiguous MVP rule. Leaving these details implicit would make retries, deadlines, and authorization behave differently across modules.

## Decision

This ADR supplements ADRs 0003, 0004, 0006, 0007, and 0008. Where wording conflicts, this ADR controls.

### API keys and idempotency

- Add `agreements:write` to the organization API-key scopes. Creating an agreement requires it; `agreements:read` remains sufficient for reads, and `payments:write` is reserved for payment-related mutation endpoints.
- Scope an idempotency record to organization, HTTP method, route, and `Idempotency-Key`.
- On the first idempotent mutation, including agreement creation and every money-affecting mutation, persist the canonical request-payload SHA-256, original response status/body, and resulting resource identity atomically with the business intent.
- A retry with the same scope, key, and payload hash returns the stored response and creates no new resource, command, or ledger effect.
- Reusing the same scoped key with a different payload hash returns `409` with code `idempotency_key_reused`; it creates no side effect.
- Database uniqueness remains the final defense for financial effects even when an idempotency record or message is replayed.

### Delivery deadline and fee calculation

- Agreement creation accepts and snapshots `delivery_window_days`, an integer from 1 through 90. It does not assign a delivery deadline before payment.
- On a valid confirmed PIX callback, set `delivery_due_at` atomically to `confirmed_at + delivery_window_days`. Rejected or refunded funding never creates a usable delivery deadline.
- Store the organization fee as an integer `fee_bps`, default `200` (2%), and snapshot it with the agreement.
- At release, calculate `fee_minor = ROUND_HALF_UP(gross_minor * fee_bps / 10_000)` in integer minor units. `net_minor` is `gross_minor - fee_minor`; no floating-point arithmetic is allowed.

### Platform staff provisioning

- Platform staff are provisioned only through an explicit, idempotent Django management command for local/demo/test use.
- The command provisions distinct `PLATFORM_ADMIN` and `RISK_DISPUTE_ANALYST` users from supplied email and secret inputs. It must not run at application startup, create default credentials, or commit credentials to the repository.
- The command is disabled in production configuration unless an operator explicitly enables a future production provisioning path.

### Private evidence access

- A customer may download only evidence attached to that customer's own dispute.
- This requires both the opaque checkout token and a fresh successful email-OTP verification. The checkout token alone permits limited status viewing but no evidence download.
- Authorize the request server-side, emit an immutable evidence-access audit event, then issue a short-lived Ceph RGW pre-signed URL. Organization users cannot download customer evidence. Assigned analysts and platform admins use their authenticated staff authorization instead.

### Informative dispute policy

- Dispute-report generation uses a deterministic, versioned, informative policy. Its flags include duplicate evidence hashes, customer dispute count and timing, organization dispute history/rate, and the agreement/payment/delivery timeline.
- The report persists the policy version, input snapshot, flags, and score. With no flags it explicitly reports `NO_SUSPICION`.
- Dispute-policy output can never release, refund, or otherwise move funds. It only informs the analyst recommendation; `PLATFORM_ADMIN` remains the sole final decision maker.

### Pending funding-risk deadline

- A funding decision of `REVIEW_REQUIRED` receives `risk_review_due_at = confirmed_at + 24 hours`.
- Until the decision, value remains in `FUNDS_PENDING_RISK`; it is not escrow custody.
- An analyst may approve or reject before that deadline. If no decision exists when it expires, a PostgreSQL-backed deadline scan applies the conservative terminal outcome: reject funding and create exactly one automatic refund intent.
- This scan is independent of a particular Celery worker's availability and uses the same locking, uniqueness, outbox, and ledger guards as ordinary rejection.

### Webhook sequence semantics

- Per-agreement webhook `sequence` is monotonically assigned when the event is recorded, but outbound delivery is at-least-once and does not guarantee arrival order.
- Consumers must tolerate duplicate and out-of-order events. A missing or unexpected sequence is a signal to fetch the authoritative agreement snapshot, not a reason to reject later valid events.
- Replay preserves the original `event_id` and sequence. Acknowledging any `2xx` remains the delivery contract.

## Consequences

- API integrations can distinguish a harmless retry from a conflicting idempotency-key reuse.
- Delivery and funding-review timers have a single database-derived origin and cannot silently hold customer value forever.
- Financial fee rounding is deterministic and property-testable.
- The customer evidence journey remains private without turning the hosted checkout token into a sufficient authorization credential.
- Webhook consumers are explicitly responsible for reconciliation rather than relying on a transport ordering guarantee.
