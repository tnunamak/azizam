---
title: Receipts and usage
summary: What a receipt records, and how Usage counts requests and spend.
---

# Receipts and usage

A receipt records what happened to one request. When an access plan decides a request, the receipt can also hold the decision and its reasons. Usage builds its summary from receipts and from the records of creations.

Receipts do not hold prompts or workflow graphs. Those are kept separately; see [Prompt saving](prompt-saving.md).

## The current month

Usage covers the current calendar month. The month starts at midnight UTC on the first day.

Totals count every kept top-level receipt in the month. The list of recent requests shows up to 50. That limit does not change the totals.

Usage shows requests, failures, creations, activity by day and activity by model.

## Whose activity you see

You see your own activity by default. An owner or a superadmin can choose another person or everyone. The gateway records each time someone views everyone's activity.

Usage says whether the month is complete. If earlier history is not available, the month is marked partial.

## Costs

Spend counts only the costs recorded in receipts. Usage shows how many requests have a measured cost and how many paid requests have an unknown cost. A missing cost does not mean free.

The provider's bill can include charges that are not shown here. See [Providers and spend](providers-and-spend.md).
