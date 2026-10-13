---
title: Categories
summary: How category rules add restrictions on top of access plans.
---

# Categories

Category enforcement has not been verified for this release. Do not rely on the coverage, revision-matching or failure behavior described below as a security guarantee.

Categories let the owner describe policy in their own words, as data. For example, an owner could create a category for a group of people and say which models it may use. That is an example, not a built-in rule.

The category check runs after the access plan allows a request. It can only make the decision stricter. It never allows a request that the plan refused.

## When category rules apply

- With no active category policy, categories add no restrictions.
- A request is governed when its person or its app is listed in the active policy. People and apps that are not listed keep the access plan's decision.
- A governed request needs a category plan that covers both its person and its app, and an entry for the requested workflow or model. Missing coverage refuses the request.

## How a model is matched

A policy entry names a model or workflow with a revision, for example `example-model@review-1`. It matches requests for `example-model`. That name is an example, not an installed model.

## If the rules cannot be read

If the active policy cannot be read or is invalid, the gateway refuses every request that the access plan allowed, with `POLICY_UNAVAILABLE`. It never ignores the policy.

Category labels are policy that the owner writes. They do not inspect or guarantee what a model produces.
