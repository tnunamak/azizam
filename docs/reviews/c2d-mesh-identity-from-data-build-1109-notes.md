# Mesh identity from data: implementation notes

Base: `f8e56732469d179adeb2840dbd0177e137fa68a7`.
Assignment: `c2d-mesh-identity-from-data-build-1109`.

## Delivery and verification boundary

One patch changes only `gateway/mesh_api.py`, `gateway/mesh_manifest.py`,
`gateway/capability_ids.py`, `registry/capability_words.yml`,
`registry/protocol_frontends.yml`, the new regression test, and this notes file.
No checks were run: no pytest, collection, compilation, lint, or repository scripts.
The brief explicitly prohibits checks. Assertions below are source-based expectations,
not observed test results. Existing-test compatibility is not claimed as verified.

The large root gateway module was not opened. Reads stayed within the allow-list.
The unchanged workflow contract remains authoritative for its identity and I/O:
`manifests/workflows/mesh.generate.trellis-2.yml:4-6,12-20,44-56`.

## Changed readers and data, with final-file evidence

- `gateway/mesh_api.py:101-111` (`mesh_profile_errors`) keeps the existing wire-shape
  validator but validates a nonempty contract intent and a registered capability,
  rather than demanding the legacy intent literal.
- `gateway/mesh_api.py:115-125` (`mesh_workflow`) copies the contract's intent
  into the workflow view. `mesh_workflow_from_record` at line 128 is unchanged
  and still delegates to that reader.
- `gateway/mesh_api.py:155-172` (`_identity_problem`) validates the row capability
  before label indexing, requires row/policy intent equality, checks a policy
  capability when explicitly supplied, and compares both identity fields with
  the supplied workflow snapshot. The shipped policy contains no separate
  capability field (`registry/protocol_frontends.yml:960`).
- `gateway/mesh_api.py:175-209` (`route_row`) finds POST ownership by canonical
  listing path, independently of record ID. Zero owners preserves FRONTEND_ABSENT;
  a unique disabled owner preserves FRONTEND_DISABLED; multiple owners are
  refused even when one is disabled. Duplicate POST rows and mixed methods are
  also refused. Its existing one-argument API remains supported; discovery and
  availability supply the optional workflow snapshot for the cross-check.
- `gateway/mesh_api.py:212-217` (`_modalities`) accepts only nonempty lists
  whose strings belong to the registered modality vocabulary. It does not
  treat arbitrary nonempty strings as valid modality names.
- `gateway/mesh_api.py:220-229` (`_workflow_metadata`) preserves off-state
  discovery when no usable frontend exists, using asset MIME families and
  declared output names from the workflow contract. This is only the missing/
  disabled-owner path, not a fallback for a mismatched or malformed enabled row.
- `gateway/mesh_api.py:241-272` (`discovery_facts`) reads the selected row's
  capability, title lookup, and model metadata, and uses the contract intent
  and media type. Identity mismatches or unknown capabilities raise the bounded
  MeshManifestError, not KeyError and not a default mesh identity. Missing or
  disabled owners retain the previous disabled/experimental/batch tiers.
- `gateway/mesh_api.py:275-299` (`pool_evidence`) supplies the same manifest
  snapshot to route validation and passes the selected row capability into
  the service check at line 288. Rejected identity does not reach that check.
- `gateway/capability_ids.py:19-37` adds the immutable modality vocabulary
  reader and validates its shape, nonblank words and uniqueness.
  `_load_capability_tables` at lines 40-71 accepts and validates the optional
  section without changing any existing tuple-return API; line 213 exposes
  MODALITY_WORDS. Existing capability labels remain the capability vocabulary.
- `registry/capability_words.yml:3-4` declares the distinct modality vocabulary.
  No sculpture or surface-model value is added to shipped data.
- `registry/protocol_frontends.yml:957-959,965-967` adds the existing
  model_metadata shape to both mesh POST rows. Other rows already use that
  shape at base `registry/protocol_frontends.yml:42-45,82-85`.

## Tests first, fixtures, and base outcomes

The new test file was applied before any production edit. The malformed-vocabulary
cases were also added before their implementation. Fixtures read and deep-copy
the shipped frontend and contract, then mutate only in memory
(`tests/test_mesh_identity_from_data.py:47-70`). The vocabulary extension is
an isolated monkeypatch, not a shipped-file edit (lines 159-161 and 222-224).
The vocabulary-loader cases mock file reads rather than writing files (lines 266-273).

