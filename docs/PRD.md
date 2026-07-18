# PRD: Escrow B2B2C Custody Simulation

## Problem Statement

Marketplace and e-commerce organizations need a clear way to demonstrate conditional payment custody: a customer pays, funds remain protected while delivery is pending, and the organization receives the funds only after customer acceptance or expiration of an inspection window. When a dispute occurs, both parties need a traceable, explainable, human-reviewed process instead of an opaque automatic decision.

For this portfolio project, the problem must be solved without processing real funds or requiring production financial licenses, support, or compliance claims. The implementation still needs to prove mature engineering behavior: immutable accounting, asynchronous workflows, duplicate-message safety, fraud analysis, dispute SLAs, tenant isolation, auditability, realtime status, reproducible local infrastructure, and a credible cloud migration path.

## Solution

Build **Escrow**, a realistic B2B2C custody simulation for marketplaces and online stores. Organizations integrate through a versioned REST API and signed webhooks. Their customers use a hosted, accountless PIX checkout. Confirmed funds pass through an explainable risk pipeline, enter an append-only double-entry ledger, remain held during delivery and inspection, then become available to the organization after release.

Customers receive seven calendar days to inspect a reported delivery. They can accept immediately, allow automatic release at the deadline, or open a dispute that freezes the funds. A deterministic worker generates a fraud/risk report. A `RISK_DISPUTE_ANALYST` validates the report and recommends an outcome; a separate `PLATFORM_ADMIN` makes the final release or refund decision within a 72-hour calendar SLA.

The full system runs locally through Docker Compose using Django, React, Bun, PostgreSQL, RabbitMQ, Celery, Redis, Ceph, MiniStack, and Terraform. The interface uses a Catppuccin Mocha “custody control room” design and exposes organization, platform-operations, and customer surfaces.

## User Stories

