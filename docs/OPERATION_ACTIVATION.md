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

`evidence_registry_revision` is an operator-declared string inside service
evidence that binds the evidence to a registry revision. It is unrelated to
`operator_settings_generation`, the server-owned integer and sole CAS
authority for the service registry.

For offline evidence canonicalization use:

```sh
./venv/bin/python scripts/operation_evidence.py receipt.json
```

The CLI reads only the explicit local path supplied by its user. The HTTP API
does not accept paths, URLs, commands, or evidence bodies in status responses.

## Offline compaction enrollment

Before a context-compaction overlay can be staged, build its operator evidence
with the offline-only enrollment boundary. It takes an explicit non-secret JSON
manifest and output directory; it does not read the registry, authenticate,
send HTTP, start a server, or perform inference.

```sh
./venv/bin/python scripts/compaction_enrollment.py collect \
  --manifest enrollment-manifest.json --output enrollment-proof
./venv/bin/python scripts/compaction_enrollment.py verify \
  --manifest enrollment-manifest.json --output enrollment-proof
./venv/bin/python scripts/compaction_enrollment.py package \
  --manifest enrollment-manifest.json --output enrollment-proof
./venv/bin/python scripts/operation_evidence.py enrollment-proof/evidence.json \
  --overlay enrollment-proof/overlay.json --base-model-id '<base-model-id>'
```

`collect` produces deterministic, redacted `evidence.json`, `overlay.json`, and
`proofs.json`. `verify` recomputes every binding and rejects tampering, expired
evidence, artifact/runtime drift, non-parity, floats, credentials, and prompt
persistence. `package` runs verification first and writes an activation-package
copy for review; the separate `evidence.json` and `overlay.json` are directly
consumable by `operation_evidence.py`.

The evidence is a bounded deployment observation, not lasting runtime
attestation. Renewal is a new evidence revision: re-collect all declared local
artifacts and transcripts, verify, package, replace the normal service evidence
through the supported registry writer, then stage and activate the matching
overlay. Never edit an expiry timestamp, use direct SQL, or reuse evidence after
the declared runtime/template/tokenizer binding has changed.
