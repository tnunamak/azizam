---
title: Privacy and the owner
summary: What other people on the gateway can see, and what the owner can always read.
---

# Privacy and the owner

The gateway keeps creations and saved prompts under separate access rules. These rules control the application. They do not hide anything from the person who runs the computer.

## What other people see

- Library and Usage start with your own work.
- Members cannot see other people's work.
- An admin role alone does not show other people's Library.
- An owner or a superadmin can see other people's creations and usage. The gateway records requests to the everyone Library and Usage views, and requests for download links from the everyone Library view.
- The list of running jobs stays personal, for the owner too.

## Saved prompts

Library shows a saved prompt only to the person who made the creation. When the owner opens someone else's creation, they do not see its prompt.

**Download with details** includes the saved prompt and workflow. Only the person with that saved prompt can download it.

A personal app key can read its owner's saved prompts only when that same person created the key. A personal key created by someone else does not act as that person. Protect an owner-created personal key as you would your password. See [Apps and keys](apps-and-keys.md).

## The operator can read the storage

Saved prompts are stored in the gateway's database. Anyone with direct access to that storage can read them.

Self-hosting does not hide your data from the person who runs the gateway. Use a gateway run by someone you trust.

Prompt settings control what the gateway keeps. They do not control copies kept by an engine, a provider or a backup.
