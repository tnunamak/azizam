# Direct route receipt-owner migration report

## Scope

Migrated the direct-current (`_forward_current_loaded_openai_request`) and
reachable direct-native catch-all (`proxy_request`) receipt lifecycles to
`ReceiptLifecycleOwner`. Responses, provider-native passthrough, and LiteLLM
were not changed.

## Lifecycle boundary

- The current-loaded owner is created after alias/extension preparation and
  before active-model resolution.
- The direct-native owner is created after authentication, path/body parsing,
  and target selection, before availability, health, autoload, connect, read,
  or stream work.
- Terminal request-shape facts are attached through each owner's
  `on_terminal` callback. Routes report normal outcomes; the owner alone
  performs terminal persistence.
- Streaming responses use `handoff_stream`, retaining owner scope until the
  response iterator completes.

## Preserved behavior

The existing error responses, status codes, current-loaded headers and
translation pipeline, native health/autoload behavior, disconnect handling,
and timeout/connect metrics remain in their original branches.

## Verification

Ran:

```text
./venv/bin/python -m py_compile proxy.py
./venv/bin/python -m pytest -q tests/test_proxy_gateway_acceptance.py \
  tests/test_route_receipts.py tests/test_receipt_lifecycle_cancellation.py \
  tests/test_engine_knowledge_boundary.py
```

Result: `69 passed, 12 subtests passed`.

Also ran `git diff --check` and searched `proxy.py` for the removed direct
route lifecycle helpers and direct begin/finalize calls; none remain.
