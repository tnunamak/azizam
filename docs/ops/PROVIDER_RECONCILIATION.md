# Unknown provider charges

A completed or interrupted paid request whose bill is unknown moves its spend
ledger row to `needs_reconciliation`. Admissions against an affected provider,
access-plan, or global daily/monthly cap return HTTP 402 with code
`provider_needs_reconciliation`. The barrier remains across restarts and budget
window changes. Unrelated caps remain usable. Uncapped calls retain the unknown
charge record but do not acquire a budget barrier.

Receipts include `cost.reservation_id` and `cost.reconciliation_status` for paid
requests. The ledger's request ID links owner reconciliation to the original
receipt. Receipts describe the result at completion; the durable ledger and
owner audit describe later corrections.

## Crash and timeout recovery

Each reservation records the process generation that owns it and its expiry.
Startup recovers reservations from a previous process generation. Admission also
recovers expired reservations and reservations without a current process owner.
Recovered charges enter `needs_reconciliation`; their estimates are never treated
as actual bills. Recovery commits before a denied admission returns, so the owner
can list and reconcile the charge.

Current-process reservations before their expiry remain in flight. Chat uses its
120-second transport timeout; paid Decisions use their 15-second request timeout.
A request that later supplies an actual bill can still settle an expired charge.
The budget barrier is conservative for requests that outlive the recorded timeout.
This ownership policy follows the gateway's single-process deployment; separate
gateway processes sharing a ledger cannot treat one another's reservations as live.

## Automatic reconciliation

When a provider record declares `config.generation_stats`, the gateway queries
that provider's generation metadata on an unknown response with a generation ID.
A later paid admission retries up to two pending generation IDs for that provider.
Missing IDs, unavailable metadata, malformed costs, and redirects leave the
charge unresolved. No stats response is substituted with the request estimate.

The OpenRouter seed declares this shape as data:

```yaml
generation_stats:
  path: /generation
  id_param: id
  cost_path: [data, total_cost]
```

Existing provider records are not overwritten by a seed. Add the same field to
an existing provider through the service registry API if you want automatic
lookup. It uses the provider's base URL and existing credential reference.

## Owner reconciliation

Use the existing admin authentication (and CSRF header for session writes).

- `GET /admin/api/providers/{provider_id}/reconciliation` lists pending charges,
  including reservation ID, access plan, request ID, and generation ID.
- `POST /admin/api/providers/{provider_id}/reconciliation/{reservation_id}` with
  JSON `{"actual_usd":"0.030000000"}` records the verified actual bill. A verified
  zero is allowed. Negative, missing, nonfinite, or unrepresentable costs fail.
- An already settled charge or a charge belonging to another provider cannot be
  reconciled by this endpoint. Normal budget checks resume after settlement; a
  charge that actually exceeds the cap continues to deny further admissions.

Provider and global spend projections include `pending_reconciliation_count`
and use status `needs_reconciliation` while unknown bills exist. The Providers
UI can use this status and the endpoints above.

## Deployment

No new environment variables or separate migration command are required.
The ledger schema adds `request_id`, `generation_id`, `process_generation`, and
`expires_at` columns lazily. On upgrade from a ledger lacking ownership metadata,
existing `reserved` rows become
`needs_reconciliation`: the old schema cannot prove whether those charges were
completed or still in flight. Reconcile those rows against provider records.
Do not use the estimate as an actual bill. This lane does not restart services.
