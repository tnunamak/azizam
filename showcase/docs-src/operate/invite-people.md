---
title: Invite people
summary: Create an access plan, send an invitation link, and give each person their own key.
---

# Invite people

Each person you invite gets their own account, their own app and their own key. Their access plan sets what they can use.

## Before you invite anyone

- **Choose an access plan.** The plan decides which models and workflows a person can use. Read [Access plans](../concepts/access-plans.md).
- **Make the gateway reachable.** A person on another device cannot use `127.0.0.1`: that address points to their own device. Set up an HTTPS address and secure cookies first. See [Open the gateway to other devices](security.md#open-the-gateway-to-other-devices).

## Send an invitation

1. Open **People**, in the **Gateway** group of the menu.
2. Create an invite.
3. Choose the access plan and when the invite expires. The default is 7 days; the range is one minute to 30 days.
4. Copy the invitation link, and send it to the person yourself.

The gateway does not send email.

## What the person does

When the person accepts the invitation, the gateway creates an account and a personal app called **My first app** for them, and shows the app's key once. They then follow [Use a gateway](../use/index.md) and connect their apps with [Apps & keys](../use/apps-and-keys.md).
