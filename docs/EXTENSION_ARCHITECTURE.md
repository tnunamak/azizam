# Extension Architecture

Date: 2026-05-22

The gateway must be adaptable without core code learning every provider,
workflow, node, model, or client API. The hard rule:

> Core code owns schemas, validators, loaders, adapter interfaces, translators,
> execution primitives, policy evaluation, and receipts. Data owns services,
> providers, targets, workflows, capabilities, protocol frontends, routes,
> aliases, resource estimates, and policy defaults.

First-party support follows the same rule as user extensions: bundled data plus
adapter/translator modules. A new provider, workflow, target, or API frontend
should not require editing `proxy.py`.

## Prior Art Shape

The pattern is a blend of:

- Kubernetes CRDs: schema-backed declarative objects extend the control plane
  without changing core API-server code.
- Home Assistant integrations: manifests plus config flows for setup,
  validation, reauth, and migration.
- Node-RED / n8n: package catalogs, credentials separated from node logic,
  install/disable/remove guarded by dependency usage.
- Grafana plugins: explicit plugin metadata, signed/trusted packages, role-gated
  install and admin-managed lifecycle.
- ComfyUI Manager: imported workflows can detect missing custom nodes/models;
  the gateway should copy that dependency-repair affordance without absorbing
  every node into the gateway domain.
- Open WebUI Functions/Pipelines: in-process code is convenient, but isolated
  workers are safer for heavy or community plugins.

## Core Object Kinds

Extension packages can provide these data objects:

- `extension_package`
- `protocol_frontend`
- `provider_adapter`
- `service`
- `target`
- `workflow`
- `capability`
- `credential_definition`
- `profile_template`
- `resource_gate`
- `queue_policy`
- `test_case`

Core code may add object kinds only when the generic model is missing a real
concept. It must not add a new object kind just because a provider has a new
feature.

## Ownership

Package files carry provenance and are immutable artifacts: bundled catalog
files stay in the gateway package, while imported package files stay at their
reviewed content hash until a package update replaces them. This package-file
rule does not restrict a service row's permissions.

For `service` records, `source` is provenance/history, not permission. After
the one-time seed, every non-managed service row is editable,
enablable/disableable, and removable in the DB/UI registry regardless of
whether its source is `ui`, `provisioned`, `builtin`, `discovered`, or package
provenance. The managed LiteLLM substrate is the sole exception: its
gateway-derived endpoint/protocol fields are locked and it cannot be removed;
its name, enabled state, and note remain editable.

Enablement never mutates bundled or imported package files. It writes DB
registry records that reference immutable package content hashes; later edits
to a non-managed service row remain DB/UI operations.

## Package Layout

```text
extensions/
  bundled/
    <package-id>/
      extension.yml
      schemas/
      adapters/
      translators/
      services/
      targets/
      workflows/
      capabilities/
      credentials/
      profiles/
      tests/
      README.md

data/extensions/
  staging/
  installed/
  cache/
```

The repo can ship first-party packages under `extensions/bundled/`. Operator
imports land under `data/extensions/` and are enabled through DB registry rows.

## Manifest

```yaml
schema_version: 1
kind: extension_package
id: com.example.local-ai
name: Example Local AI Pack
version: 0.1.0
publisher:
  name: Example
min_gateway_version: "0.1"
trust:
  tier: local
  signature: ""
objects:
  protocol_frontends:
    - protocol_frontends/example.yml
  services:
    - services/example.yml
  workflows:
    - workflows/example-image.yml
  capabilities:
    - capabilities/example-image.yml
  adapters:
    - adapters/http-json.yml
  translators:
    - translators/example-api.yml
```

The manifest does not define a service-row ownership mode. Review fixes the
package artifacts at their content hash and records source provenance; enabling
an imported service creates a normal DB service row with `source: imported`.
That row follows the normal editability law, not a package-specific permission
mode.

Code objects are referenced exactly like data objects, but their declarations
live in separate files so package validation can hash the reviewed code
artifacts and keep permissions explicit:

```yaml
kind: adapter
id: generic.http-json
mode: sidecar
language: python
interfaces: [health, invoke, inventory]
entrypoint: code.http_json:HttpJsonAdapter
package_path: code
permissions:
  network:
    outbound:
      - ref: service.base_url
  filesystem:
    read:
      - package://workflows/**
    write: []
trust_tier: local
```

## UI Flow

The admin UI is the normal extension surface:

1. **Import** package, directory, URL, or bundled extension.
2. **Review** publisher, version, signature/trust, object counts, permissions,
   adapters/translators, staged code objects, required secrets, and affected
   Profiles.
3. **Validate** schemas, unique IDs, adapter presence, route collisions, binding
   targets, dependency declarations, and policy impact.
4. **Configure** service URLs, secret refs, provenance, and which records to
   enable.
5. **Test** health checks, inventory, dry-run binding, and optional smoke jobs.
6. **Enable** DB registry records. New capabilities do not become available to
   Apps until Profiles allow them.
7. **Operate** disable, update, rollback, repair dependencies, edit
   non-managed service rows, or uninstall when no active references remain.

The UI should render forms from schemas. It should not contain
provider-specific wizards in core code.

Rows for installed or staged packages should distinguish data objects from code
objects. Services, protocol frontends, workflows, and capabilities are registry
data. Adapter or translator modules are code objects: the UI may list them as
staged/reviewed artifacts, but must not imply that a GitHub URL or local
directory is executed just because it was imported or validated.

## Install Sources

