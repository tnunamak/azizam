---
title: Apps & keys
summary: Connect a chat app or agent, and create, rotate or revoke its keys.
---

# Apps & keys

Give each app its own key. Then you can stop one app without breaking the others.

## Connect a chat app or agent

1. Open **Apps & keys** and select **New app**.
2. Enter an **App name** and choose an **App recipe**. Choose **Other app** if yours is not listed.
3. Select **Create app and key**.
4. Save the key while it is shown. You cannot see it again.
5. In your app, choose its OpenAI-compatible provider setting. Paste the gateway address that Azizam shows, and enter the key.

Use **Connection examples** if you need a setup example. If a prompt-saving note is shown with the key, read it. See [Client setup examples](../api/clients.md) for common apps.

## Create a key for an existing app

Find the app in the list. Select **Create key**, or open the app's actions menu and select **Create key** or **Create another key**. Save the key before you leave the screen.

If the app was created but its key was not, select **Retry key creation**.

## Rotate keys

Rotate keys when a key may have leaked, or to replace old keys on a schedule.

1. Open the app's actions menu and select **Rotate keys**.
2. Select **Create replacement key**, and save the new key.
3. Update your app to use the new key.
4. Confirm that you saved the key. This starts to revoke the old keys.

If some old keys still work, select **Retry revoking old keys**. You can also select **Finish later**. The app then shows **Revoke old keys** while the old keys are active. If the browser says it could not save that reminder, revoke the old keys before you leave the page.

## Stop an app from connecting

Open the app's actions menu.

- **Revoke keys** revokes the app's keys. The app stops connecting.
- **Remove app** removes the app and its keys.

Each action asks you to confirm.

## When you cannot create a key

Read the message shown for the app. The app may be turned off, or its access plan may have been removed. Ask the owner to check the app's access.

Read [Apps and keys](../concepts/apps-and-keys.md) to learn what a key can do, including access to your saved prompts.
