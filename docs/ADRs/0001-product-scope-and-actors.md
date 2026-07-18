# ADR 0001: Product scope and actors

- Status: Accepted
- Date: 2026-07-18

## Context

The project is a capstone portfolio project. It should demonstrate the engineering concerns of a financial escrow platform without regulatory exposure, production support, or real money movement.

The initial B2B contract concept evolved into a B2B2C platform integrated by marketplaces, online stores, and other selling organizations. The buyer is a customer of the organization, not a direct registered user of Escrow.

## Decision

- Use **Escrow** as the working project and product name.
- Build a realistic simulation only. Never accept real payments, real credentials, or real customer data.
- Model these actors:
  - `Organization`: merchant or marketplace integrating with Escrow.
  - `OrganizationMember`: human dashboard user with `OWNER`, `FINANCE`, `SUPPORT`, or `VIEWER` role.
  - `Customer`: external buyer with no Escrow account.
  - `PlatformStaff`: internal user with `PLATFORM_ADMIN` or `RISK_DISPUTE_ANALYST` role.
- Organizations integrate through API keys and receive signed webhooks.
- Customers use a hosted checkout/status link and authorize sensitive actions with email OTP.
- Support simulated PIX only in the MVP. Keep the payment-provider boundary extensible for cards, debit, installments, and interest later.
- End the MVP settlement flow at the organization's internal available balance. External bank payout is out of scope.
- Use only fictional seeded/demo data.

## Consequences

- The product needs three UI surfaces: organization dashboard, platform operations dashboard, and customer checkout/status.
- Organization integrations are first-class product behavior, not a dashboard-only shortcut.
- The repository and documentation must state prominently that this is not production-ready financial software.
- Card processing, PCI concerns, real KYC/AML, licensing, and real custody remain outside project claims.

