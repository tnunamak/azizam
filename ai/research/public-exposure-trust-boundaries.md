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
