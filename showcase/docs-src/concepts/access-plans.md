---
title: Access plans
summary: How an app's access plan allows or refuses each request.
---

# Access plans

An access plan is a named set of rules that decides which requests an app may make. The API calls it a profile.

A plan can limit protocols, workflows, models, engines and capabilities. It also sets rules for remote providers, fallback, status visibility and whether a request may start a model that is not loaded.

## Which plan applies

For a request with an app key, the gateway uses the first of these that is set:

1. The plan set on the key.
2. The app's default plan.
3. For an app that belongs to a person, that person's plan. If the person has no plan, the default plan for their role or sign-in group.
4. The built-in default plan.

If the selected plan is missing or invalid, the request is refused. A turned-off app cannot run requests.

You can change an app's plan at any time. The app does not need a new key.

## How a request is checked

The gateway checks the rules in order. The first refusal wins.

- A list that is not set does not restrict anything.
- An empty list refuses everything.
- Protocol, workflow, model and engine lists apply when the request names that value.

Capability rules use the labels that describe each model. A label is not proof of what a model or its output contains.

Cloud fallback and paid fallback are separate permissions. Allowing a provider does not, by itself, allow paid fallback.

## Seeing a model is not the same as running it

A plan can show every configured model, or only the models available now. It can also allow or refuse starting a model that is not loaded. Seeing a model in the list does not mean that it is loaded.

[Categories](categories.md) can add more restrictions to a request that a plan allows. They cannot allow a request that the plan refuses.