1. As an organization owner, I want to register with email and a strong confirmed password, so that I can securely access the platform.
2. As an organization owner, I want breached passwords rejected through a privacy-preserving check, so that known compromised credentials cannot protect financial operations.
3. As an organization member, I want to log in with email and password, so that I can access my authorized organization workspace.
4. As an organization member, I want to recover a forgotten password, so that loss of a password does not permanently block access.
5. As an organization owner, I want to invite and manage members with `OWNER`, `FINANCE`, `SUPPORT`, and `VIEWER` roles, so that duties follow least privilege.
6. As an organization member, I want to see only my organization's data, so that another tenant's customers, balances, and integrations remain private.
7. As an organization owner, I want to create a scoped API key, so that my marketplace can integrate without using a human session.
8. As an organization owner, I want an API key displayed only once, so that the original secret is not recoverable later.
9. As an organization owner, I want to rotate an API key with temporary overlap, so that I can deploy a replacement without downtime.
10. As an organization owner, I want to revoke an API key immediately, so that a suspected credential can no longer access the API.
11. As an organization owner, I want to see API-key prefix, scopes, last use, and source IP, so that I can audit integration activity.
12. As an organization developer, I want generated OpenAPI documentation, so that I can integrate against an explicit contract.
13. As an organization developer, I want stable error codes and correlation IDs, so that I can diagnose failed requests without parsing prose.
14. As an organization developer, I want idempotent agreement and payment creation, so that retries never create duplicate financial intents.
15. As an organization developer, I want to create an escrow agreement with customer, amount, currency, fee, and delivery deadline, so that custody terms are explicit.
16. As an organization developer, I want an opaque hosted-checkout token returned for an agreement, so that I can send the customer to Escrow without creating an Escrow account.
17. As an organization developer, I want versioned webhook events, so that my integration can evolve safely.
18. As an organization owner, I want to configure a webhook destination and signing secret, so that my system receives authenticated state changes.
19. As an organization developer, I want webhook signatures over timestamp and body, so that I can reject forged or replayed deliveries.
20. As an organization developer, I want each webhook to contain event, agreement, and sequence identifiers, so that I can deduplicate and detect ordering gaps.
21. As an organization owner, I want webhook delivery logs, so that I can see pending, successful, retried, and failed notifications.
22. As an organization owner, I want to replay an exhausted webhook delivery, so that a recovered endpoint can receive the missed event.
23. As an organization developer, I want outbound webhook rate limiting to queue rather than discard events, so that throttling never loses state changes.
24. As an external customer, I want to open a hosted checkout without registering an Escrow account, so that payment remains simple.
25. As an external customer, I want my CPF/CNPJ validated, encrypted, and masked, so that identity is accurate without being casually exposed.
26. As an external customer, I want to pay through a simulated PIX charge, so that the checkout resembles a realistic asynchronous payment.
27. As an external customer, I want immediate acknowledgement that processing started, so that I am not blocked while fraud and ledger workers run.
28. As an external customer, I want status to update without refreshing the page, so that I can follow payment and custody progress.
29. As an external customer, I want status to recover after a disconnected WebSocket, so that temporary realtime failure does not hide the authoritative state.
30. As an organization developer, I want duplicate provider webhooks handled safely, so that network retries cannot duplicate funding.
31. As a platform risk operator, I want every confirmed PIX payment evaluated by versioned deterministic rules, so that funding decisions are explainable and reproducible.
32. As a platform risk operator, I want funding risk to return approved, manual-review, or rejected, so that ambiguous cases are not automatically forced into a binary result.
33. As a platform risk operator, I want high amount, transfer velocity, organization age, dispute rate, and block status represented in the score, so that the MVP demonstrates meaningful risk signals.
34. As a `RISK_DISPUTE_ANALYST`, I want a manual funding-review queue, so that borderline payments can be resolved before custody.
35. As a `RISK_DISPUTE_ANALYST`, I want to see the policy version, input snapshot, score, and triggered rules, so that I can explain a review decision.
36. As an external customer, I want rejected funding returned automatically, so that confirmed PIX value never remains stranded in pending risk.
37. As an organization member, I want approved funding posted into custody exactly once, so that duplicate messages cannot inflate or debit balances.
38. As an organization finance member, I want to see held balances separately in BRL and USD, so that I know how much is still under custody.
39. As an organization finance member, I want to see each scheduled release and its date, so that I can forecast availability.
40. As an organization finance member, I want to see available balances separately in BRL and USD, so that released funds are distinct from held funds.
41. As an organization finance member, I want an approximate BRL/USD display toggle, so that I can compare values without changing agreement or ledger currency.
42. As an organization finance member, I want fees disclosed and snapshotted when an agreement is created, so that later pricing changes do not rewrite existing terms.
43. As an organization finance member, I want the release to show gross amount, platform fee, and net available amount, so that the ledger result is understandable.
44. As an organization developer, I want to report delivery through the API, so that the seven-day inspection window starts from an explicit event.
45. As an external customer, I want to see the delivery and inspection deadline, so that I know when funds will be released.
46. As an external customer, I want an OTP before accepting delivery, so that a leaked status link cannot release funds.
47. As an external customer, I want acceptance to release funds immediately, so that a satisfactorily completed order does not wait unnecessarily.
48. As an organization finance member, I want funds automatically released when the seven-day inspection window expires without a dispute, so that inactivity does not hold money forever.
49. As an external customer, I want an automatic refund when the organization misses the delivery deadline, so that funds cannot remain held indefinitely.
50. As an external customer, I want to open a dispute during inspection after OTP verification, so that I can stop release when delivery is unacceptable.
51. As an external customer, I want to submit private evidence with a dispute, so that the platform can review supporting material.
52. As an external customer, I want evidence downloads authorized and time-limited, so that uploaded material is not public.
53. As an external customer, I want disputed funds to remain held until a final decision, so that the organization cannot receive contested value.
54. As a `RISK_DISPUTE_ANALYST`, I want a dispute report generated for every case, so that manual review always starts with a consistent evidence summary.
55. As a `RISK_DISPUTE_ANALYST`, I want an explicit `NO_SUSPICION` report when no fraud indicator exists, so that absence of evidence is recorded rather than implied.
56. As a `RISK_DISPUTE_ANALYST`, I want the report to include timeline, customer history, organization dispute history, evidence integrity, score, and policy version, so that my recommendation is grounded.
57. As a `RISK_DISPUTE_ANALYST`, I want an SLA dashboard showing on-track, at-risk, and overdue cases, so that I can prioritize work before 72 hours expires.
58. As a `RISK_DISPUTE_ANALYST`, I want to validate the worker report and submit a release/refund recommendation, so that automation informs but does not replace human judgment.
59. As a `PLATFORM_ADMIN`, I want a dashboard showing open, closed, at-risk, and overdue disputes, so that I can manage the operations queue.
60. As a `PLATFORM_ADMIN`, I want to review evidence, report, and analyst recommendation, so that I can make the final financial decision.
61. As a `PLATFORM_ADMIN`, I want to approve either release to the organization or refund to the customer, so that every dispute has one explicit terminal outcome.
62. As a platform stakeholder, I want analyst recommendation and admin approval separated, so that one person cannot both recommend and execute a disputed movement.
63. As a platform auditor, I want immutable records of state changes, evidence access, report validation, decryption, and financial decisions, so that actions remain attributable.
64. As a platform auditor, I want posted accounting entries to be immutable and corrected only by reversal, so that history cannot be rewritten.
65. As a platform auditor, I want every ledger transaction balanced by currency, so that accounting corruption is rejected at commit.
66. As a platform operator, I want repeated RabbitMQ messages acknowledged without repeated effects, so that at-least-once delivery remains safe.
67. As a platform operator, I want committed outbox events eventually published after broker recovery, so that a temporary RabbitMQ failure cannot strand a workflow.
68. As a platform operator, I want transient failures retried with bounded backoff, so that recoverable outages do not require immediate intervention.
69. As a platform operator, I want permanent or exhausted failures isolated in queue-specific DLQs, so that poison messages do not create retry loops.
70. As a platform operator, I want audited DLQ replay retaining the original message identity, so that recovery remains safe and traceable.
71. As a platform operator, I want structured logs and traces connected by correlation and causation IDs, so that I can follow one agreement across API, workers, database, and webhook.
72. As a platform operator, I want metrics for latency, queues, retries, DLQs, outbox age, risk results, ledger postings, and SLA, so that failures become visible.
73. As a platform operator, I want readiness and health checks, so that unavailable dependencies are distinguishable from a healthy application.
74. As a platform developer, I want sandbox controls to approve, reject, delay, and duplicate PIX callbacks, so that asynchronous edge cases are demonstrable.
75. As a platform developer, I want the entire stack started with Docker Compose, so that reviewers can run the project locally.
76. As a platform developer, I want Terraform to configure MiniStack SES, Secrets Manager, and KMS, so that local cloud resources are reproducible.
77. As a platform developer, I want Ceph to expose private S3-compatible evidence storage, so that object storage remains separate from MiniStack AWS emulation.
78. As a platform developer, I want CI to verify migrations, types, tests, images, Terraform, secrets, dependencies, and containers, so that main stays reproducible.
79. As a portfolio reviewer, I want one complete happy-path demonstration from API request to released funds, so that architecture claims are visible in working behavior.
80. As a portfolio reviewer, I want a documented mapping from local services to potential AWS services, so that I can evaluate cloud reasoning without a real deployment.
81. As an organization member, I want a comfortable Catppuccin Mocha interface with clear semantic status colors, so that long operational sessions remain readable.
82. As an operations user, I want a Custody Rail timeline, so that payment, risk, custody, inspection, dispute, and release states are understandable at a glance.
83. As a keyboard or assistive-technology user, I want accessible controls, tables, charts, focus handling, and reduced motion, so that the system remains operable without pointer-only interaction.
84. As a Portuguese-speaking user, I want the initial UI in pt-BR, so that domain actions and failures are clear.

