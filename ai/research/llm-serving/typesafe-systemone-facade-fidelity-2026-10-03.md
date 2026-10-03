---
title: "TypeSafe System One facade fidelity for Vivid Fish Gateway"
date: 2026-10-03
topic: llm-serving
tags: [typesafe, systemone, decisions-api, gateway, sdk-fidelity]
status: draft
sources: [typesafe-api-live, typesafe-models-live, typesafe-api-snapshot, typesafe-sdk-local-probe]
---

## Claims

The TypeSafe System One HTTP endpoint is `POST https://api.typesafe.ai/v1/systemone` with bearer auth and a JSON request containing `state`, `model`, and `questions`. `state` is a string, object, or array. `questions` is a map from caller-chosen ids to typed question objects. The ids return under the same keys and are not part of inference.

The three question types are:

- `noul`: requires `type: "noul"` and `instructions`; optional `criteria` object with only `true` and `false`.
- `choice`: requires `type: "choice"`, `instructions`, and `criteria` as an option map. Choice allows at most 255 options. The docs type each option description as string, object, array, or null.
- `score`: requires `type: "score"`, `instructions`, and `criteria` as an ordered array. Score accepts 2 to 10 levels.

The System One response requires:

- `model`: string model/version that answered.
- `answers`: object keyed by request question ids.
- `usage`: object with `input_tokens` and `output_tokens`.

Every answer carries a mandatory `type` discriminator matching the question. `noul` answers carry `noul`; `choice` answers carry `choice`, `probabilities`, and `confidence`; `score` answers carry `score`, `legend`, `probabilities`, and `confidence`.

`GET /v1/models` is not OpenAI-shaped. It returns an object with a `models` array. Each model entry has `name`, `description`, and `release_date`. The Python SDK example iterates `client.models.list().models` and reads `model.name`, `model.release_date`, and `model.description`.

The documented HTTP error statuses are 401, 422, 429, and 529. The docs say errors use standard status codes with a JSON body describing what went wrong; they do not show a concrete JSON error example. The SDK maps server error responses to `TypeSafeAPIError` subclasses after retries.

The Python SDK package is `typesafe-sdk` and imports as `typesafe_sdk`. In a local probe, `TypeSafeClient` exposed `models` and `system_one`. `TypeSafeClient.system_one()` accepts `state`, `questions`, optional `model`, and optional `extra_body`. `extra_body` can override top-level body keys, so the gateway facade should reject unsupported top-level fields rather than silently forward or ignore fields that could change output.

The SDK response models confirm the wire shape:

- `SystemOneResponse` fields: `model`, `usage`, `answers`.
- `Usage` fields: `input_tokens`, `output_tokens`.
- `NoulAnswer`, `ChoiceAnswer`, and `ScoreAnswer` are discriminated by `type`.
- `ListModelsResponse` has only `models`, a tuple of `ModelMetadata`; `ModelMetadata` requires `name`, `description`, and `release_date`.

The sync SDK uses `httpx2.Client`. Its `ASGITransport` is async-only and does not implement `handle_request`, so an offline sync-client fidelity test needs either a real local HTTP server or a small custom `httpx2.BaseTransport` that forwards requests into FastAPI's `TestClient` and returns `httpx2.Response`.

## Sources

**typesafe-api-live**

URL: https://docs.typesafe.ai/api.md
Fetched: 2026-10-03 with `urllib.request` from this worktree.
Size: 11,344 bytes.
sha256: `6b760275f89341fe15cff80c28aacae94d7d0c0c99afcd987ec0bca680b5713a`.

Relevant facts captured from the page:

- Request body has required `state`, `model`, and `questions`.
- Noul, Choice, and Score are the three question types.
- Choice maximum is 255 options.
- Score accepts 2 to 10 levels.
- Response has required `model`, `answers`, and `usage`.
- Answer types carry `type`.
- Errors list statuses 401, 422, 429, and 529.

**typesafe-models-live**

URL: https://docs.typesafe.ai/models.md
Fetched: 2026-10-03 with `urllib.request` from this worktree.
Size: 6,684 bytes.
sha256: `15351adaab85874b5f1a942fc2fdf3c7b3ecadcc936d580793d734855a0038f3`.

Relevant facts captured from the page:

- Current alias table lists `jev-latest` and `jev-preview`, both pointing to `jev-1.13.0` at fetch time.
- `GET /v1/models` returns names accepted by the `model` field.
- The documented response field is `models`, an array.
- Each model entry requires `name`, `description`, and `release_date`.

