---
title: Glossary
summary: One name for each thing in Azizam, and what it means.
---

# Glossary

These docs use one name for each thing. Where the API uses another name, the entry says so.

## Access plan

In the API: profile. A named set of rules that decides which requests an app may make. See [Access plans](../concepts/access-plans.md).

## Admin key

A key for changing the gateway's configuration. It cannot run model requests. Keep it out of apps.

## App

A client that uses the gateway, such as a chat app, a script or an agent. A personal app belongs to a person. A service app belongs to a service account. See [Apps and keys](../concepts/apps-and-keys.md).

## App key

A key that lets an app send requests. It is shown once, when it is created.

## Category

An owner-defined label in a policy that can add restrictions to access plans. See [Categories](../concepts/categories.md).

## Creation

A picture, video or audio file made through the gateway. Creations are listed in Library.

## Engine

A server that does model work, such as chat, pictures or speech. You run it and connect it to the gateway. See [Engines and models](../concepts/engines-and-models.md).

## Gateway

One running installation of Azizam.

## Job

Work that can take longer than one request, such as making a picture or a video. You can check a job and read its result later. See [Media jobs](../api/media-jobs.md).

## Member

A person invited to use a gateway.

## Model name

The name that apps use to choose a model. The owner maps it to an engine's own model ID.

## Owner

The person who runs the gateway. The owner creates the first account, connects engines and invites members.

## Provider

A remote service, usually paid, that the gateway can send requests to. See [Providers and spend](../concepts/providers-and-spend.md).

## Receipt

A record of what happened to one request. See [Receipts and usage](../concepts/receipts-and-usage.md).

## Workflow

A named task that the owner sets up, such as making a video from a prompt. A workflow lists the inputs it needs.
