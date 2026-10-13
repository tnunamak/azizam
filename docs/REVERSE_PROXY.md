# Reverse Proxy and Deployment Security Guide

Azizam binds to `127.0.0.1:5000` by default. The expectation
for LAN or internet exposure is that a TLS-terminating reverse proxy (Nginx,
Caddy, Traefik, HAProxy) sits in front.

## Health Checks

For client-address trust, first-owner claiming and the family-access checklist,
read [Public exposure](ops/PUBLIC_EXPOSURE.md). The gateway applies forwarding
trust itself; custom Uvicorn launch commands must use `--no-proxy-headers`.

Use `GET /health` for reverse-proxy health checks. It returns a JSON body with
auth and credential status.

Do **not** use `HEAD /`, `GET /` or `HEAD /admin` as health check targets.
`/` is the web app's Home page: it answers `503` when the web build is missing,
which would mark a working API as down. `HEAD /admin` answers `405`. If your
probe used `HEAD /admin` or `HEAD /`, change it to `GET /health` before you
deploy the release that moves the web app to `/`.

## Recommended Nginx snippet

```nginx
# Put map and upstream in the http context.
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

upstream gateway {
    server 127.0.0.1:5000;
}

server {
    listen 443 ssl;
    server_name ai.example.com;

    # TLS config omitted — use certbot/acme

    location / {
        proxy_pass http://gateway;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # Streaming responses — disable buffering for SSE/chunked
        proxy_buffering off;
        proxy_read_timeout 600s;
    }

    # Tighter health check probe (no auth required)
    location = /health {
        proxy_pass http://gateway;
        access_log off;
    }
}
```

WebSocket smoke check:

```bash
export AZIZAM_API_KEY='opk_...'
wscat -c 'wss://ai.example.com/v1/realtime?model=gpt-realtime' -H "Authorization: Bearer $AZIZAM_API_KEY"
```

With `wscat` installed and a configured Realtime backend, the check should
complete the WebSocket upgrade with HTTP 101.

## Recommended Caddy snippet

```caddyfile
ai.example.com {
    reverse_proxy 127.0.0.1:5000 {
        flush_interval -1   # enable streaming
        transport http {
            read_timeout 600s
        }
    }
}
```

## Security Headers

The gateway injects the following headers on all HTML/admin/portal responses:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `SAMEORIGIN` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'` |

`unsafe-inline` is required because the admin UI uses inline scripts and styles.
If you need a stricter CSP for compliance purposes, add nonce-based overrides
at the reverse-proxy layer — do not remove `unsafe-inline` without auditing all
inline script/style usage in the UI first.

Your reverse proxy may add its own headers (e.g. HSTS). Use `add_header` /
`header` directives at the proxy layer; the gateway headers are defaults and
will not override what the proxy sets.

## App keys and trusted addresses

Authentication cannot be disabled. Remove `auth.enabled`, `auth.lan_bypass_*`
and `auth.ollama_compat_discovery`, along with their old environment overrides.
They are no longer supported.

Protected API requests without a key return `401 key_required`, unless an
owner added the client address to an App under **Apps & keys → app menu →
Trusted addresses**. The request then uses that App's access plan. A sent key
always wins. A placeholder such as `loopback` returns `401 key_invalid`, even
from a trusted address.

`X-Forwarded-For` counts only when the connecting proxy is listed in
`auth.trusted_proxies`. Configure proxy trust narrowly. A reverse proxy or a
LAN address does not grant access by itself. Keep the gateway port protected
from untrusted networks.
