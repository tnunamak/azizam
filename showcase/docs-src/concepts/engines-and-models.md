---
title: Engines and models
summary: Why the engines and models you can use depend on each gateway's settings.
---

# Engines and models

Every gateway has its own engines, so there is no fixed list of models for Azizam. Each gateway's catalog shows what that gateway offers.

## Engine, model and workflow

- An **engine** record describes a server that does model work: its address, health probe, priority and capabilities.
- A **model name** is the name apps use. The owner maps it to the engine's own model ID.
- A **workflow** is a named task, with the inputs it needs. A workflow runs on an engine.

For example, an owner could connect one engine for chat and another for pictures. That is an example of settings, not a promise that either engine exists on your gateway.

## Settings, not code

The owner can add, change, turn off and remove engine records. The gateway records each change in its activity history. Because engines are settings, you can replace an engine when a better one comes out, without changing the gateway.

## Configured is not the same as running

A model in the catalog is not always loaded. An [access plan](access-plans.md) decides whether an app sees all configured models or only the ones available now, and whether a request may start a model that is not loaded.

To add or change an engine, see [Connect engines](../operate/engines.md).
