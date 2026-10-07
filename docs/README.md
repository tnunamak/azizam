# Documentation

Choose the guide for the task you want to complete. For the product overview, start with [the main README](../README.md).

## Start here

Follow this ordering through the existing guides. A future trimmed structure
would group these as start, operator, clients, reference, contributor, and archive:

1. [Product and first chat](../README.md), then [first-run setup](FIRST_RUN_SETUP.md).
2. [Publish safely](ops/PUBLIC_EXPOSURE.md) and [TLS/WebSocket proxying](REVERSE_PROXY.md).
3. [Operator tasks, backup, and troubleshooting](guides/OPERATOR.md), then [providers](REMOTE_PROVIDERS.md) and [people](PEOPLE_AND_ACCOUNTS.md).
4. [Connect clients](LOCAL_CLIENT_RECIPES.md), then [route limits](SUPPORT_MATRIX.md), [configuration](CONFIG_SOURCE_OF_TRUTH.md), and [media formats](../MODEL_COMPATIBILITY.md#speech-formats).
5. [Contributor setup and checks](../CONTRIBUTING.md), then design references. Historical research and example deployments belong after operational guides.

## Operator

Run a gateway, connect engines and providers, and give people access.

| Task | Guide |
| --- | --- |
| Start and claim a new instance | [First-run setup](FIRST_RUN_SETUP.md) |
| Operate, back up, upgrade or troubleshoot | [Operator guide](guides/OPERATOR.md) |
| Understand which settings apply live | [Configuration ownership](CONFIG_SOURCE_OF_TRUTH.md), [bootstrap configuration](../CONFIG.md) |
| Configure paid models and resolve charges | [Remote providers](REMOTE_PROVIDERS.md), [reconciliation](ops/PROVIDER_RECONCILIATION.md) |
| Share access and publish safely | [Accounts and invites](PEOPLE_AND_ACCOUNTS.md), [reverse proxy](REVERSE_PROXY.md), [security policy](../SECURITY.md) |

## Member

You have an invite or an account on someone else's gateway. Use the [member guide](guides/FOR_MEMBERS.md) to sign in, save your key and connect an app.

## App or agent integrator

Use a supported protocol with an App key. Check route and app evidence separately.

| Task | Reference |
| --- | --- |
| Connect a client or SDK | [Client recipes](LOCAL_CLIENT_RECIPES.md), [client compatibility matrix](COMPATIBILITY_TARGET_MATRIX.md) |
| Check supported endpoints and limits | [Route support matrix](SUPPORT_MATRIX.md), [vendor facades](VENDOR_FACADES.md) |
| Discover capabilities, workflows and receipts | [API targets](API_TARGETS.md), [core contracts](CORE_CONTRACTS.md) |
| Call typed decision models | [Decision guide](ops/DECISIONS.md) |
| Configure keys and permissions | [Authentication guide](../AUTH_GUIDE.md) |

The running instance's `/docs` page is the interactive API reference. Discovery reflects that instance's configuration and your permissions. Some design documents describe target behavior; the support matrix and implementation notes state the current limits.

## Workflow builder

Workflows are manifests with typed inputs and explicit engine bindings.

| Task | Reference |
| --- | --- |
| Build or package a workflow | [Extension architecture](EXTENSION_ARCHITECTURE.md), [extension targets](EXTENSION_TARGETS.md) |
| Control image and video inputs | [Conditioning controls](CONDITIONING_CONTROLS.md) |
| Work with likeness and identity | [Identity and likeness](IDENTITY_LIKENESS.md) |
| Understand long-video planning | [Long-video design](long-video-design.md) |

## Contributor

Start with [Contributing](../CONTRIBUTING.md), [Code of Conduct](../CODE_OF_CONDUCT.md), and the [security policy](../SECURITY.md).

| Area | Reference |
| --- | --- |
| Product intent | [Product strategy](PRODUCT_STRATEGY.md) |
| Engine integrations | [Engine knowledge boundary](design/ENGINE_KNOWLEDGE_BOUNDARY.md), [registry design](design/ENGINE_REGISTRY_DESIGN.md), [transforms](design/ENGINE_TRANSFORMS_DESIGN.md) |
| UI and API boundaries | [UI steering](design/WEB_UI_STEERING.md), [UI cutover](design/UI_CUTOVER.md), [web API contract](design/web-api-contract.md) |
| Compatibility verification | [SDK harness](SDK_COMPATIBILITY_HARNESS.md), [portable browser checks](../scripts/ui/README.md) |
| Scope and unfinished work | [Known work](KNOWN_WORK.md), [scope](SLVP_SCOPE.md) |

Design documents explain decisions and planned work; they are not deployment instructions. Use the operator guide and current configuration reference when changing an instance.