## Implementation Decisions

- Build a realistic simulation only. Never process real funds or use real customer data.
- Use a B2B2C model: `Organization`, `OrganizationMember`, external `Customer`, and internal `PlatformStaff`.
- Implement a Python 3.13 and Django 5.2 LTS modular monolith with Django REST Framework and psycopg 3.
- Run API, risk worker, ledger worker, integration/notification worker, Celery Beat, and outbox publisher as separate processes from one codebase.
- Divide backend responsibilities into identity, organizations, agreements, payments, ledger, risk, disputes, integrations, notifications, and audit modules.
- Keep DRF views thin; serializers validate transport shape; application services own transactions/state transitions; selectors own non-trivial reads.
- Use direct Django ORM instead of generic repository abstractions. Do not implement full CQRS or event sourcing.
- Model `EscrowAgreement`, `Transfer`, `LedgerTransaction`/`LedgerEntry`, and `Dispute` separately.
- Use explicit agreement, transfer, and dispute state machines. Reject invalid transitions with HTTP `409`.
- Accept and snapshot a `delivery_window_days` value from 1–90 on agreement creation; on confirmed PIX, derive `delivery_due_at` from `confirmed_at + delivery_window_days`. Keep the seven-day inspection window, immediate release on acceptance, automatic release on inactivity, and automatic refund when delivery expires.
- Do not offer guaranteed post-release refunds in the MVP.
- Protect acceptance/dispute/scheduler races with PostgreSQL row locks, optimistic versions, uniqueness constraints, and a final ledger-state check.
- Implement an append-only double-entry ledger with deferred PostgreSQL balance validation, no cascading deletion, and reversing entries for corrections.
- Record confirmed PIX in `FUNDS_PENDING_RISK`; approval moves it to `ESCROW_LIABILITY`, rejection returns it through `PIX_CLEARING`, and manual review leaves it pending.
- Store money in integer minor units. Accept decimal strings at the API boundary. Support BRL and USD without transactional FX or combined balances.
- Make currency toggle display-only using timestamped simulated rates.
- Apply a snapshotted, configurable organization fee as integer basis points at release; seed `200` bps (2%) and calculate the minor-unit fee with `ROUND_HALF_UP`.
- Use RabbitMQ and Celery with explicit command/event exchanges, named queues, queue-specific DLQs, JSON-only messages, and no Celery result backend.
- Treat RabbitMQ as at-least-once. Use transactional outbox, publisher confirms, consumer inbox, unique message IDs, correlation/causation IDs, and idempotent effects.
- Retry transient failures five times with exponential backoff/jitter; route permanent or exhausted failures to DLQ; replay only through an audited command.
- Use periodic PostgreSQL deadline scans rather than long-lived Celery ETA tasks.
- Use deterministic, explainable, versioned risk policies. Funding produces approved, review-required, or rejected; `REVIEW_REQUIRED` expires 24 calendar hours after confirmed PIX and then safely rejects/refunds if an analyst has not decided.
- Generate a dispute risk report for every dispute, including explicit no-suspicion outcomes and informative duplicate-evidence, customer-history/timing, organization-history/rate, and timeline flags. It cannot decide or move funds.
- Combine risk and dispute analysis in `RISK_DISPUTE_ANALYST`; reserve final movement authority for `PLATFORM_ADMIN`.
- Apply a 72-hour calendar dispute SLA: on-track below 48 hours, at-risk from 48–72, overdue after 72, stopped by final admin decision.
- Expose REST JSON under `/api/v1`, generate OpenAPI, require API keys and idempotency keys for agreement creation and every money-affecting mutation, use cursor pagination, and return stable structured errors. A same-scope key with the same canonical payload returns its stored response; a different payload returns `409 idempotency_key_reused` with no side effect.
- Hash API keys, show values once, scope them (`agreements:write`, `agreements:read`, `payments:write`, `payments:read`, `webhooks:manage`), allow two active keys per organization, and support expiry/rotation/revocation.
- Deliver signed outgoing webhooks at least once, retry for 24 hours, preserve monotonically assigned per-agreement sequence, and expose replay. Sequence detects gaps but does not promise ordered arrival; consumers reconcile through the authoritative API snapshot.
- Use rate limits backed by Redis for B2B API, login, OTP, public checkout, and outgoing webhooks. Rate limits cannot determine financial correctness.
- Use Django Channels with Redis for WebSockets. PostgreSQL is authoritative; reconnect and sequence gaps trigger an HTTP refetch, with polling fallback.
- Use Django session cookies for human auth, CSRF, strict tenant authorization, and role-based least privilege. Provision demo platform staff only through an explicit idempotent management command with supplied secrets; never seed default credentials at startup.
- Validate strong passwords and check HIBP through five-character k-anonymity with padded responses. Fail closed in production registration and use a dev/test mock.
- Require name, email, external customer ID, and validated CPF/CNPJ. Encrypt sensitive values with MiniStack KMS envelope encryption/AES-GCM, store blind-index HMACs, mask by default, and audit decryption.
- Follow an OWASP ASVS 5.0 Level 2-inspired baseline without compliance claims. Include CSP, secure cookies, CORS/CSRF controls, SSRF-safe webhook validation, safe uploads, and CI security scans.
- Exclude ClamAV locally because of resource cost in a controlled fictional-data environment; keep production malware scanning deferred.
- Keep PostgreSQL authoritative; use Redis only for caches, Channels, rate limits, and non-financial short locks.
- Store private evidence in Ceph RGW; store metadata and SHA-256 in PostgreSQL; authorize every download. A customer may download only their own dispute evidence after checkout-token and fresh OTP verification, through a short-lived pre-signed URL with an audit event; organizations cannot download it.
- Use MiniStack only for SES, Secrets Manager, and KMS. Do not substitute its S3/SQS/RDS/ElastiCache for Ceph/RabbitMQ/PostgreSQL/Redis.
- Use Terraform against MiniStack with configurable endpoint and local ignored state. Document remote locked state and real AWS gaps as future work.
- Build one React 19.2 strict-TypeScript SPA with organization, operations, and checkout route shells.
- Use Bun for all frontend dependency installation, scripts, unit tests, Vite execution, and Playwright execution. Commit `bun.lock`; never document npm/pnpm/Yarn commands.
- Use React Router, TanStack Query, React Hook Form, Zod, Tailwind semantic tokens, Radix Primitives, Recharts, and an OpenAPI-generated client. Do not add Redux initially.
- Use Catppuccin Mocha with the “custody control room” direction, self-hosted Instrument Serif/Manrope/IBM Plex Mono, restrained motion, and the domain-specific Custody Rail.
- Keep ADRs, code, API, queue/event names, and technical documentation in English. Ship initial UI in pt-BR with an i18n-ready structure.
- Manage backend dependencies with `uv`; use Ruff, mypy/Django stubs, pytest, pytest-django, and Hypothesis.
- Run optional local observability with Prometheus, Grafana, OpenTelemetry Collector, Jaeger, and Flower.
- Deliver in tracer-bullet slices: full happy path first, dispute/human operations second, resilience/operational depth third.

