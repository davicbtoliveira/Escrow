# ADR 0007: External API, webhooks, and realtime updates

- Status: Accepted
- Date: 2026-07-18

## Context

Organizations need programmatic integration while customers need a hosted, accountless payment and inspection flow. Asynchronous state changes must be observable without treating a WebSocket connection as durable state.

## Decision

### External API

- Expose REST JSON under `/api/v1` and publish generated OpenAPI documentation.
- Require organization API keys in headers, never query strings.
- Require `Idempotency-Key` for agreement/payment creation and other money-related mutations.
- Use cursor pagination, ISO-8601 UTC timestamps, and decimal-string amounts with currency.
- Return stable errors with `code`, `message`, `details`, and `correlation_id`.
- Generate the TypeScript API client/types from OpenAPI.

### API keys

- Show a key value once; store only its hash and display prefix.
- Support scopes: `payments:write`, `payments:read`, `agreements:read`, and `webhooks:manage`.
- Allow at most two active keys per organization.
- Support optional expiry, overlap-based rotation, and immediate revocation.
- Display last use and source IP. Never expose the original key again.

### Simulated PIX

- The hosted checkout creates a fake PIX charge.
- A sandbox control can approve, reject, delay, or duplicate the provider webhook to exercise reliability behavior.
- Verify a simulated provider signature before creating the `FUNDING` transfer and risk command.
- Keep the provider adapter independent from agreement and ledger rules.

### Customer authorization

- Checkout/status uses an opaque bearer token.
- Customer acceptance and dispute creation require an email OTP because either action can move or freeze funds.
- MiniStack SES delivers/stores OTP emails locally.

### Outgoing webhooks

- Deliver at least once with `event_id`, `agreement_id`, and per-agreement `sequence`.
- Sign `timestamp + body` using HMAC SHA-256.
- Use a five-second timeout and exponential retries for up to 24 hours.
- Treat any `2xx` as acknowledgement.
- Mark an exhausted delivery `FAILED` and allow audited replay through dashboard/API.
- Rotate webhook secrets with temporary overlap.
- Limit delivery to 10 requests/second per organization and destination host; excess remains queued.

### API limits

- B2B API: 100 requests/minute per key, burst 20.
- Login: five attempts per 15 minutes by IP and email.
- OTP: five sends/hour and five verification attempts per code.
- Public checkout: 60 requests/minute per IP.
- Return `429` and `Retry-After` for inbound limits.
- Store limiter state in Redis; keep financial correctness independent from limiter state.

### Realtime status

- Use Django Channels and Redis channel layer for WebSockets.
- PostgreSQL remains authoritative.
- Send small status events with a sequence, never PII, evidence, or risk-report contents.
- On connect/reconnect, the client fetches a fresh HTTP snapshot; sequence gaps also trigger refetch.
- Provide polling fallback.

## Consequences

- API consumers must handle `202 Processing`, webhooks, duplicate events, and retries.
- A leaked checkout link permits viewing limited order status but cannot authorize acceptance/dispute without OTP.
- Redis or WebSocket failure degrades freshness, not financial correctness.

