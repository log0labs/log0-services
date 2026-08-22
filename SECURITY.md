# Security Policy

log0 is a multi-tenant log-intelligence and incident platform, so we take
security - especially **tenant isolation** - seriously.

## Reporting a vulnerability

**Please do not report security issues through public GitHub issues, pull
requests, or discussions.**

Instead, report privately by email to **ashmitgupta.official@outlook.com**. If possible,
include:

- a description of the issue and its impact,
- the affected service(s) and version/commit,
- clear steps to reproduce (a proof of concept helps),
- any suggested remediation.

We will acknowledge your report, keep you updated on progress, and coordinate a
disclosure timeline with you. Please give us a reasonable window to fix the issue
before any public disclosure. We're grateful for responsible disclosure and will
credit reporters who wish to be acknowledged.

## Scope

Issues we especially want to hear about:

- cross-tenant data access or any break in tenant isolation,
- authentication/authorization bypass (JWT handling, API keys, RBAC),
- remote code execution, injection, or SSRF,
- exposure of secrets or credentials.

## Supported versions

log0 is under active development. Security fixes target the `main` branch and the
latest release. Please test against the latest `main` before reporting.