**typesafe-api-snapshot**

Path: `/home/tnunamak/.tmp/decisions-design/ts_api.md`.
Read: 2026-10-03.

This snapshot matched the live `api.md` request/response sections used for the facade correction.

**typesafe-sdk-local-probe**

Commands run from this worktree on 2026-10-03:

```bash
uv run --with typesafe-sdk python - <<'PY'
from typesafe_sdk import TypeSafeClient
import inspect
c = TypeSafeClient(api_key="x", base_url="http://127.0.0.1:9")
print(inspect.signature(c.system_one))
print(inspect.signature(c.models.list))
PY
```

And:

```bash
uv run --with typesafe-sdk python - <<'PY'
from typesafe_sdk._core import response_types
for name in ["ListModelsResponse", "SystemOneResponse", "Usage", "NoulAnswer", "ChoiceAnswer", "ScoreAnswer", "ModelMetadata"]:
    obj = getattr(response_types, name, None)
    print(name, getattr(obj, "model_fields", None))
PY
```

Findings:

- Import module: `typesafe_sdk`.
- Client class: `TypeSafeClient`.
- Methods: `models.list()` and `system_one(...)`.
- `ListModelsResponse.model_fields == {"models": ...}`.
- `ModelMetadata` requires `name`, `description`, and `release_date`.
- `SystemOneResponse` requires `model` and `usage`; `answers` is a dict of discriminated `NoulAnswer | ChoiceAnswer | ScoreAnswer`.

## Implementation consequences

For `gateway/decisions_api.py`:

- `/typesafe/v1/models` must return `{"models": [...]}` with `name`, `description`, and `release_date` fields, while gateway metadata can remain an extension field.
- Backend System One responses must be shape-checked before returning to clients so the official SDK can parse them.
- The facade should reject unsupported top-level and question fields. The SDK's `extra_body` can add or override top-level fields, and silently accepting those fields would be dishonest.
- Rejection and backend-error receipts should be written after authentication, with auth passed to the receipt callback so the parent can owner-scope receipt storage.
- Receipt callback failure must not convert a successful decision response into a 500.

## 2026-10-03 official OpenAPI schema pin for findings 5 and 6

Source verified live before pinning: `https://api.typesafe.ai/openapi.json`.

Fetch command used in the gateway worktree:

```bash
curl -fsSL https://api.typesafe.ai/openapi.json -o tests/fixtures/typesafe_openapi.json
```

Hash metadata:

| Artifact | SHA-256 |
| --- | --- |
| Raw fetched bytes before formatting | `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5` |
| Pinned pretty-printed fixture `tests/fixtures/typesafe_openapi.json` | `032526ef1c1e00fc5330f66af5ee296c043355e10f1201983193ac6108b82975` |

Relevant schema evidence:

- `components.schemas.NoulQuestion.properties.instructions.anyOf` includes `string`, `object`, `array`, and `null`. Null instructions are therefore part of the wire schema, not an invalid type by themselves.
- `components.schemas.NoulQuestion.properties.criteria.anyOf` includes `NoulCriteria` and `null`.
- `components.schemas.NoulCriteria.properties.true.anyOf` and `.false.anyOf` each include `string`, `object`, `array`, and `null`. A request such as `criteria={"true":"Spam","false":null}` is schema-valid.
- `components.schemas.HTTPValidationError.properties.detail` is an array of `ValidationError` records.
- `components.schemas.ValidationError.required` is `loc`, `msg`, and `type`. `loc` is an array of strings or integers. This supports a TypeSafe/FastAPI-style envelope such as `{"detail":[{"loc":["body","state"],"msg":"Field required","type":"missing"}]}` for local System One request validation failures.

Regression tests added from this evidence:

- `test_typesafe_noul_accepts_null_instructions_with_meaningful_nullable_criteria`
- `test_systemone_request_validation_errors_use_typesafe_detail_envelope`
- `test_pinned_openapi_documents_nullable_noul_fields_and_detail_validation_envelope`

Additional schema reconciliation: an earlier local `api.md` excerpt said Score criteria needed at least two levels, but the live pinned OpenAPI fixture is authoritative for the gateway facade and declares `components.schemas.ScoreQuestion.properties.criteria.minItems == 1`. The facade and tests now follow the pinned OpenAPI for request validation while keeping the local advertised maximum of 10 levels.
