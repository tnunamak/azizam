# Exposing the gateway to family

Keep a new instance private until the owner account is claimed. Invite members
with an access plan that permits the capabilities they need. Public inference
requests need an App key; a browser session grants access to the person's own
account pages and does not grant anonymous inference access.

## Preserve the client-address boundary

The supported Python and Compose launch paths disable Uvicorn's forwarding
header middleware. The application resolves the client address from the socket
peer and its own `auth.trusted_proxies` configuration. If you supply a separate
Uvicorn command, include `--no-proxy-headers`.

Set `auth.trusted_proxies` to the actual reverse-proxy addresses. Do not use a
whole LAN or a wildcard as a proxy trust list. The proxy must add the address
of the client it receives. Missing, malformed or wholly trusted forwarding
chains cannot grant anonymous LAN access. Cloudflare's client-IP header is
used only for a socket peer verified against Cloudflare edge ranges. A LAN
proxy forwarding that header does not become a Cloudflare edge.

At Traefik, leave `forwardedHeaders.insecure` off. Trust only the upstream
ingress that you control, such as the edge network. Verify both public-edge
and direct LAN requests after changing forwarding settings. See the
[Traefik entrypoint reference](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/).

There is no anonymous LAN access. A request from the LAN, loopback or a
tailnet needs an App key like any other, and uses that App's identity; an
invalid or revoked key fails, and so does a placeholder such as `lan`,
`sk-local` or `EMPTY` (`401 key_invalid`). The client address decides only
throttling and which networks may claim the first owner. Give each device and
each family member their own App and key.

## Claim the first owner

Password-based first-owner claiming accepts a resolved loopback client or a
client in `auth.setup_allowed_cidrs` (`AZIZAM_SETUP_ALLOWED_CIDRS`). When
that setting is absent, only loopback is accepted. Public or unresolved clients receive 403. A browser
form from a different origin is also refused. Setup accepts `localhost`, an
IP-literal Host, or the configured public base URL's hostname; arbitrary
hostnames cannot use DNS rebinding to pass the setup check. The database commits the first
owner and password together, so concurrent setup requests cannot create two
first owners or replace the winner's password.

Compose defines a bridge network and grants setup to its exact host gateway
address. The published port remains loopback-only, and this grant admits no
inference access. If the default subnet conflicts with your networks,
set `AZIZAM_DOCKER_SUBNET` and `AZIZAM_DOCKER_GATEWAY` together.

If you add a host reverse proxy, include its translated socket peer in
`auth.trusted_proxies` and require it to forward the real client address;
otherwise its requests inherit the host's setup allowance. Claim the owner
privately before exposing that proxy. For another container layout, configure
the exact intended setup peer; do not trust a whole bridge subnet. Use the
existing admin-key recovery flow for a remote-only installation. That flow
still requires the admin credential; this change adds no setup token or
secret environment variable.

## Operator checklist

- Require TLS. Keep browser cookies `Secure`, `HttpOnly`, and `SameSite=Lax`
  or stricter. Use a strong owner password and protect account recovery links.
- Register the exact configured OIDC callback URL with the identity provider.
  Sign-in through an alias first redirects to the callback host, where a
  host-only, ten-minute cookie binds that flow to its initiating browser.
  Finish pending sign-ins before updating the gateway, or start them again.
- Firewall the gateway's listening port so public clients cannot skip ingress.
  Leave query credentials off unless their URL disclosure risks are
  acceptable.
- Use member-owned keys and explicit access plans. Remote providers require an
  explicit grant and spend limits; keep remote grants empty for local-only
  members.
- Set request/upload byte limits, read timeouts, and rate/concurrency limits at
  ingress. Media admission does not provide a per-member chat or Realtime
  session limit. Protect login and OIDC start routes from sustained abuse.
- Decide which discovery information may be public. `/docs`, `/openapi.json`,
  the discovery documents and static UI are public. Signed media URLs are
  bearer capabilities: anyone holding an unexpired URL can download it.

## Recheck the public edge

Resolve the hostname through public DNS first; local DNS may point directly
at a LAN proxy. The credential-free probe discards response bodies and sends
only invalid inference payloads and MCP initialization. It does not attempt
sign-in, upload media, or change an account.

```sh
uv run --no-project scripts/security/exposure_probe.py \
  --host gateway.example --edge-ip PUBLIC_EDGE_IP --lan-ip LAN_PROXY_IP
```

Check every protected protocol for 401 without a key. Check spoofed LAN
forwarding headers for the same refusal, and owner pages for login redirects
or 401. A timeout, 404 or 405 does not prove a route's authorization. Perform
authenticated invite, revocation, CSRF and member-isolation checks against a
throwaway instance, so they cannot lock out a real owner.
