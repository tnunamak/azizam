# Operation activation

Operation activation stages a service-local alias overlay outside the active
registry, validates a durable derived artifact, and only then makes one scoped
registry update. A staged candidate is never visible to discovery, execution,
fresh boot, or an ordinary registry read.

The supported API is under `/admin/api/operator/operation-targets/{service_id}/{alias_id}`.
Read the target, retain its composite ETag (`"ot-N-os-M"`), stage with `PUT`
and `If-None-Match: *`, dry-run with `POST /validation`, then activate with the
composite `If-Match` and a visible-ASCII `Idempotency-Key`. Results are
non-cacheable. Evidence is an authenticated operator assertion with a digest
and expiry; it is not runtime attestation.

For offline evidence canonicalization use:

```sh
./venv/bin/python scripts/operation_evidence.py receipt.json
```

The CLI reads only the explicit local path supplied by its user. The HTTP API
does not accept paths, URLs, commands, or evidence bodies in status responses.
