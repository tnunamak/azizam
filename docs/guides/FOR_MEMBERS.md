# Guide for members

Use this guide when someone gives you access to a Azizam.
The gateway gives your apps one AI endpoint and one key. Your owner decides
which models, capabilities, and remote providers your account can use.

## What you need

- A gateway link from the owner.
- An invitation link, or an account that already exists.
- The app that you want to connect, such as Open WebUI, LibreChat,
  SillyTavern, Cursor, Codex, Claude Code, Python, JavaScript, or `curl`.

Keep your API key, which starts with `opk_`, private by keeping it out of public files, chat messages, screenshots, and support tickets.

## Accept an invitation

1. Open the invitation link.
2. Create an account, or sign in with the configured identity provider.
3. Save the API key when the page shows it.
4. Continue to Home.

The key is shown once. If you lose it, create a new key from
`/ui/apps/`, or ask the owner to help you.

An invitation can be used once before it expires; if your link no longer works, request a new invitation from the owner.

## Connect an app from Home

1. Open `/ui/home/`.
2. Choose an app card.
3. Create or select a key.
4. Copy the base URL and key into your app.
5. Use **Gateway check**, then send a message from your app.

For OpenAI-compatible clients, use this base URL:

```text
https://YOUR-GATEWAY/openai/v1
```

For a local install, the owner can give you a URL such as:

```text
http://127.0.0.1:5000/openai/v1
```

That loopback URL works only on the device that runs the gateway. If your app
runs on another phone, laptop, container, or server, ask the owner for the
gateway URL that is reachable from that device.

**Gateway check** tests the gateway with a real request. It does not test your app settings; the message from your app checks those settings.

The page shows only capabilities that your access plan and the gateway can use.
If the gateway has no available model for a capability, that capability can be
absent or unavailable.

## Manage your app keys

Open `/ui/apps/` to see your apps and keys.

You can:

- create a key for a new app;
- rotate a key;
- revoke a key;
- copy the gateway base URL for an app.

Use one key per app. This makes it easier to revoke one app without breaking
the others.

If a key is revoked, disabled, or expired, the app that uses it stops working.
Create a new key and update the app.

## What your access plan controls

Your owner assigns an access plan that controls which capabilities you can use: chat, images, video, speech, transcription, decisions, embeddings, and reranking. The plan can also restrict model names, local or remote locations, app protocols, workflows, and engines. Paid remote providers have separate permissions and spending limits.

Remote providers can cost money. The gateway does not silently switch from a
free local model to a paid remote provider. A paid provider must be configured
by the owner, named by the model or alias, and allowed by your access plan.

## Use generated media

Generated images, video, and audio can appear in `/ui/library/` when those
capabilities are configured and allowed for your account.

Some result URLs are signed download links. A signed link can be opened without
your API key until it expires. Share those links only with people who should
see the file.

## View usage and receipts

Open `/ui/usage/` to see your usage when measurement data exists. Some counts
can say `Not measured yet` when the gateway does not have a complete source
for that value.

Requests can have receipts. Receipts show what happened for a request, such as
the model, time, and cost information when the gateway has it.

## Common problems

### The invite link fails

The link can be expired, revoked, or already used. Ask the owner for a new
invitation.

### The app says the key is invalid

Check that you copied the full `opk_...` key. If the key was revoked or lost,
create a new key in `/ui/apps/` and update the app.

### The app cannot connect

For an OpenAI-compatible app, check that the base URL ends with `/openai/v1` and is reachable from the device running the app.

### A model is missing

Your access plan can restrict models. The gateway also only lists models that
are configured. Use the alias or model name that the owner recommends.

### A paid or remote model is blocked

Your access plan must allow that provider and model. The provider also needs a
configured key and available spend budget.

### Media does not appear

The capability can be disabled, unavailable, or outside your access plan. The owner can check the engine, workflow, and access plan to find the cause.

## Links

- [Local client recipes](../LOCAL_CLIENT_RECIPES.md)
- [People and built-in accounts](../PEOPLE_AND_ACCOUNTS.md)
- [Remote providers](../REMOTE_PROVIDERS.md)
- [Public API support matrix](../SUPPORT_MATRIX.md)
