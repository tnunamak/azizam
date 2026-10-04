# Public exposure and reverse-proxy trust

Date: 2026-10-03. Scope: the exposure review and its isolated lane fixes.

## Question and local corpus

Can a public client inherit anonymous LAN access through a reverse proxy? Does
the server preserve the socket address used by the application trust check?

Read `ai/research/INDEX.md` before searching. The account-authentication and
provider-key-custody entries cover password work, links and encrypted secrets.
They do not cover client-address replacement by Uvicorn or Traefik's forwarded
header settings. Also read `docs/REVERSE_PROXY.md`, `gateway/admin_oidc.py`, the
gateway authentication functions, and the pinned hermetic dependency lock.

## Primary sources and findings

| Source read | Relevant detail | Decision |
| --- | --- | --- |
| [Uvicorn settings](https://www.uvicorn.org/settings/) | Search results describe proxy headers as populating the remote address and `forwarded-allow-ips` as the trust list. Direct page fetch returned 502. | Do not use the failed fetch as implementation proof; inspect the pinned source below. |
| [Uvicorn deployment](https://www.uvicorn.org/deployment/) | Search results warn that proxy-header behavior may be insufficient for multiple intermediary services. Direct page fetch timed out. | Keep application trust independent of server header rewriting. |
| [Uvicorn 0.33.0 configuration](https://raw.githubusercontent.com/encode/uvicorn/0.33.0/uvicorn/config.py) | This is the version in the hermetic lock. `proxy_headers` defaults to true. An unset forwarded trust list resolves from `FORWARDED_ALLOW_IPS`, with loopback as its fallback. Configuration wraps the application with `ProxyHeadersMiddleware` when enabled. | Explicitly disable this wrapper in both supported launch commands. Environment-wide trust must not change the application's security boundary. |
| [Uvicorn 0.33.0 proxy middleware](https://raw.githubusercontent.com/encode/uvicorn/0.33.0/uvicorn/middleware/proxy_headers.py) | The middleware handles HTTP and WebSocket scopes. For a trusted peer it uses forwarding headers to replace `scope["client"]`, and may also replace the scheme. Wildcard trust accepts the supplied forwarding chain. | The gateway must receive the original peer address before applying its own trusted-proxy policy. TLS terminates at the proxy; cookie security must remain explicit. |
| [Traefik entrypoints](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/) | `forwardedHeaders.trustedIPs` limits whose forwarding data is trusted. `forwardedHeaders.insecure` accepts forwarding data without that restriction. The reference also documents `notAppendXForwardedFor`, which can preserve a supplied chain without adding the client address. | Name actual ingress peers, leave insecure trust off, and verify that each public request retains an untrusted public address in the chain. Do not enable forwarding preservation without understanding its effect. |

Relevant Traefik excerpt: “Set the insecure mode to always trust the forwarded
headers information”. The gateway's equivalent policy must never silently turn
an unknown client into the LAN reverse proxy.

## Reproduction and disagreements with initial hypotheses

The old gateway resolver returned its trusted LAN proxy's address when XFF was
missing or empty. It skipped malformed entries, so a chain ending in an invalid
entry could resolve to an earlier private address. Five isolated regression
cases failed before the fix, including missing/malformed Cloudflare metadata.

Anonymous LAN authorization also preceded explicit credentials. That discarded
the member's access plan and ownership, and accepted invalid keys on the LAN.
The fix validates an explicitly supplied key first and keeps anonymous LAN
access for requests that omit credentials.

The public edge probes did not reproduce a public-to-LAN bypass: inference
requests returned 401, and spoofed `CF-Connecting-IP` requests returned 403.
These results describe the observed ingress; they do not disprove the local
resolver defect when proxy metadata is absent or malformed.

An initial hypothesis that SDAPI and image-edit bodies were parsed before auth
was rejected after reading their wrappers. Both authenticate before body/form
parsing. Missing body caps remain an authenticated-member resource concern.

## OIDC callback possession

The local account-authentication entry did not address browser binding for
pending OIDC callbacks. Read the existing OIDC state/nonce/PKCE implementation,
its integration tests, and these additional primary sources:

| Source read | Relevant detail | Decision |
| --- | --- | --- |
| [OWASP OAuth2 Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/OAuth2_Cheat_Sheet.html), Essential Basics and PKCE | State tokens must bind to the initiating user agent. Nonce or PKCE provides CSRF protection only with transaction and browser binding. PKCE also requires provider support. | Server-side nonce and verifier storage alone does not prove that the callback browser started the flow. |
| [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html), sections 2.1.1, 4.5.3.2 and 4.7 | Defines callback injection as causing a victim to access attacker-controlled resources. Requires transaction-specific PKCE/nonce bound to the initiating user agent. Section 4.5.3.2 explains that the nonce must connect to the browser session, not just match an ID token. | Add browser possession before code exchange; retain nonce, PKCE, JWT verification and one-time state validation. |

OWASP excerpt: “securely bound to the client and the user agent in which the
transaction was started”. RFC 9700 excerpt: “bind it to the user agent session”.
Both sources agree; neither says a process-global state dictionary establishes
that binding. This is an implementation inference tested against the gateway.

The corrected two-client baseline returned 302 and created a session in the
browser that did not initiate login. The first test fixture mistakenly excluded
its fake member's email domain, causing positive cases to fail independently;
the corrected fixture produced one attack failure and two passing controls.
The exploit needs a valid unused code/state from the attacker's own flow, not
knowledge of the victim's password. Its impact is session swapping: subsequent
victim work is attributed to the attacker's account. No live IdP exchange or
account change was attempted.

Each pending flow now has an independent random cookie; its hash is stored
with the state. Cookie names separate parallel tabs. Secure deployments use
the `__Host-` prefix, omit Domain and use Path=/, HttpOnly and SameSite=Lax.
The callback checks possession and its configured authority before consuming
state or exchanging a code. Invalid possession cannot burn the real flow.
Redeemed flows clear their cookie; other tabs retain theirs.

Host-only cookies require starting on the configured callback host. Alias
starts therefore redirect there before minting state. Independent review found
that comparing raw authorities could loop for an explicit HTTPS port 443;
comparison now normalizes default ports. It uses the configured callback scheme
to interpret an omitted port because ASGI sees HTTP behind TLS termination.
The callback URI remains pinned per flow across Settings edits. Unbounded
server-side state retention and start throttling remain separate abuse concerns.
External provider redirects, browser cookie-policy enforcement and Docker NAT
behavior were not exercised against a deployment.

## Limits and capture

Probes originated on the LAN but connected to public Cloudflare addresses with
`curl --resolve`, preserving hostname and TLS verification. This crosses the
public edge; it is not a test from a separate off-LAN machine. Split-horizon
DNS otherwise resolved the public names to the LAN reverse proxy.

No live configuration, credentials, database contents, or service-manager state
were read. Public route responses were discarded; only status, peer, response
length and content type were recorded. Initial probes included several wrong
route names; those were corrected. Transport failures were retained and retried
against the other public address, rather than counted as auth refusals.

The detailed private deployment evidence is in the requested
`EXPOSURE_REVIEW.md` and JSONL files under the brief's report directory. The
generic operator instructions are in `docs/ops/PUBLIC_EXPOSURE.md`.
