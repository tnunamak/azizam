---
title: Security
summary: Control who can set up the gateway, protect keys, and open it safely to other devices.
---

# Security

Keep access closed until you have decided who may use the gateway. A reachable page, a home network address or a healthy engine is not permission to run requests.

## Who can create the owner account

Setup creates the owner account. It works only while the gateway has no owner or admin, and only from allowed networks:

- Loopback is always allowed.
- `AZIZAM_SETUP_ALLOWED_CIDRS` lists more networks. It overrides `auth.setup_allowed_cidrs` in the configuration file. If neither is set, setup uses `auth.lan_bypass_cidrs`.
- In Docker Compose, setup is allowed only from the computer that runs the containers. Compose sets `AZIZAM_SETUP_ALLOWED_CIDRS` itself.

Setup permission does not allow anyone to run requests without a key.

## Keys

- **App keys** run requests. Give each app its own key, so you can revoke one without breaking the others.
- **Admin keys** change the gateway's configuration. Keep them out of apps.

Protect every key as you would a password. Do not put keys in URLs, screenshots, shared logs or support reports.

## Open the gateway to other devices

The default installation listens only on the computer's loopback address. Before other devices connect:

1. Put HTTPS in front of the gateway, for example with a reverse proxy.
2. Turn on secure cookies: in the configuration file, set `admin.oidc.cookie.secure` to `true`. In Docker Compose, the configuration file is `first-run.yml` in the repository, and the change needs a rebuild with `docker compose up -d --build`.
3. Keep the gateway's own port and your engines' ports closed to the outside.
4. Test access through the public address, not only through loopback. A healthy local check does not prove that the reverse proxy reaches the gateway.

## What the gateway sends to engines

The gateway forwards only `Content-Type`, `Accept` and `User-Agent` from the client, with `Accept-Encoding: identity`. It does not forward gateway cookies, CSRF tokens, internal headers or credentials in query parameters.

To authenticate the gateway to an engine, put the engine's key in an environment variable of the gateway, and enter the variable's name in **Key environment variable** for that engine. Do not write keys into the configuration file.

## Fetching media from URLs

When a request gives a media URL, the gateway fetches it with limits. It does not fetch from private network addresses unless you list the exact host in `AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS`. Even then, it does not fetch from loopback, link-local or cloud metadata addresses. See [Configuration](../reference/configuration.md#media-downloads).

## Single sign-on button label

In **Settings → Sign-in**, the owner can set the name of the identity provider. It appears on the sign-in button. A saved value takes precedence over `AZIZAM_ADMIN_OIDC_DISPLAY_NAME`. An empty value shows the generic single sign-on label. The name can have up to 80 characters, with no angle brackets or control characters.

## Check that denial works

Before you open the gateway to other people:

- Send a request without a key, and check that it is refused.
- Sign in as a member, and check that they see only their own work.

## Report a problem safely

When you report a failure, include the error code, the message and the layer. Remove keys, cookies, private addresses, prompts and personal data first.
