# Category migration report

Run this before converting a legacy access plan. The tool reads a private
`admin.db` snapshot and compares three decisions for each stored profile:

1. The current `gateway.policy.evaluate`, without changes.
2. That evaluator with a completeness gate: a tag-restricted plan denies
   requests whose tags are absent, `None`, or empty.
3. A deterministic conversion evaluated by `gateway.category_policy.evaluate`.

```sh
~/applications/azizam/venv/bin/python scripts/categories-migration-report \
  ~/.tmp/azizam-catmigrate-admin.db --examples 3
```

`--examples 0` prints counts and the static path inventory. The database is
opened with a `file:` URI and `mode=ro`. The reader selects only profile IDs,
policy JSON and the service registry setting. It does not initialize the
database or select people, apps, credentials or profile names. Examples use
synthetic request labels and reason codes; they omit target names and raw
legacy reason strings.

## Finite conversion

The registry matrix crosses every declared capability with every known
model, model-group alias and workflow, and every declared frontend protocol.
A service without enumerated models contributes a service-only target.
Each workflow contributes one target per stored backend binding. These
targets carry backend and provider restrictions into both legacy decisions
and converted grants. A workflow without bindings stops the report.
The matrix includes configured disabled records because this is an access
comparison, not an availability probe. It does not import filesystem
catalogs, infer missing model metadata or probe running engines.

Each target/protocol/provider context receives a deterministic synthetic
service and root revision ID. These IDs end in `@report`. They satisfy the
kernel's revision syntax but do not attest an actual reviewed revision.

Capability/intent, protocol, model, workflow, backend, provider, fallback and
native permissions become exact grants over this finite matrix. To decide
which grants to create, the conversion runs the legacy evaluator with only
its category selectors removed. Intent family matching therefore expands
into exact capabilities. A `None` list imposes no constraint; an empty list
grants no matching requests. No wildcard grants are created.
For each comparison, the report passes the matching exact grant to the
kernel. Nonmatching grants cannot change that decision. This avoids
revalidating thousands of generated grants for every matrix request.

Tags become the `legacy-tags` set category. Exposure, trust and resource
selectors become three separate set categories. Category values are the
union of plan values, matrix values and fixture values. Each configured
selector becomes a require-in row containing its allowed values, without a
missing-value waiver. All supplied values become accepted assertions with
synthetic owner provenance for this preview. This does not accept evidence
in the stored configuration.

The converted plan supplies both the person and app sides of the kernel's
intersection. Mismatch fixtures remove one side's grants. These fixtures
show the new intersection semantics; they do not infer actual assignments
from people or app records.

## Expected differences

Legacy exact skips a tag selector when tags are `None`. It **denies** an
empty tag set when the plan has a tag selector. It permits a mixed set if
any tag matches. V3 requires a known, nonempty accepted set, and every
value must be allowed. Missing metadata and mixed sets therefore produce
intentional differences. Other configured category selectors also stop
skipping missing values in v3.

The fixtures cover absent, `None` and empty tags; matching, disallowed and
mixed tags; multiple assertions; an unknown accepted category; person/app
mismatch; empty grants; a changed route; and helper, retry, cache, release,
retrieval, download, share and action boundaries. Boundary fixtures add a
disallowed subject to the recorded allowed root bundle. The kernel must
check the whole bundle before the next effect.

The report counts changes in allow/deny decisions, not changes in reasons.
Each plan adds the same fixture types to one matrix request, preferring a
request permitted by its non-category restrictions. A plan with no such
request uses the first matrix request; its earlier restrictions may mask
later fixture differences. Fixtures are synthetic tests, not observed use.

## Static path inventory and limits

The AST scan lists `make_policy_request` and `PolicyRequest` calls in
`proxy.py` and `gateway/*.py`, with enclosing function and route decorators.
Omitted tags, literal `None` and literal empty collections are listed as
paths that would fail the gate under a tag-restricted plan. Variable values
and `**kwargs` are listed separately as unresolved dynamic inputs. Wrapper
and preview calls remain visible; the inventory is not a count of runtime
HTTP clients.

Static source cannot identify which installed clients use these paths or
whether dynamic tags are complete. The report also cannot verify real
person/app assignments, route alternatives, pinned dependency graphs,
current-loaded state, budgets, provider admission, or metadata at actual
output boundaries. Runtime activation and owner acceptance remain separate
release gates. An empty registry or invalid profile stops the report with a
generic error that does not disclose profile contents.
