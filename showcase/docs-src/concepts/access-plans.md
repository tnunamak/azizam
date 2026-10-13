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

You can change an app's default plan without issuing new keys. A key with its own plan keeps using that plan instead.

## How a request is checked

The gateway checks the rules in order. The first refusal wins.

- For protocol, workflow, model and engine allow-lists, an unset list adds no restriction. An empty list refuses requests that supply that value.
- Capability selectors apply only when the request supplies the corresponding metadata.
- Remote providers must be explicitly allowed.
- Native passthrough must be enabled separately. Its empty engine and operation lists add no restriction.

Capability rules use the labels that describe each model. A label is not proof of what a model or its output contains.

Cloud fallback and paid fallback are separate permissions. Allowing a provider does not, by itself, allow paid fallback.

## Seeing a model is not the same as running it

A plan can show every configured model, or only the models available now. It can also allow or refuse starting a model that is not loaded. Seeing a model in the list does not mean that it is loaded.

[Categories](categories.md) can add more restrictions to a request that a plan allows. They cannot allow a request that the plan refuses.

## Trusting an address instead of a key

By default a request with no key is refused. An owner can change that for one device, or one network, without giving up the access plan.

Open Apps & keys, choose the app, and add a trusted address. Enter one IP address or a range such as `192.168.1.0/24`. A request with no key that comes straight from that address runs as that app. The app's access plan limits it, and each request shows the app and the address.

- No address is trusted until an owner adds one.
- A key that is sent always decides the request, even from a trusted address.
- Only the address of the direct connection counts. The gateway reads `X-Forwarded-For` only when the connection comes from a reverse proxy listed in `auth.trusted_proxies`. A request that shows signs of an unlisted proxy never matches.
- Turn the app off, or remove the address, and the address stops working at once.