The installer accepts several sources, but they all enter the same staged
lifecycle: fetch, stage, validate, review, configure, test, then enable.

- Bundled catalog entries shipped with the gateway.
- Catalog/registry entries published by a trusted index.
- Git or GitHub URLs, pinned by tag, branch plus resolved commit, or explicit
  commit SHA.
- Release archives or local zip/tar uploads.
- Local directories for private operator packages.
- Discovered services or workflows that an operator adopts into a managed
  package record.

Git URLs are convenient input, not authority. The enabled package index stores
the package identity, enabled state, source/resolved ref, reviewed package
hash, object IDs, code-review facts, warnings, and update time. It does not
persist an ownership mode. A Git-backed update is a new staged candidate that
must pass validation before replacing the enabled package.

Local directories follow the same model. The path is an install source for
staging and hashing package content; enablement writes DB registry records
with immutable package provenance, not a live pointer that executes whatever
the directory later contains.

Data-only packages can be live-enabled after validation because their effect is
limited to registry records and policy-gated capabilities. Code-bearing packages
are different: adapter and translator modules are staged as review artifacts
with declared permissions, hashes, trust tier/signature information, and the
records that would reference them. They become runnable only after an explicit
operator trust decision and loader support for the approved code path. Importing
from GitHub is never itself permission to execute arbitrary repository code.

ComfyUI custom-node dependencies follow the same rule. Workflow packages may
declare GitHub URLs or install hints for missing nodes, but the gateway does
not silently mutate ComfyUI. The admin chooses whether to install through
ComfyUI Manager, run a shown command, or mark the dependency manually
satisfied.

## ComfyUI And Node Graphs

ComfyUI graphs remain foreign backend artifacts. The gateway does not model
every node.

The ComfyUI adapter knows mechanics:

- upload assets,
- submit a prompt graph,
- poll queue/history,
- fetch outputs,
- inspect available node classes when possible,
- report missing custom nodes/models.

Workflow data knows semantics:

```yaml
schema_version: 1
kind: workflow
id: example-inpaint
intent: image.edit
inputs:
  prompt:
    type: string
    required: true
  mask:
    type: asset
  denoise:
    type: number
bindings:
  comfyui:
    service_selector:
      adapter: generic.comfyui.http
    workflow:
      api_graph: package://workflows/example.workflow.json
      ui_graph: package://workflows/example.ui.workflow.json
    requirements:
      custom_nodes:
        - id: example/custom-node-pack
          version: ">=1.2.0"
          required: true
      models:
        - id: example-model
          required: true
    map:
      prompt:
        - node: "6"
          input: text
      mask:
        - node: "12"
          input: mask
      denoise:
        - node: "18"
          input: denoise
```

Core validation checks only the stable boundary: graph files exist, node/input
bindings point at existing graph entries, declared dependencies are present or
reported missing, and outputs match the workflow contract.

## Protocol Frontends

Protocol/API frontends are data too. A frontend record declares routes,
canonical intents, translators, error shape, streaming shape, SDK base URL
recipes, and smoke tests. Core code supplies translator interfaces and mounting
machinery; the catalog decides which APIs are mounted.

Adding a Gemini-compatible, ElevenLabs-compatible, A1111-compatible, or custom
studio API should be an extension import plus installed translator, not a
`proxy.py` branch.

## Security

- Installing extensions is admin-only.
- Data-only packages are preferred.
- Code plugins must declare permissions, trust tier, and reviewable code
  objects. Import and validation stage those objects; execution requires an
  explicit trust decision and a loader path that supports the approved code.
- Community/heavy code should run as isolated sidecar workers where practical.
- Packages never contain secret values; they reference secrets by `secret://`,
  `env://`, `docker-secret://`, or external secret-store refs.
- Receipts record which secret ref was used, never the secret value.

## Implementation Test

A change violates this architecture if it adds a specific provider, service,
workflow, model, or protocol route as a hardcoded product fact in core code.
The acceptable alternative is bundled data plus a generic loader/validator and,
only when necessary, a named adapter/translator implementation.

## Implemented Installer Shape

The implemented installer covers the essential data-owned path:

- local directory and Git/GitHub sources,
- data-only packages can be enabled live,
- `services`, `protocol_frontends`, `workflows`, and `capabilities`,
- validate/review before enable,
- enable by merging normalized records into the DB service registry,
- store installed package metadata in `admin_store.operator_settings`,
- keep imported package files immutable, and stamp DB records with package id,
  version, content hash, and resolved source commit when installed from Git.

This gives operators the Home Assistant / ComfyUI Manager style flow without
making core code provider-specific. Code-bearing packages extend the same
staging/review model by showing adapter or translator modules as staged code
objects with trust warnings. They are not treated as arbitrary GitHub code to
execute. Archive uploads, signed catalog indexes, dependency repair, uninstall,
and rollback are still separate lifecycle features, not prerequisites for
data-only packages.

The API shape should be:

```json
{"source": {"type": "local_directory", "path": "/path/to/package"}}
```

```json
{"source": {"type": "git", "url": "https://github.com/example/gateway-pack.git", "ref": "v0.1.0", "subdir": "package"}}
```

Current admin API:

- `GET /admin/api/extensions`
- `POST /admin/api/extensions/validate`
- `POST /admin/api/extensions/enable`

Validation returns package metadata, object counts, normalized candidate
records, warnings, and errors without mutating state. Enable writes the service
registry and package index, records audit events, and persists Git-backed
packages under `data/extensions/installed`. Imported records carry package
provenance: package id, version, hash, source summary, and resolved commit when
available.
