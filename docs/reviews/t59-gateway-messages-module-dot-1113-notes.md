# T59 gateway message module extraction

Base: 3479f67a7110e4e12935c5825c602a65b302ded7.

## Changes
- New gateway/gateway_messages.py owns the original path, fields, validator, loader and import-time immutable mapping. Loader block copied verbatim, including all validation/error text.
- toolcall_detection removes Formatter/MappingProxyType imports and loader, imports gateway_messages, and reads gateway_messages.GATEWAY_MESSAGES at advisory call time. Path, Mapping and yaml remain necessary for marker loading.
- discovery_metadata replaces toolcall_detection import with gateway_messages; both surface messages use module lookup.
- extension_catalog replaces toolcall_detection as _message_data with gateway_messages; metadata_description uses module lookup.
- test_gateway_messages_data imports gateway_messages for every loader call and message monkeypatch; detection import remains for detection behavior.
- test_t58_domain_data replaces toolcall_detection import with gateway_messages; all message reads/patches target new owner.
- Added exactly 1 pytest case: test_toolcall_detection_does_not_export_message_loader. No existing test cases removed.

## Lead integration required
Root gateway module deliberately not opened or edited. Replace its two toolcall_detection.GATEWAY_MESSAGES reads for long_video_not_supported and video_extend_not_supported with gateway_messages.GATEWAY_MESSAGES, importing the module, not a bound mapping. Do this before running root-handler parity tests; those tests intentionally patch the new owner.

## Verification and uncertainty
No tests, imports, generators or application code executed, as requested. Static in-memory source inspection confirmed loader block equality and no legacy message references in the five changed scoped files. This is not a whole-repository git grep; unknown out-of-scope consumers were not inspected. No shim or load_gateway_messages re-export added.
Least certain assertions: root endpoint monkeypatch parity depends on lead's pending two-line/root import change; package import ordering/cycles cannot be executed here; no-loader assertion imports the existing detector (which still loads marker data normally), so unrelated package startup failures remain possible. Existing root-handler tests are unverified until integration. The new module uses the same parents[1] path because it remains in gateway/.

Suggested owner checks after root integration: run scoped pytest files and grep all tracked Python files for toolcall_detection.GATEWAY_MESSAGES and detection.load_gateway_messages. Expected zero matches outside this explanatory notes file; the root references should also be gone after integration.
