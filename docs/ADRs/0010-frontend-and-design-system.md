# ADR 0010: React frontend and visual direction

- Status: Accepted
- Date: 2026-07-18

## Context

The product has organization, platform-operations, and customer workflows. It needs a distinctive but comfortable Catppuccin Mocha interface rather than a generic crypto/fintech dashboard template.

## Decision

### Application structure

- Build one React 19.2 + strict TypeScript SPA with Vite.
- Use route shells:
  - `/dashboard/*` for organizations;
  - `/ops/*` for platform staff;
  - `/checkout/:token` for external customers.
- Organize frontend code by feature/domain and share a small design system.
- Use React Router, TanStack Query, React Hook Form, Zod, and an OpenAPI-generated API client.
- Do not add Redux in the MVP.

### Bun-only frontend toolchain

- Use Bun as package manager, script runtime, and unit-test runner.
- Commit `bun.lock` and pin Bun in Docker and CI.
- Run Vite and Playwright through Bun.
- Use `bun:test`, React Testing Library, and Happy DOM; do not use Vitest.
- Documentation and CI must not contain npm, pnpm, or Yarn commands.

### Visual direction

Use a “custody control room” direction:

- Operations dashboards are desktop-first, dense, calm, and precise.
- Organization dashboards prioritize held balance, available balance, and scheduled-release calendar.
- Customer checkout is mobile-first, spacious, and plain-language.
- Avoid neon crypto styling, decorative gauges, excessive gradients, and generic card grids.
- Use a **Custody Rail** as the signature component: a continuous financial timeline across payment, risk review, custody, inspection, dispute, and release/refund.
- Use motion for meaningful state transitions only and honor `prefers-reduced-motion`.

Catppuccin Mocha tokens provide dark surfaces and semantic accents:

- yellow for processing/attention;
- green for approved/released;
- red for rejected/risk/dispute/overdue;
- mauve for primary interactive actions.

Typography:

- Instrument Serif for brand and selected key headings.
- Manrope for navigation, forms, tables, and prose.
- IBM Plex Mono with tabular figures for money, IDs, timestamps, and scores.
- Self-host all fonts.

Use Tailwind CSS with semantic CSS variables and Radix Primitives for accessible behavior. Create visual components specifically for this domain, including `Money`, `StatusBadge`, `CustodyRail`, `SlaClock`, and `RiskScore`. Do not apply a stock shadcn theme.

### Data visualization

- Use Recharts for restrained bar, line, and aging-bucket views.
- Always pair a chart with a textual summary and accessible table.
- Put urgent SLA items in actionable queues, not only charts.
- Avoid 3D charts, decorative donuts, and gauges.

### MVP screens

- Authentication: registration, login, password recovery.
- Organization: financial overview, agreements, agreement detail/timeline, releases, API keys, webhook configuration/logs, members.
- Sandbox: create charge and control simulated PIX outcome.
- Customer: checkout, status, OTP, accept delivery, open dispute, upload evidence.
- Analyst: SLA overview, manual funding queue, dispute queue, risk report, recommendation.
- Admin: executive/SLA overview, decision queue, dispute decision, audit log.

## Consequences

- One deployable frontend supports all audiences while backend authorization remains authoritative.
- Shared visual components reduce duplication between analyst and admin dashboards without merging permissions.
- The frontend design carries a specific escrow identity; implementation must be visually reviewed rather than accepted from component defaults.
- Advanced organization analytics and trust scoring remain outside the MVP.