Expected failures on the unmodified base, established from source:

- Shipped metadata assertion at test lines 91-94: base route row has no
  model_metadata (`registry/protocol_frontends.yml:953-957`).
- Renamed-owner assertion at lines 97-110: base `mesh_api.py:152` selects
  record ID `mesh`, so `atelier-3d` is treated as absent.
- Duplicate-owner cases at lines 125-131: base line 152 takes the first
  named record rather than detecting two owners.
- GET-only ownership and GET-record shadowing at lines 134-143:
  base lines 152-166 use the ID and reject methods only afterward.
- Custom sculpture/sculpture.create case at lines 158-179: base
  `mesh_api.py:103-104` refuses the contract intent before the assertions;
  lines 118,193,196-197 and 224 additionally pin the reported intent,
  capability/title, and service capability.
- Six mismatch cases at lines 182-208 and four unknown-capability cases
  at lines 211-218: base discovery ignores route_row's problem and returns
  fixed identity instead of raising (`mesh_api.py:190-199`).
- Custom surface-model case at lines 221-229: the new MODALITY_WORDS API
  is absent on the base, and base lines 198-199 hard-code both modalities.
- Five invalid-modality cases at lines 232-242 and missing-metadata case
  at lines 245-248: base lines 198-199 ignore metadata and do not raise.
- Six malformed vocabulary cases at lines 265-273: the new
  load_modality_words API does not exist on the base.

Expected base-passing compatibility cases: the two setup-state cases
(lines 113-122), duplicate rows within one record (146-149), mixed methods
(152-155), and the golden case (251-263).

## Golden interpretation

The entire Capability is captured at `tests/test_mesh_identity_from_data.py:37-44`
from base `gateway/mesh_api.py:186-207` and the Capability defaults at
`gateway/capabilities.py:107-153`; line 256 compares dataclass equality.

The literal base route is captured at test lines 22-34 from base
`registry/protocol_frontends.yml:953-957` and base
`gateway/mesh_api.py:150-175`. Lines 258-263 feed that unchanged base row
to route_row and compare its exact returned tuple. Line 255 compares every
pre-existing field of the newly shipped row, and lines 91-94 independently
assert the requested metadata addition.

A raw shipped route mapping cannot simultaneously acquire model_metadata
and remain byte-for-byte identical to the old mapping. The patch therefore
preserves all prior route fields and the complete Capability, with exactly
the explicitly requested new metadata. This interpretation and the new
modality vocabulary were sent to the submitting thread as a QUESTION;
no clarification had arrived at the final owner-note check.

## Compatibility aliases and visible importers

- FRONTEND_ID and CAPABILITY remain legacy import aliases at
  `gateway/mesh_api.py:35-37`; none of the changed readers consults them.
- MESH_INTENT remains a legacy alias at `gateway/mesh_manifest.py:12-13`.
- The only directly observed importer of MESH_INTENT in the allowed files is
  `gateway/mesh_api.py:22`, which now explicitly re-exports it. The same line
  preserves MESH_MEDIA_TYPE for external imports, although discovery now
  reads the workflow media type.
- No direct importer of FRONTEND_ID or CAPABILITY was seen in the allowed
  files. `tests/test_mesh_discovery.py:17` imports the mesh_api module and
  uses its public functions. An exhaustive repository importer search was
  not permitted by the read allow-list; aliases remain to avoid breaking
  possible callers outside it.
- The existing external wire-profile implementation and test harness were
  not opened because they are outside scope. The lead's checks must exercise
  their integration, especially custom identity and the new registry metadata.

## Case count and exact lead-run files

Case-count delta: **+35 cases** across **16 test functions**, counted from
the source parametrizations, not from collection. No existing tests are
changed. The test budget file is untouched.

Lead should run these exact files together:

`python -m pytest tests/test_mesh_identity_from_data.py tests/test_mesh_discovery.py`

The lead should then run the repository's normal full gate to establish that
every existing test remains green, including shared registry/vocabulary
consumers outside this assignment's read scope. No test, lint, compilation,
or full-suite success is asserted by this report.
