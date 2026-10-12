---
title: Azizam documentation
summary: Install a gateway, connect your apps, and give each person their own access.
---

# Azizam documentation

Azizam is an AI gateway that you run on a computer you control. It puts the engines you run, and any paid providers you choose, behind one OpenAI-compatible address. Each person and each app gets its own key and access plan.

## Choose where to start

<ul class="start-cards">
<li><a href="start/install.md"><strong>Run a gateway</strong><span>Install Azizam, connect an engine and get a first reply.</span></a></li>
<li><a href="use/index.md"><strong>Use a gateway</strong><span>Someone invited you. Use Try, Library, Apps &amp; keys and Usage.</span></a></li>
<li><a href="api/index.md"><strong>Build with the API</strong><span>Connect an app, an SDK or an agent with your app key.</span></a></li>
</ul>

## What Azizam does

The gateway sits between your apps and the engines that do the model work.

1. An app sends a request with its app key.
2. The gateway checks the key and the app's access plan.
3. The gateway sends the request to the engine or provider that serves the chosen model.
4. The gateway records a receipt for the request, on routes that record receipts.

You choose the engines. Engine addresses and model names are settings, not code. You can replace an engine without changing the gateway.

## What you need to bring

- A computer to run the gateway, with Docker and Docker Compose.
- At least one engine that serves an OpenAI-compatible chat API, with a model already loaded.
- Model files for any image, video or audio work you want to do.

Azizam does not download or install models. What you can do depends on the engines and model files you connect.

## Before you rely on it

- A route that accepts a vendor's API format does not mean that every feature of that API works. Each page under [Build with the API](api/index.md) lists its limits.
- A local model that is down does not silently become a paid request. Paid providers need explicit setup and permission. See [Providers and spend](concepts/providers-and-spend.md).
- You run the computer. Updates, backups and HTTPS for other devices are your responsibility. See [Upgrade and back up](operate/upgrade-and-backup.md) and [Security](operate/security.md).

## Find your way around

| Section | Use it to |
| --- | --- |
| Start here | Install a gateway and get a first reply. |
| Use a gateway | Do everyday tasks as a member. |
| Run a gateway | Connect engines, invite people, keep the gateway safe and up to date. |
| Build with the API | Connect apps, SDKs and agents. |
| Concepts | Understand how access, keys, receipts and privacy work. |
| Reference | Look up settings, error codes and terms. |

Use **Search** at the top of any page, or press <kbd>/</kbd>.
