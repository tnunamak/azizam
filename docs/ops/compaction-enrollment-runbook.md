# Context-compaction enrollment runbook

Status: placeholder only. This file intentionally contains no live paths,
credentials, prompts, service identifiers, or activation authorization.

1. Obtain an approved, non-secret enrollment manifest from the normal operator
   process.
2. Run `collect`, `verify`, and `package` with explicit local paths.
3. Review only the redacted digests and failed predicates.
4. Use the existing operation-activation lifecycle separately, with fresh
   authorization and its own documented CAS/ETag procedure.

Do not run inference, mutate a database, edit a service, or copy prompts into
the proof package as part of enrollment.
