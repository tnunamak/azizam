# MCP authorization

Status: implemented (gateway/mcp_oauth.py, gateway/mcp_oauth_store.py,
gateway/mcp_server.py). Checked against the MCP specification revision
2026-07-28 on 2026-10-13.

## Goal

Any MCP client that follows the specification can connect, with no
client-specific code. ChatGPT and Claude.ai cannot send a header, so they need
OAuth. Header-capable clients may keep using app keys.

## Shape

The gateway is both the protected resource (`/mcp`) and its authorization
server (issuer = the saved public address).

| Step | Endpoint | Standard |
|---|---|---|
| 401 challenge with `resource_metadata` and `scope="mcp"` | `POST /mcp` | RFC 6750, RFC 9728 5.1 |
| Protected-resource metadata, path and root forms | `/.well-known/oauth-protected-resource[/mcp]` | RFC 9728 |
| Authorization-server metadata | `/.well-known/oauth-authorization-server` | RFC 8414 |
| Client identity | `https` client_id documents, or `POST /oauth/register` | CIMD draft, RFC 7591 |
| Authorization, PKCE S256 only, `iss` on every response | `GET /oauth/authorize` | OAuth 2.1, RFC 7636, RFC 9207 |
| Consent | web page `/authorize/`, `GET/POST /me/api/mcp/requests/{id}` | |
| Tokens, rotating refresh tokens | `POST /oauth/token` | OAuth 2.1, RFC 8707 |
| Revocation | `POST /oauth/revoke`, Connect page | RFC 7009 |

Authlib supplies the protocol machinery (grant validation, client
authentication, PKCE, RFC 7591 metadata validation, RFC 7009). Its authorization
server core is framework-neutral; `mcp_oauth.py` adapts it to FastAPI.

## Decisions

- **Azizam is the authorization server, not the OIDC provider.** The owner
  wants one consent screen in Azizam that ties the token to the signed-in person
  and their plan, and revocation in Azizam. The existing browser sign-in (OIDC or
  local) only authenticates the person. Provider tokens never reach MCP clients.
- **A token acts as the person's signed-in identity** (their session App), so
  `_runtime_app_profile` re-reads their plan and account status on every call.
  A plan change or removal applies at once. Approval never raises a plan.
- **Opaque tokens, hashed at rest.** Every check is a database lookup, so
  revocation is immediate. No signing keys to manage.
- **Audience binding.** A code and its tokens are bound to the canonical `/mcp`
  URL. The token is refused on every other route, except the in-process calls
  the MCP adapter makes, which carry a per-process secret header. An OAuth token
  never falls through to the API-key, LAN or path-token rules.
- **Replay ends the connection.** A reused code or refresh token revokes the
  grant (RFC 9700 4.14). Exactly one concurrent redemption wins.
- **Registration.** CIMD is preferred by the specification and is supported for
  public clients (`none`; a document that also lists `none`, as ChatGPT's does,
  is accepted). DCR stays for clients that need it; it is deprecated in MCP but
  still widely used. `private_key_jwt` is not offered.
- **Consent safety.** The consent request is bound to the browser that started
  it (a cookie), needs the session CSRF token, is single-use and expires in ten
  minutes. The page names the domain that serves a client's metadata document or
  warns that a self-registered client is unverified, shows where the browser
  will return, and focuses Cancel. Invalid clients and redirect URIs get a local
  error page, never a redirect.
- **Sign-in needs https.** OAuth 2.1 allows plain http only on loopback.
  Without a public https address the metadata omits the authorization server
  and API keys keep working.
- **Transport.** Streamable HTTP only. The legacy HTTP+SSE transport is not
  added: no current client needs it exclusively (ChatGPT, Claude.ai, Claude Code,
  Cursor, VS Code and Goose all document Streamable HTTP), and adding it would
  bring session state this server does not otherwise need.

## Configuration

`mcp.oauth` in `proxy_config.yml` (all optional):

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Offer OAuth sign-in |
| `dynamic_client_registration` | `true` | Offer `POST /oauth/register` |
| `access_token_seconds` | 3600 | Access token lifetime |
| `refresh_idle_seconds` | 2592000 | Refresh token lifetime without use (30 days) |
| `refresh_max_seconds` | 7776000 | A connection's absolute refresh lifetime (90 days) |
| `code_seconds` | 60 | Authorization code lifetime |
| `request_seconds` | 600 | Time to answer the consent page |

## Known limits

- Signed asset URLs handed out before a revocation stay valid until they expire.
- The capability catalog the `capabilities` tool lists is not filtered by plan;
  the core routes enforce the plan when work is started.
- Live ChatGPT and Claude.ai connections need the public deployment; the gate
  proves the protocol with the official conformance suite and scripted
  handshakes (tests/test_mcp_oauth_wiring.py), not with those products.

## Conformance

`scripts/ci/mcp-conformance` (a gate step) runs the pinned
`@modelcontextprotocol/conformance` against the production router and
authorization server. The baselines in `tests/conformance/` list only optional
features not offered and checks that need the suite's own diagnostic tools.
