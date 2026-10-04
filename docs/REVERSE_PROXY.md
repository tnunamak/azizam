# Reverse Proxy and Deployment Security Guide

Vivid Fish AI Gateway binds to `127.0.0.1:5000` by default. The expectation
for LAN or internet exposure is that a TLS-terminating reverse proxy (Nginx,
Caddy, Traefik, HAProxy) sits in front.

## Health Checks

For client-address trust, first-owner claiming and the family-access checklist,
read [Public exposure](ops/PUBLIC_EXPOSURE.md). The gateway applies forwarding
trust itself; custom Uvicorn launch commands must use `--no-proxy-headers`.

Use `HEAD /` or `HEAD /admin` for reverse-proxy health checks. Both return
`200` with no body. The dedicated `GET /health` endpoint returns a JSON
body with auth and credential status and is also suitable for upcheck polling.

Do **not** use `GET /` or `GET /admin` as health check targets from a reverse
proxy: those paths return HTML and require admin auth respectively.

## Recommended Nginx snippet

```nginx
upstream gateway {
    server 127.0.0.1:5000;
}

server {
    listen 443 ssl;
    server_name ai.example.com;

    # TLS config omitted — use certbot/acme

    location / {
        proxy_pass http://gateway;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

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

## auth.enabled=false Warning

If `auth.enabled` is set to `false` **and** the gateway is bound to a
non-loopback address (anything other than `127.0.0.1`, `::1`, or `localhost`),
the gateway will print a loud startup warning:

```
[proxy] WARNING: auth is disabled and the gateway is bound to '0.0.0.0' ...
```

Deploying with auth disabled and a non-loopback bind is only safe when:
- The reverse proxy enforces its own authentication layer, **and**
- The gateway port is firewalled from untrusted networks.

To silence the warning legitimately: keep `auth.enabled=false` but set
`proxy.host` to `127.0.0.1` and let the reverse proxy handle TLS + auth.

To enable auth: set `auth.enabled=true` in `proxy_config.yml` or set the
environment variable `OPENAI_PROXY_AUTH_ENABLED=true`.