## Testing Decisions

- Test externally observable behavior rather than framework calls, ORM method usage, Celery invocation details, or component internals.
- Use one primary acceptance seam: drive the system through the public organization API and hosted customer actions, then assert authoritative API state plus emitted webhook/status behavior. This seam covers the happy path and dispute path across real containers.
- Add one necessary lower seam for accounting: property/stateful tests post domain-approved ledger transactions and assert balanced entries, currency isolation, immutable history, reversal behavior, and duplicate-effect prevention.
- Unit-test pure agreement/transfer/dispute state transitions, fee calculations, risk policies, authorization decisions, serializers, signatures, and money parsing.
- Integration-test PostgreSQL constraints/triggers, transactional outbox/inbox, RabbitMQ routing/retry/DLQ, Redis degradation behavior, Ceph object authorization, MiniStack SES/Secrets Manager/KMS, and Terraform bootstrap.
- Contract-test OpenAPI request/response shapes, message envelopes/version compatibility, simulated PIX signatures, and outgoing webhook signatures/sequences.
- Use Playwright for customer, organization, analyst, and admin journeys, emphasizing visible outcomes and accessibility rather than DOM structure.
- Explicitly test duplicate and out-of-order messages, API idempotency retries, concurrent accept/dispute/auto-release, worker death, broker outage, database outage, stale outbox events, retry exhaustion, DLQ replay, WebSocket gaps, and webhook replay.
- Test tenant isolation and role restrictions at API boundaries, including masked PII, forbidden secret access, and audited decryption.
- Test the 48/72-hour SLA boundaries and scheduled delivery/inspection expiration using controlled clocks.
- Test display conversion separately from ledger currency to prove toggling cannot mutate financial state.
- Require at least 90% coverage in financial domain modules; do not use a global coverage target as a quality proxy.
- The repository has no implementation test prior art yet. ADR 0011 defines the initial testing standard; new tests should establish reusable fixtures/builders at the public API seam instead of creating multiple bespoke harnesses.

