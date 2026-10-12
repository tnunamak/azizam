---
title: Prompt saving
summary: How long prompts are kept, and what a one-request "don't keep" choice does.
---

# Prompt saving

The gateway can keep the prompt for a picture, speech or workflow request, separate from the creation. It can also keep the workflow details, for a full download that only the maker can get.

By default, prompts are kept with no time limit. Each person can turn keeping off. The owner can set the default, set a shorter limit, or turn keeping off for everyone.

## Don't keep one prompt

In Try, turn on **Don’t keep this prompt**. See [Try](../use/try.md#dont-keep-a-prompt).

From the API, add this object to the request body:

```json
{
  "extensions": {
    "com.azizam.prompts": {
      "mode": "forget"
    }
  }
}
```

Keep the other fields the endpoint needs. The only accepted value is `"mode": "forget"`. A request cannot force a prompt to be kept when the person or the owner has turned keeping off.

A Responses API request with `store: false` is also treated as "don't keep".

## How long prompts are kept

| Setting | Stored value |
| --- | --- |
| No time limit | `never` (never expires) |
| 90 days | `3_months` |
| 365 days | `1_year` |

When a person's choice and the owner's limit are both set, the shorter one wins. Expired prompts are not shown.

## Limits

- Not keeping a prompt does not delete the creation, and does not hide the request from Usage.
- A normal job keeps its input privately until it finishes, so it can resume after a restart. A job whose prompt is not kept cannot resume.
- The choice does not control what an engine or a provider keeps.
- Service keys never save prompts for a person. See [Apps and keys](apps-and-keys.md).
