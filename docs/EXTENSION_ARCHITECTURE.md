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

Registry records carry an ownership mode:

- `ui`: created and edited in `/admin`; DB is authoritative.
- `bundled`: shipped with the gateway; immutable until copied.
- `provisioned`: supplied by deployment/config; read-only until detached or
  copied.
- `imported`: installed extension package object; immutable until upgraded.
- `copied`: operator-owned fork of bundled/imported/provisioned data.
- `discovered`: found by inventory; read-only until adopted.

Enablement never mutates bundled, imported, or provisioned files. It creates DB
records referencing immutable package content hashes, or copies records into
`ui`/`copied` ownership.

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
ownership:
  mode: imported
  editable: false
objects:
  protocol_frontends:
    - protocol_frontends/example.yml
  services:
    - services/example.yml
  targets:
    - targets/example-model.yml
  workflows:
    - workflows/example-image.yml
  credentials:
    - credentials/example-api-key.yml
  tests:
    - tests/health.yml
adapters:
  - id: generic.http-json
    module: gateway.adapters.http_json:HttpJsonAdapter
    interfaces: [health, invoke, inventory]
permissions:
  network:
    outbound:
      - ref: service.base_url
  filesystem:
    read:
      - package://workflows/**
    write: []
```

## UI Flow

The admin UI is the normal extension surface:

1. **Import** package, directory, URL, or bundled extension.
2. **Review** publisher, version, signature/trust, object counts, permissions,
   adapters/translators, required secrets, and affected Profiles.
3. **Validate** schemas, unique IDs, adapter presence, route collisions, binding
   targets, dependency declarations, and policy impact.
4. **Configure** service URLs, secret refs, ownership mode, and which records to
   enable or copy.
5. **Test** health checks, inventory, dry-run binding, and optional smoke jobs.
6. **Enable** DB registry records. New capabilities do not become available to
   Apps until Profiles allow them.
7. **Operate** disable, update, rollback, repair dependencies, copy as editable,
   or uninstall when no active references remain.

The UI should render forms from schemas. It should not contain
provider-specific wizards in core code.

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

Git URLs are convenient input, not authority. Enabled DB records store source
URL, resolved ref, package hash, enabled objects, ownership mode, config
overrides, and audit history. A Git-backed update is a new staged candidate
that must pass validation before replacing the enabled package.

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
- Code plugins must declare permissions and trust tier.
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
- data-only packages only,
- `services`, `protocol_frontends`, `workflows`, and `capabilities`,
- validate/review before enable,
- enable by merging normalized records into the DB service registry,
- store installed package metadata in `admin_store.operator_settings`,
- keep imported records immutable and provenance-stamped by package id, version,
  content hash, and resolved source commit when installed from Git.

This gives operators the Home Assistant / ComfyUI Manager style flow without
making core code provider-specific. Code plugins, archive uploads, signed
catalog indexes, dependency repair, uninstall, and rollback are still separate
lifecycle features, not prerequisites for data-only packages.

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
