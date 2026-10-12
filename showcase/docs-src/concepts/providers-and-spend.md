---
title: Providers and spend
summary: How permission to use a paid provider works, and why recorded spend is not a bill.
---

# Providers and spend

A provider is a remote service that the gateway can send requests to. Most providers charge for use.

## Permission to use a provider

Each access plan has a list of allowed providers. A request for a provider that is not on the list is refused.

Paid fallback is a separate permission. Allowing a provider does not allow the gateway to fall back to it when a local engine is unavailable. A local model that is down does not become a paid request unless you set that up.

## Spend settings

An access plan can hold a per-request limit, a daily budget and a monthly budget, in US dollars. All apps on the plan share its budgets. The daily budget covers the last 24 hours. The monthly budget covers the calendar month.

Before a paid request, the gateway estimates its cost and reserves it against the budgets. It refuses the request if the reservation would go over a limit. After the request, it records the actual cost. Requests that are already running can take spend past a budget.

When a budget is set, the gateway refuses paid requests whose cost it cannot estimate. A charge that is not yet reconciled blocks further paid requests.

Set a spending limit with the provider too.

## Recorded spend is not a bill

Usage adds up the costs recorded in receipts. Some requests have no recorded cost. Usage shows how many requests have a measured cost, and how many paid requests have an unknown cost.

A total of zero with no measured requests does not prove that nothing was spent. Use the provider's billing records to check what it actually charged.

See [Receipts and usage](receipts-and-usage.md).
