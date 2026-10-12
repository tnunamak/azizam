---
title: Apps and keys
summary: What an app is, what a key proves, and why a personal key can read saved prompts.
---

# Apps and keys

An app is a client that uses the gateway. A key proves that a request may act as that app. An app can have more than one key, so you can replace a key without losing the app's history.

## Kinds of app

- A **personal app** belongs to a person.
- A **service app** belongs to a service account.
- A **session app** is how the browser interface acts for a signed-in person. It has no key, and a key for it is not accepted.

## App keys and admin keys

App keys run requests. Admin keys change the gateway's configuration. Neither one can do the other's job.

The gateway shows a new app key once, when it is created. It stores only a hash and a short prefix. Key lists never show the full key. Save the key somewhere safe, and never put it in a public page or a repository.

A revoked or expired key is refused. A key also needs an enabled app and a valid access plan.

## A personal key can read saved prompts

A personal app key acts as its person only when that same person created it. Anyone who holds such a key can read that person's saved prompts.

A service key, or a personal key that someone else created, does not act as a person. Prompts sent with it are not saved to anyone's prompt memory.

This does not prevent an app from explicitly saving a conversation. A request to responses with store: true can save a conversation until it is explicitly deleted.

Protect a personal key as you would your password. Read [Prompt saving](prompt-saving.md) for the retention choices, and [Apps & keys](../use/apps-and-keys.md) to create, rotate and revoke keys.
