# Transform pack file validation

Base: e9ea074ef701acfbabfb13382d8d803e8bed6354.

Expect it to work because checks run only in load_transform_packs before staged registration. All six constructors' cfg reads match the brief's closed sets; no additional consumed fields found. DefaultParamsTransform documents only_if_absent but never reads it, so file-only policy rejects it rather than silently accepting an ineffective option. Confidence: 85%, unverified.

Changes: safe_load_strict; closed primitive fields; phase and when structure/op vocabulary; clamp bound presence/order; nonblank unpadded parameter paths and set/drop entries. Strict-YAML errors are attributed to their enclosing pack using safe syntax nodes and source marks, not accepted fallback data. Existing pack-row errors and all primitive factories/constructors remain unchanged. engine_admin._validate_transforms still calls _build_primitive; request/response pipelines remain untouched.

Tests authored: exactly 25 pytest cases added: one parametrized function with 24 rejection cases (23 dict cases plus one raw duplicate-key YAML case), and one shipped-file compatibility case. Each refusal checks path, pack ID, field and unchanged preexisting target registry; valid preceding row must not publish. Zero tests executed, as instructed. No passing count claimed.

Least-certain assertions requiring lead verification:
1. Strict YAML problem_mark for the repeated nested max key lies within the composed containing pack's span; diagnostic test covers ordinary duplicate keys, not YAML merge/alias source locations.
2. Existing import-time registry initialization accepts shipped packs; static file contains only allowed fields, but no import/test run was performed.
3. Registry object identity and ordering assertions rely on resolve returning its stored list and names preserving insertion order, confirmed by source only.
4. No claim of full predicate semantic validation: optional value remains optional per brief; in/gt/lt operand types are not newly restricted. Clamp min:null/max:null remain present bounds under the specified presence-only rule, matching constructor compatibility. This patch does not address NaN or all ineffective semantic configurations.

Lead verification: run the touched pytest file in the project's normal environment; review its 25 added cases against the budget. No publication, deployment or live owner-engine database changes were performed.
