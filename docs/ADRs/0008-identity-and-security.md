# ADR 0008: Identity, PII, and security baseline

- Status: Accepted
- Date: 2026-07-18

## Context

The simulated system still needs credible authentication, tenant isolation, sensitive-data handling, and protection against threats introduced by webhooks and evidence uploads.

## Decision

### Human authentication

- Use email/password and Django server-side sessions in `HttpOnly` cookies.
- Require password confirmation during registration.
- Require at least 12 characters and Django checks for common passwords and similarity to user data.
- Check passwords against HIBP Pwned Passwords through k-anonymity: send only the first five SHA-1 characters and request padded responses. Never send the password or complete hash.
- If HIBP is unavailable, block production registration temporarily. Use a configurable mock in development/tests.
- Email verification by registration code and 2FA are roadmap items.

### Authorization

- Enforce tenant scope in backend services/querysets; frontend routes are not a security boundary.
- Organization members only access their organization.
- `RISK_DISPUTE_ANALYST` sees assigned queues, masked PII, relevant values, evidence, and reports, but not API secrets, organization keys, or global platform revenue.
- `PLATFORM_ADMIN` sees global operations, approves financial dispute decisions, manages staff, and accesses audit history.
- Decrypting customer identity requires an explicit reason and produces an audit event.

### Customer identity and PII

- Require organization `external_customer_id`, name, email, and valid CPF/CNPJ.
- Encrypt email and CPF/CNPJ at rest using envelope encryption with MiniStack KMS plus AES-GCM.
- Store ciphertext, nonce, and encrypted data key in PostgreSQL.
- Store a keyed HMAC blind index for cross-transaction fraud correlation without decryption.
- Mask CPF/CNPJ in normal UI/API output.
- Seed only fictional identities and warn against real data in documentation.

### Security baseline

- Use an OWASP ASVS 5.0 Level 2-inspired checklist without claiming certification or production compliance.
- Enable secure cookies, CSRF protection, restrictive CORS, CSP, and production HSTS/security headers.
- Validate configured webhook URLs against SSRF:
  - allow only HTTP(S), and HTTPS outside development;
  - reject loopback, link-local, private, reserved, and metadata addresses;
  - re-resolve DNS and validate every redirect;
  - set strict time and size limits.
- Validate evidence extension, magic-byte MIME, size, and generated object name.
- Do not add ClamAV to the local MVP because its memory cost does not justify value in a controlled, fictional-data environment. Keep malware scanning as a production-readiness roadmap item.
- Keep secrets out of Git and scan source, dependencies, and container images in CI.

### Data lifecycle

- Never hard-delete ledger entries, agreements, decisions, or audit events.
- Deactivate organizations and revoke their credentials.
- Support PII anonymization while retaining financial reference integrity.
- Make evidence retention configurable after resolution.
- Automatically remove expired OTP/token records.
- State explicitly that retention defaults are technical simulation choices, not validated legal compliance.

## Consequences

- Local development depends on MiniStack KMS, SES, and Secrets Manager.
- Encrypted fields require dedicated services/selectors; ordinary model serialization cannot expose raw data.
- Some availability is traded for password breach-check assurance at registration.
- Production use would require a new security, privacy, legal, and operational review.

