# Local language input modality contract
Base: 2e76fa596f9cf63fcaae675300a2bafbc868ae8b.

The pure reader now accepts MetadataContract (default METADATA_CONTRACT), maps normalized backend tags through language implications, and emits text first followed by implied modalities in contract input order. A single named projector tag constant translates mmproj source evidence into the same rule. No schema, catalog, budget, OpenAPI, or golden edits.

Added four test methods (+4 collected cases; subtests do not increase pytest collection): current projector/vision/Vision/neither/whitespace behavior; renamed sight tag; remapped vision modality for backend and projector; order, type filter and deduplication.

Validation: parsed modified implementation and new tests with Python AST. Executed the four added methods against the exact extracted reader/helper/constant and an explicit stand-in dataclass contract representing the current vision-to-image row: 4 passed. This is an isolated logic check, NOT repository import or pytest execution, and does not validate dependencies or the module-loaded vocabulary. Repository run_check(default) returned capability_not_available: worker execution cannot use host_execution. Full suite and existing parity tests remain unrun. Budget untouched.

Allowed current representative backend data: tabby lists vision/chat/completions; kobold chat/completions; tts audio/speech; stt audio/transcriptions; embeddings embeddings; comfyui images/generations/videos/audio.generate. Only vision matches the current language implication. Lowercasing remains unchanged; no new stripping. Thus current logic is equivalent for arbitrary lists of backend words, given the current single vision-to-image implication and text/image vocabulary. Full shipped backend definitions are not in the read allowlist and were not read.

The parity fixture was read, never regenerated or patched. Workspace diff must contain only implementation, existing tests, and this notes file. Published bytes have not been modified, but generated-output byte parity needs the owner's existing parity check. Import dependency closure needs the owner's normal test run; no claim of integration readiness until those checks pass.
