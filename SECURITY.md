# Security Policy

## Reporting vulnerabilities

Please report security issues privately. Do not open a public issue for a
suspected vulnerability.

Preferred route: open a private GitHub security advisory draft for
`Vivid-Fish/azizam` at:

```text
https://github.com/Vivid-Fish/azizam/security/advisories/new
```

If GitHub private vulnerability reporting is not enabled for the repository,
use the maintainer's private contact channel from the project profile or ask for
a private channel without including exploit details in the public issue.

Send enough detail for a maintainer to reproduce and assess the issue:

- Affected version or commit.
- Deployment shape, without secrets.
- Exact route, request shape, or configuration involved.
- Expected result and observed result.
- Whether the gateway was exposed to a LAN or the internet.

Do not include real `opk_...`, `oak_...`, provider keys, database files, or
session cookies.

## Scope

Security reports are in scope when they affect:

- Gateway authentication or authorization.
- App API keys, admin keys, OIDC sessions, or local owner accounts.
- Cross-user isolation for jobs, assets, receipts, signed URLs, or media.
- Secret redaction in logs, docs, UI, exports, or API responses.
- Remote-provider spend controls or paid-model routing.
- Vendor-compatible public routes exposed by the gateway.

Reports about a downstream engine, model, or provider are usually out of scope
unless the gateway makes the issue worse or exposes it across users.

## Internet exposure

The gateway binds to `127.0.0.1:5000` by default. If you expose it outside the
host, put a TLS-terminating reverse proxy in front of it and keep auth enabled
unless the proxy enforces its own authentication layer.

See [reverse-proxy guidance](docs/REVERSE_PROXY.md) for health checks, streaming settings,
security headers, and the warning emitted when auth is disabled on a non-loopback
bind address.

## Supported versions

This repository is pre-1.0. Security fixes target the main development line
unless a release branch states otherwise.
