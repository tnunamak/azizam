# W6a public HTTP engine execution

Branch: `engine/w6a`. Base: r65 (`5f9a825f`) plus
`origin/dot/w4-followups` (`c3d9151b`). The production code and test ceiling
were checked at `350a8b07`; later report edits do not change that code.

A record opts in with `config.connection.dispatch: protocol` and a protocol id
whose description declares `executor: http-v1`. Connection and offering blocks
can also be top-level contract data. Public handlers execute chat (buffered and
streamed), models, embeddings, synchronous and submit/poll/fetch images, and
declared buffered speech. Queued images use the existing worker and settler.
The [how-to](../guides/ADD_HTTP_ENGINE_AS_DATA.md) gives the record and operation
names, credential binding, selection transaction and restart policy.

## Evidence

| Check | Result |
|---|---|
| `scripts/run-hermetic-pytest.sh -q -n 8` | 11,389 passed, 3 skipped, 1 xfailed; 3,517 subtests passed; 100.36 s |
| Hermetic collection | 11,393 items; ceiling raised by the 49 required regression cases |
| `npx vitest run` in `web/` | 2,667 passed in 173 files |
| `npx tsc --noEmit` in `web/` | Passed |
| Independent local review, different requested model | PASS after fail-before reproductions and repairs; checker read code and owner-run test evidence |
| Engine-knowledge ratchet | Total 942 → 941; `proxy.py` 618 → 617; no new file entries |

The frozen `tests/test_engine_contract_oracle.py` has no edits. Its registry,
route graph, catalog, compiler/current and denial-receipt observations pass.
`tests/test_engine_protocol_legacy_wire.py` compares the 19 seeded record
projections and compiled config, six sanitized live-shaped record projections
and compiled config, and 13 public chat/stream/embedding wires with a golden
captured before dispatch changed, at `43728181`. It preserves request URL,
body bytes, headers, timeout and final response bytes; generated correlation
ids are excluded. The live-shaped data includes the local systemd hop, its
eight model names, a stored-key remote record, environment-key substrate,
embeddings timeout and base path, and systemd speech records. The compiler
preserves both `current` variants. The full suite also exercises existing
speech, transcription, remote-provider and internal-hop fixtures.

The second engine lives only in
`tests/fixtures/engine_protocol/prism/{prism.yml,engine.json}`. Its first
addition commit (`e1f5ae9c`) changed four files under `tests/` and no production
file. Production code contains no reference to its name. Public tests compare
its changed paths, request field names, enum/clamp mappings, custom SSE,
embeddings and image ticket/poll/fetch wires. They assert downstream response
shapes, persisted jobs and receipt outcomes, image replay/conflict, class-bound
credentials, admission/plan denial, asset ownership, switch verification and
rollback, lock-scoped no-switch permission, final-byte leases and cancellation
through the actual queued-image worker.

## W4 review follow-ups

`tests/test_engine_protocol_w4_followups.py` contains 13 cases. Ten failed
before their fixes; the three existing deadline/framing behaviors were pinned
without changing their behavior.

| Request | Evidence and result |
|---|---|
| Do not retry DNS, TLS or blocked transport failures | Three fail-before cases now return the original typed failure after one poll |
| Keep the last scrubbed failure in deadline errors | Fail-before case now retains diagnostic context without its test credential |
| Raise the poll interval floor | Fail-before case refuses 0.05 s and accepts 0.5 s; no shipped description needs less |
| Treat cancel 404/409/410 as already done | Three fail-before race cases now complete cancellation successfully |
| Clamp sleep and check the deadline before polling | Two deterministic clock cases passed before and after; no poll starts at or after the deadline |
| SSE CRLF split across chunks | Executor regression passed before and after with each CR/LF pair split |
| Count excerpt characters | Fail-before Unicode case now returns 160 characters without splitting UTF-8 |
| Refuse a lone surrogate in a path value | Fail-before case now raises `MappingError` before transport |

The core job loop shares the permanent-failure predicate, and public tests
exercise both a blocked poll and a transient 503 followed by completion.

## Overhead and limits

The hermetic `scripts/bench_engine_protocol.py --json` run uses an in-memory
fake upstream and best-of-seven or best-of-nine samples.

| Non-streaming case | Executor | Direct mapping | Ratio |
|---|---:|---:|---:|
| Request map plus JSON serialization | 9.052 µs | 7.137 µs | 1.27× |
| Five rules with equal defensive copying | 2.049 µs | 0.685 µs | 2.99× |
| Whole invocation | 100.230 µs | 62.142 µs | 1.61× |
| Connection/adapter construction plus invocation | 137.485 µs | 66.284 µs | 2.07× |

Including connection setup adds 0.071 ms. This measures mapping and execution
machinery, not public-handler, admission-database, network or inference latency.
Isolated response mapping is 12.63× a container-sharing function, and the SSE
parse/map case is 3.10×; the roughly 3× target is met by the comparable
non-streaming work above, not by every isolated primitive.

No live record was read or switched, no real inference engine was used, and no
service was restarted. Playwright and live latency were not checked. Startup
retains the existing fail-closed policy for uncertain running media work;
this slice does not automatically resume that work after a process crash.