## Out of Scope

- Real money, real PIX providers, production support, or production customer data.
- Legal custody, licensing, validated KYC/AML, PCI, regulatory, privacy, or retention compliance.
- Credit/debit cards, installments, interest, and card antifraud.
- External bank payout/withdrawal from organization available balance.
- Transactional FX, combined-currency totals, or currencies beyond BRL/USD.
- Guaranteed refunds after release, reserves, clawbacks, or negative organization balances.
- Customer Escrow accounts.
- Organization registration email verification and two-factor authentication.
- Machine learning risk models and a visual risk-policy editor.
- Production antivirus/malware scanning.
- Advanced organization onboarding review, trust scores, complaint rankings, and broad analytics.
- Real AWS deployment, production IAM/networking, remote Terraform state, or a claim that MiniStack validates AWS security.
- Automatic RabbitMQ-to-SQS migration; that requires a separate architecture decision.
- Multi-repository microservices, full event sourcing, and generic repository abstractions.
- Additional UI locales and Storybook.

## Further Notes

- ADRs 0001–0013 are authoritative for initial architecture and scope. Future changes should supersede decisions with new ADRs rather than silently rewriting accepted records.
- The project and product name **Escrow** is provisional.
- All demo seeds and screenshots must use fictional identities and values.
- The README must include startup requirements, architecture diagrams, message flow, ledger examples, cloud mapping, security limitations, and an explicit “not production financial software” warning.
- Local Terraform state is intentionally accepted for MVP and must be called out as future work.
- Ceph makes the local stack resource-heavy; minimum RAM/CPU/disk expectations and optional Compose profiles must be documented.
- Potential cloud mapping is Django/Celery to ECS, PostgreSQL to RDS, Redis to ElastiCache, Ceph to S3, and MiniStack APIs to their AWS counterparts. RabbitMQ to SQS/SNS or Amazon MQ is a future design, not a drop-in replacement.
- Implementation order follows the accepted tracer-bullet strategy; reliability primitives are part of the first financial slice, not post-MVP cleanup.
